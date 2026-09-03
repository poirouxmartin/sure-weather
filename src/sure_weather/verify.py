from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .calibration import learn_stats
from .config import Config
from .fusion import _VARIABLE_TOLERANCE, fuse, spatial_stats
from .models import ALL_VARIABLES, OBSERVABLE_VARIABLES
from .storage import Storage

# Same ground-truth priority as calibration (station > reanalysis):
# validating only against the archive while the fusion learns from stations
# would systematically mis-measure reliability.
_GT_RANK = {"station": 0, "model": 1}


def _best_truth_index(observations, kinds) -> dict:
    index: dict[tuple[str, datetime], float] = {}
    rank: dict[tuple[str, datetime], int] = {}
    for o in observations:
        rounded = o.time.replace(minute=0, second=0, microsecond=0)
        key = (o.variable, rounded)
        r = _GT_RANK.get(kinds.get(o.provider, "model"), 1)
        if key not in index or r < rank[key]:
            index[key] = o.value
            rank[key] = r
    return index


# Confidence bands reported in the reliability table.
BANDS = [
    (0.000, 0.900, "0.00-0.90"),
    (0.900, 0.950, "0.90-0.95"),
    (0.950, 0.990, "0.95-0.99"),
    (0.990, 0.999, "0.99-0.999"),
    (0.999, 1.001, "0.999-1.00"),
]


def verify(
    storage: Storage,
    config: Config,
    cell_key: str,
    split_ratio: float = 0.7,
    now: datetime | None = None,
) -> dict:
    """Replay fusion on the residual window and measure empirical hit rates.

    Confidence is validated like a probabilistic forecast: for each past valid
    time, fuse the stored analysis values, compare the consensus to the ground
    truth (era5), and bucket results by stated confidence. A reliable system
    has empirical hit rate ~ stated confidence in every band.

    Out-of-sample: stats are learned from the oldest `split_ratio` of the
    window, validation runs on the newest part. This avoids inflating the
    result with the very data the stats were trained on.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=config.residual_window_days)
    cutoff = since + (now - since) * split_ratio

    # Train on residuals before the cutoff.
    residuals = storage.residuals_window(None, None, since, cutoff)
    stats = {
        (s.provider, s.cell_key, s.variable, s.horizon_h): s
        for s in learn_stats(residuals)
    }
    cells = storage.get_cells()
    if cells:
        stats = spatial_stats(stats, cells)
    kinds = {p.name: p.kind for p in storage.get_providers()}

    # Validation set: analysis forecasts in the newest part of the window.
    forecasts = storage.forecasts_in_window(
        [cell_key], list(ALL_VARIABLES), cutoff, now
    )
    observations = storage.observations_in_window(
        [cell_key], list(OBSERVABLE_VARIABLES), cutoff, now
    )
    obs_index = _best_truth_index(observations, kinds)

    # Fuse all validation forecasts at once; the fusion groups per (var, valid_at).
    results = fuse(forecasts, stats, kinds, config, now)

    hits: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
    calibrated_flags: dict[tuple[str, int], bool] = {}
    for r in results:
        rounded = r.valid_at.replace(minute=0, second=0, microsecond=0)
        obs = obs_index.get((r.variable, rounded))
        if obs is None:
            continue
        tolerance = _VARIABLE_TOLERANCE.get(r.variable, 1.5)
        hit = abs(r.consensus - obs) <= tolerance
        band = _band_index(r.confidence)
        hits[(r.variable, band)].append((r.confidence, 1.0 if hit else 0.0))
        calibrated_flags[(r.variable, band)] = r.calibrated

    return _build_report(hits, calibrated_flags)


def _band_index(confidence: float) -> int:
    for i, (lo, hi, _label) in enumerate(BANDS):
        if lo <= confidence < hi or (i == len(BANDS) - 1 and confidence >= lo):
            return i
    return 0


def _build_report(
    hits: dict[tuple[str, int], list[tuple[float, float]]],
    calibrated_flags: dict[tuple[str, int], bool],
) -> dict:
    report: dict[str, list] = {"bands": []}
    for band_idx, (_lo, _hi, label) in enumerate(BANDS):
        bucket: list[tuple[float, float]] = []
        for (var, band), entries in hits.items():
            if band == band_idx:
                bucket.extend(entries)
        if not bucket:
            continue
        n = len(bucket)
        avg_conf = sum(c for c, _ in bucket) / n
        emp = sum(h for _, h in bucket) / n
        report["bands"].append(
            {
                "band": label,
                "n": n,
                "avg_confidence": round(avg_conf, 4),
                "empirical_rate": round(emp, 4),
                "gap": round(emp - avg_conf, 4),
            }
        )
    return report


def report_by_variable(
    storage: Storage,
    config: Config,
    cell_key: str,
    split_ratio: float = 0.7,
    now: datetime | None = None,
) -> dict:
    """Reliability per variable, pooling all confidence bands."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=config.residual_window_days)
    cutoff = since + (now - since) * split_ratio

    residuals = storage.residuals_window(None, None, since, cutoff)
    stats = {
        (s.provider, s.cell_key, s.variable, s.horizon_h): s
        for s in learn_stats(residuals)
    }
    cells = storage.get_cells()
    if cells:
        stats = spatial_stats(stats, cells)
    kinds = {p.name: p.kind for p in storage.get_providers()}

    forecasts = storage.forecasts_in_window(
        [cell_key], list(ALL_VARIABLES), cutoff, now
    )
    observations = storage.observations_in_window(
        [cell_key], list(OBSERVABLE_VARIABLES), cutoff, now
    )
    obs_index = _best_truth_index(observations, kinds)

    results = fuse(forecasts, stats, kinds, config, now)

    per_var: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in results:
        rounded = r.valid_at.replace(minute=0, second=0, microsecond=0)
        obs = obs_index.get((r.variable, rounded))
        if obs is None:
            continue
        tolerance = _VARIABLE_TOLERANCE.get(r.variable, 1.5)
        hit = abs(r.consensus - obs) <= tolerance
        per_var[r.variable].append((r.confidence, 1.0 if hit else 0.0))

    out: dict[str, object] = {}
    for var, bucket in sorted(per_var.items()):
        n = len(bucket)
        avg_conf = sum(c for c, _ in bucket) / n
        emp = sum(h for _, h in bucket) / n
        out[var] = {
            "n": n,
            "avg_confidence": round(avg_conf, 4),
            "empirical_rate": round(emp, 4),
            "gap": round(emp - avg_conf, 4),
        }
    return out
