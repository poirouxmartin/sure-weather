from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import Config
from .fusion import ProviderStat, horizon_bucket
from .models import ForecastSample, Observation, Residual, Variable
from .storage import Storage, _parse_dt


def compute_residuals(
    forecasts: list[ForecastSample], observations: list[Observation]
) -> list[Residual]:
    """Match each forecast to the observation at its valid time, per cell+variable.

    A forecast is matched to the observation that falls closest in time (within
    30 minutes) in the same cell and variable. One residual per (provider, cell,
    variable, valid_at).
    """
    # Index observations by (cell, variable, rounded time).
    obs_index: dict[tuple[str, str, datetime], Observation] = {}
    for o in observations:
        rounded = o.time.replace(minute=0, second=0, microsecond=0)
        obs_index[(o.cell_key, o.variable, rounded)] = o

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
        residuals.append(
            Residual(
                provider=f.provider,
                cell_key=f.cell_key,
                variable=f.variable,
                valid_at=f.valid_at,
                horizon_h=horizon_bucket(f.valid_at, f.issued_at),
                predicted=f.value,
                observed=obs.value,
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
    row = storage._conn.execute(
        "SELECT MAX(valid_at) FROM residuals WHERE provider = 'gfs_seamless'"
    ).fetchone()
    if row is None or row[0] is None:
        return True
    latest = _parse_dt(row[0])
    return (datetime.now(timezone.utc) - latest) > timedelta(
        hours=config.calibration_interval_h
    )
