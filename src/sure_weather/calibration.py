from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import Config
from .fusion import ProviderStat, horizon_bucket
from .models import ForecastSample, Observation, Residual, Variable
from .storage import Storage, _parse_dt
from .collectors.open_meteo import DEFAULT_ARCHIVE

PRIMARY_ARCHIVE = DEFAULT_ARCHIVE

# Providers considered "ground truth" in priority order for residual matching.
# Real station observations beat reanalysis: they measure the actual atmosphere
# instead of reconstructing it, so residuals computed against them are more
# honest and the learned bias/rmse transfers better to live forecasts.
_GROUND_TRUTH_RANK = {"station": 0, "model": 1}


def compute_residuals(
    forecasts: list[ForecastSample],
    observations: list[Observation],
    kinds: dict[str, str] | None = None,
) -> list[Residual]:
    """Match each forecast to the observation at its valid time, per cell+variable.

    A forecast is matched to the observation that falls closest in time (within
    30 minutes) in the same cell and variable. One residual per (provider, cell,
    variable, valid_at). When several sources are present at the same timestamp
    the best ground truth wins: station observations outrank reanalysis.
    """
    kinds = kinds or {}
    # Index observations by (cell, variable, rounded time), preferring the
    # highest-ranked ground-truth provider (station > reanalysis).
    obs_index: dict[tuple[str, str, datetime], Observation] = {}
    for o in observations:
        rounded = o.time.replace(minute=0, second=0, microsecond=0)
        key = (o.cell_key, o.variable, rounded)
        rank = _GROUND_TRUTH_RANK.get(kinds.get(o.provider, "model"), 1)
        current_rank = (
            _GROUND_TRUTH_RANK.get(kinds.get(obs_index[key].provider, "model"), 1)
            if key in obs_index
            else 1
        )
        if key not in obs_index or rank <= current_rank:
            obs_index[key] = o

    residuals: list[Residual] = []
    for f in forecasts:
        rounded = f.valid_at.replace(minute=0, second=0, microsecond=0)
        obs = obs_index.get((f.cell_key, f.variable, rounded))
        if obs is None:
            # Try adjacent hour.
            for delta in (timedelta(hours=1), timedelta(hours=-1)):
                obs = obs_index.get((f.cell_key, f.variable, rounded + delta))
                if obs is not None:
                    break
        if obs is None:
            continue
        horizon = (f.valid_at - f.issued_at).total_seconds() / 3600.0
        if horizon < 0:
            continue
        # Wind direction is circular: a linear predicted-observed bias
        # (350° vs 10° → 340°) is meaningless and would explode rmse, wiping
        # the provider's weight. Directions still produce residuals (for
        # dispersion), but with zero bias so only rmse carries the error.
        # The fusion never bias-corrects directions anyway.
        if f.variable == "wind_direction_10m":
            predicted, observed = obs.value, obs.value
        else:
            predicted, observed = f.value, obs.value
        residuals.append(
            Residual(
                provider=f.provider,
                cell_key=f.cell_key,
                variable=f.variable,
                valid_at=f.valid_at,
                horizon_h=horizon_bucket(f.valid_at, f.issued_at),
                predicted=predicted,
                observed=observed,
            )
        )
    return residuals


def learn_stats(residuals: list[Residual]) -> list[ProviderStat]:
    """Aggregate residuals into per (provider, cell, variable, horizon) stats."""
    groups: dict[tuple[str, str, str, float], list[float]] = defaultdict(list)
    for r in residuals:
        groups[(r.provider, r.cell_key, r.variable, r.horizon_h)].append(r.bias)

    now = datetime.now(timezone.utc)
    stats: list[ProviderStat] = []
    for key, biases in groups.items():
        arr = np.array(biases, dtype=float)
        samples = len(arr)
        stats.append(
            ProviderStat(
                provider=key[0],
                cell_key=key[1],
                variable=key[2],
                horizon_h=key[3],
                samples=samples,
                bias=float(np.mean(arr)),
                rmse=float(np.sqrt(np.mean(arr**2))),
                updated_at=now,
            )
        )
    return stats


def calibration_due(storage: Storage, config: Config) -> bool:
    """True if no calibration has been done in the last interval."""
    row = storage.locked_fetchone("SELECT MAX(valid_at) FROM residuals")
    if row is None or row[0] is None:
        return True
    latest = _parse_dt(row[0])
    return (datetime.now(timezone.utc) - latest) > timedelta(
        hours=config.calibration_interval_h
    )
