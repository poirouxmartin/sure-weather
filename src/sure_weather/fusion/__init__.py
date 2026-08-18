from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from ..config import Config
from ..models import ForecastSample

# Baseline weight per provider kind when no residual history exists yet.
_KIND_BASE_WEIGHT = {"model": 1.0, "station": 2.0}


@dataclass(frozen=True)
class ProviderStat:
    """Learned calibration for one (provider, cell, variable, horizon bucket)."""

    provider: str
    cell_key: str
    variable: str
    horizon_h: float
    samples: int
    bias: float  # predicted - observed, learned
    rmse: float
    updated_at: datetime


def horizon_bucket(
    valid_at: datetime, issued_at: datetime, now: datetime | None = None
) -> float:
    """Horizon in hours from issue to valid time, snapped to a coarse bucket."""
    h = (valid_at - issued_at).total_seconds() / 3600.0
    if h <= 6:
        return 3.0
    if h <= 24:
        return 12.0
    if h <= 72:
        return 48.0
    return 120.0


def _inverse_variance_weight(rmse: float, samples: int) -> float:
    """1/var scaled by samples: more samples with low rmse dominate."""
    if samples <= 0 or rmse <= 0:
        return 1.0
    return samples / (rmse * rmse)


def base_weight(
    stat: ProviderStat | None,
    provider_kind: str,
    horizon_h: float,
    now: datetime | None = None,
) -> float:
    """Static confidence: from learned rmse when available, else kind baseline."""
    now = now or datetime.now(timezone.utc)
    if stat is None or stat.samples < 3:
        return _KIND_BASE_WEIGHT.get(provider_kind, 1.0)
    age_h = (now - stat.updated_at).total_seconds() / 3600.0
    staleness = np.exp(-age_h / (8 * 24))  # learned stats decay over ~8 days
    return _inverse_variance_weight(stat.rmse, stat.samples) * staleness


@dataclass(frozen=True)
class FusionResult:
    variable: str
    valid_at: datetime
    consensus: float  # weighted, bias-corrected value
    bias_corrected: bool
    dispersion: float  # spread of providers around consensus (IQR or MAD)
    confidence: float  # 0..1 composite score
    contributors: tuple[tuple[str, float, float], ...]  # (provider, raw_value, weight)


def _mad(values: np.ndarray) -> float:
    if len(values) == 0:
        return 0.0
    med = float(np.median(values))
    return float(np.median(np.abs(values - med)))


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Weighted median via the standard pivot approach."""
    order = np.argsort(values)
    v = values[order]
    w = weights[order]
    half = w.sum() / 2.0
    cumsum = np.cumsum(w)
    return float(v[np.searchsorted(cumsum, half)])


def _fuse_one(
    samples: list[ForecastSample],
    stats: dict[tuple[str, str, str, float], ProviderStat],
    kinds: dict[str, str],
    config: Config,
    now: datetime | None = None,
) -> FusionResult | None:
    """Fuse all providers for a single (variable, valid_at) across given samples."""
    if not samples:
        return None
    variable = samples[0].variable
    valid_at = samples[0].valid_at
    cell_key = samples[0].cell_key
    now = now or datetime.now(timezone.utc)

    pairs: list[tuple[str, float, float]] = []  # (provider, raw_value, weight)
    for s in samples:
        if s.variable != variable or s.valid_at != valid_at:
            continue
        bucket = horizon_bucket(s.valid_at, s.issued_at)
        stat = stats.get((s.provider, s.cell_key, variable, bucket))
        kind = kinds.get(s.provider, "model")
        w = base_weight(stat, kind, bucket, now)
        pairs.append((s.provider, s.value, w))

    if not pairs:
        return None

    raw = np.array([p[1] for p in pairs], dtype=float)
    weights = np.array([p[2] for p in pairs], dtype=float)

    # Robustness pass 1: reject gross outliers relative to weighted median.
    med = _weighted_median(raw, weights) if weights.sum() > 0 else float(np.median(raw))
    spread = _mad(raw) or float(np.std(raw)) or 1.0
    dev = np.abs(raw - med)
    keep = dev <= 4.0 * spread  # very conservative outlier gate
    if 2 <= keep.sum() < len(raw):
        raw = raw[keep]
        weights = weights[keep]
        pairs = [p for p, k in zip(pairs, keep) if k]

    # Bias correction per provider from learned stats.
    corrected: list[float] = []
    any_corrected = False
    for provider, value, w in pairs:
        bucket = horizon_bucket(valid_at, samples[0].issued_at)
        stat = stats.get((provider, cell_key, variable, bucket))
        if stat and stat.samples >= 3:
            corrected.append(value - stat.bias)
            any_corrected = True
        else:
            corrected.append(value)
    corrected_arr = np.array(corrected, dtype=float) if corrected else raw

    # Robustness pass 2: bias-corrected weighted consensus.
    consensus = (
        float(np.average(corrected_arr, weights=weights))
        if weights.sum() > 0
        else float(np.median(corrected_arr))
    )
    residual_spread = float(
        np.average(np.abs(corrected_arr - consensus), weights=weights)
    )
    residual_spread = residual_spread or float(np.std(corrected_arr)) or 0.0

    # Composite confidence 0..1.
    # - absolute spread: smaller residual spread -> higher confidence
    scale = _per_variable_scale(variable)
    spread_score = np.clip(1.0 - residual_spread / (2.0 * scale), 0.05, 1.0)
    # - agreement: more agreeing providers -> higher confidence
    agreement = min(1.0, len(pairs) / 4.0)
    # - coverage of provider kinds: station+model is better than model alone
    kinds_used = {kinds.get(p, "model") for p, _, _ in pairs}
    kind_score = 1.0 if "station" in kinds_used else 0.8
    confidence = float(spread_score * 0.6 + agreement * 0.3 + kind_score * 0.1)

    return FusionResult(
        variable=variable,
        valid_at=valid_at,
        consensus=consensus,
        bias_corrected=any_corrected,
        dispersion=residual_spread,
        confidence=confidence,
        contributors=tuple((p, v, w) for (p, v, w) in pairs),
    )


def _per_variable_scale(variable: str) -> float:
    """Typical scale of the variable, used to normalize dispersion into 0..1."""
    return {
        "temperature_2m": 2.0,
        "dew_point_2m": 2.0,
        "relative_humidity_2m": 10.0,
        "precipitation": 0.5,
        "precipitation_probability": 15.0,
        "cloud_cover": 15.0,
        "wind_speed_10m": 2.0,
        "wind_gusts_10m": 3.0,
        "pressure_msl": 1.5,
        "visibility": 5.0,
    }.get(variable, 2.0)


def fuse(
    samples: list[ForecastSample],
    stats: dict[tuple[str, str, str, float], ProviderStat],
    kinds: dict[str, str],
    config: Config,
    now: datetime | None = None,
) -> list[FusionResult]:
    """Fuse a list of forecast samples into one result per (variable, valid_at)."""
    by_key: dict[tuple[str, datetime], list[ForecastSample]] = {}
    for s in samples:
        by_key.setdefault((s.variable, s.valid_at), []).append(s)
    results: list[FusionResult] = []
    for key in sorted(by_key, key=lambda k: k[1]):
        r = _fuse_one(by_key[key], stats, kinds, config, now)
        if r is not None:
            results.append(r)
    return results
