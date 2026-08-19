from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from ..config import Config
from ..models import Cell, ForecastSample

# Baseline weight per provider kind when no residual history exists yet.
_KIND_BASE_WEIGHT = {"model": 1.0, "station": 2.0}

# Prior rmse used when a provider has no learned stats yet, per kind.
_KIND_PRIOR_RMSE = {"model": 2.0, "station": 1.0}

# Tolerance per variable: the fused value is considered "right" when it is
# within +/- tolerance of the true value. This makes confidence a calibrated
# probability (error within tolerance), not an arbitrary score.
# Values are chosen as meteorologically defensible "acceptable error" bounds.
# Visibility is in meters (up to ~70 km), so its tolerance is ±5 km.
_VARIABLE_TOLERANCE = {
    "temperature_2m": 1.5,
    "dew_point_2m": 2.5,
    "relative_humidity_2m": 10.0,
    "precipitation": 0.5,
    "precipitation_probability": 20.0,
    "cloud_cover": 20.0,
    "wind_speed_10m": 2.0,
    "wind_gusts_10m": 3.0,
    "pressure_msl": 2.0,
    "visibility": 5000.0,
}

# Physical bounds per variable: the fused value is clipped to these ranges.
_VARIABLE_BOUNDS = {
    "temperature_2m": (None, None),
    "dew_point_2m": (None, None),
    "relative_humidity_2m": (0.0, 100.0),
    "precipitation": (0.0, None),
    "precipitation_probability": (0.0, 100.0),
    "cloud_cover": (0.0, 100.0),
    "wind_speed_10m": (0.0, None),
    "wind_gusts_10m": (0.0, None),
    "pressure_msl": (None, None),
    "visibility": (0.0, None),
}


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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def spatial_stats(
    stats: dict[tuple[str, str, str, float], ProviderStat],
    cells: list[Cell],
    sigma_km: float = 30.0,
) -> dict[tuple[str, str, str, float], ProviderStat]:
    """Interpolate stats across cells so every cell has a calibration prior.

    A cell with no learned stats for a (provider, variable, horizon) borrows
    the inverse-distance-weighted stats of its neighbors (Gaussian kernel with
    `sigma_km` range). A cell with few samples blends its own stats with the
    spatial prior so early predictions are not overfit to a handful of hours.
    Returns a new dict; cells with no neighbors keep their entries as-is.
    """
    # Group stats by (provider, variable, horizon) across cells.
    keys_by_group: dict[tuple[str, str, float], list[tuple[str, ProviderStat]]] = {}
    for key, s in stats.items():
        group = (key[0], key[2], key[3])
        keys_by_group.setdefault(group, []).append((key[1], s))

    positions = {c.key: (c.lat, c.lon) for c in cells}
    out = dict(stats)

    for group, entries in keys_by_group.items():
        provider, variable, horizon = group
        # Cells with solid learned stats for this group.
        solid = {cell_key for cell_key, s in entries if s.samples >= 3}
        for cell in cells:
            own = next((s for ck, s in entries if ck == cell.key), None)
            if own is not None and own.samples >= 3:
                continue
            # Inverse-distance Gaussian weights over neighbor cells.
            lat, lon = cell.lat, cell.lon
            weights: list[tuple[float, ProviderStat]] = []
            for cell_key, s in entries:
                if cell_key == cell.key or cell_key not in positions:
                    continue
                plat, plon = positions[cell_key]
                d = _haversine_km(lat, lon, plat, plon)
                w = math.exp(-(d * d) / (2.0 * sigma_km * sigma_km))
                if w > 1e-3:
                    weights.append((w, s))
            if not weights:
                continue
            total = sum(w for w, _s in weights)
            bias = sum(w * s.bias for w, s in weights) / total
            # Blend rmse via squared weights to keep spread meaningful.
            rmse = math.sqrt(
                sum(w * s.rmse * s.rmse for w, s in weights) / total
            )
            samples = int(round(sum(w * s.samples for w, s in weights) / total))
            # Blend the cell's own few-sample stat with the spatial prior.
            if own is not None and own.samples > 0:
                own_w = own.samples / (own.samples + total)
                bias = own_w * own.bias + (1 - own_w) * bias
                rmse = math.sqrt(
                    own_w * own.rmse**2 + (1 - own_w) * rmse**2
                )
                samples = own.samples + samples
            out[(provider, cell.key, variable, horizon)] = ProviderStat(
                provider=provider,
                cell_key=cell.key,
                variable=variable,
                horizon_h=horizon,
                samples=max(samples, 1),
                bias=bias,
                rmse=rmse,
                updated_at=max((s.updated_at for _w, s in weights)),
            )
    return out


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
    confidence: float  # 0..1 calibrated probability, error within tolerance
    calibrated: bool  # False when no provider has learned stats (no ground truth)
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
    lo, hi = _VARIABLE_BOUNDS.get(variable, (None, None))
    if lo is not None:
        consensus = max(consensus, lo)
    if hi is not None:
        consensus = min(consensus, hi)
    residual_spread = float(
        np.average(np.abs(corrected_arr - consensus), weights=weights)
    )
    residual_spread = residual_spread or float(np.std(corrected_arr)) or 0.0

    # Calibrated confidence: probability that the fused value is within
    # +/- tolerance of the truth, assuming a normal error distribution.
    #   sigma_learned: inverse-variance propagation of each provider's noise.
    #   sigma_instant: observed disagreement among providers right now.
    # The provider noise is the residual spread around its own bias
    # (sqrt(rmse^2 - bias^2)): the systematic part was already removed by the
    # bias correction, so it must not count twice.
    variances: list[float] = []
    for provider, _v, _w in pairs:
        bucket = horizon_bucket(valid_at, samples[0].issued_at)
        stat = stats.get((provider, cell_key, variable, bucket))
        if stat and stat.samples >= 3:
            noise = math.sqrt(max(stat.rmse * stat.rmse - stat.bias * stat.bias, 0.0))
            noise = max(noise, 1e-6)
        else:
            noise = _KIND_PRIOR_RMSE.get(kinds.get(provider, "model"), 2.0)
        variances.append(noise * noise)
    sigma_learned = 1.0 / math.sqrt(sum(1.0 / v for v in variances))
    # Weighted mean absolute deviation ~ 0.8 * sigma for a normal distribution.
    sigma_instant = residual_spread / 0.8
    sigma_total = math.sqrt(sigma_learned**2 + sigma_instant**2)
    tolerance = _VARIABLE_TOLERANCE.get(variable, 1.5)
    # P(|N(0, sigma)| <= tol) = erf(tol / (sigma * sqrt(2))).
    confidence = float(math.erf(tolerance / (sigma_total * math.sqrt(2.0))))

    # Calibrated only if at least one provider has learned stats for this
    # (cell, variable, horizon). Without ground truth, confidence is a prior.
    bucket = horizon_bucket(valid_at, samples[0].issued_at)
    any_learned = False
    for provider, _, _ in pairs:
        s = stats.get((provider, cell_key, variable, bucket))
        if s is not None and s.samples >= 3:
            any_learned = True
            break

    return FusionResult(
        variable=variable,
        valid_at=valid_at,
        consensus=consensus,
        bias_corrected=any_corrected,
        dispersion=residual_spread,
        confidence=confidence,
        calibrated=any_learned,
        contributors=tuple((p, v, w) for (p, v, w) in pairs),
    )


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
