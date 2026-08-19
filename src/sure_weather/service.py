from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .calibration import compute_residuals, learn_stats
from .collectors import (
    ARCHIVE_MODELS,
    FORECAST_MODELS,
    HIGH_RES_MODELS,
    OpenMeteoCollector,
)
from .config import Config
from .fusion import (
    ProviderStat,
    _VARIABLE_TOLERANCE,
    fuse,
    horizon_bucket,
    spatial_stats,
)
from .grid import cell_from_point
from .models import ALL_VARIABLES, OBSERVABLE_VARIABLES, Cell, Provider
from .storage import Storage


# Key variables users care about for a "sure" promise; precipitation is shown
# as calibrated probability instead of a point estimate.
_SURE_VARIABLES = (
    "temperature_2m",
    "wind_speed_10m",
    "wind_gusts_10m",
    "pressure_msl",
    "dew_point_2m",
    "precipitation_probability",
)


def _summarize(forecast: list[dict], hours: int) -> dict:
    """Per-horizon summary of how much of the forecast is genuinely 'sure'."""
    bands = [(0, 6, "3h"), (6, 24, "12h"), (24, 72, "48h"), (72, hours, "J+")]
    horizons: dict[str, dict] = {}
    by_var: dict[str, list[float]] = {}
    for item in forecast:
        if item["variable"] in _SURE_VARIABLES:
            by_var.setdefault(item["variable"], []).append(item["confidence"])
            for lo, hi, label in bands:
                if lo < item["horizon_h"] <= hi or (lo == 0 and item["horizon_h"] <= hi):
                    horizons.setdefault(label, []).append(item["confidence"])
                    break
    summary = {"horizons": {}, "variables": {}}
    for label, confs in horizons.items():
        if confs:
            avg = sum(confs) / len(confs)
            summary["horizons"][label] = {
                "avg_confidence": round(avg, 3),
                "sure_share": round(sum(1 for c in confs if c >= 0.98) / len(confs), 3),
                "n": len(confs),
            }
    for var, confs in by_var.items():
        avg = sum(confs) / len(confs)
        summary["variables"][var] = {
            "avg_confidence": round(avg, 3),
            "sure": avg >= 0.98,
        }
    return summary


class WeatherService:
    """Orchestrates storage + fusion into a single forecast for a point."""

    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config
        self._stats: dict[tuple[str, str, str, float], ProviderStat] | None = None
        self._stats_fingerprint: tuple[str, int] | None = None

    def _residuals_fingerprint(self, since: datetime) -> tuple[str, int]:
        """Fingerprint of the residual pool used to invalidate the stats cache."""
        row = self.storage._conn.execute(
            "SELECT MAX(valid_at), COUNT(*) FROM residuals WHERE valid_at >= ?",
            (since.isoformat(),),
        ).fetchone()
        return (row[0] or "", int(row[1] or 0))

    def _load_stats(self) -> dict[tuple[str, str, str, float], ProviderStat]:
        """Materialize learned stats, spatially interpolated across cells.

        Cached until the residual pool changes (new residuals from a
        collect/calibrate cycle invalidate the fingerprint).
        """
        since = datetime.now(timezone.utc) - timedelta(
            days=self.config.residual_window_days
        )
        fp = self._residuals_fingerprint(since)
        if self._stats is not None and fp == self._stats_fingerprint:
            return self._stats
        stats: dict[tuple[str, str, str, float], ProviderStat] = {}
        residuals = self.storage.residuals_window(None, None, since)
        learned = learn_stats(residuals)
        for s in learned:
            stats[(s.provider, s.cell_key, s.variable, s.horizon_h)] = s
        cells = self.storage.get_cells()
        if cells:
            stats = spatial_stats(stats, cells)
        self._stats = stats
        self._stats_fingerprint = fp
        return stats

    def _ensure_cell_data(self, center: Cell) -> None:
        """Fetch model forecast + reanalysis for a cell on first demand.

        A cell with no stored data gets a 92-day analysis backfill so it is
        immediately calibrated: model analysis (past) + reanalysis (ground
        truth) are collected, residuals computed, and the cell joins the grid.
        """
        row = self.storage._conn.execute(
            "SELECT COUNT(*) FROM forecasts WHERE cell_key=?", (center.key,)
        ).fetchone()
        if row and row[0] > 0:
            return
        for m in FORECAST_MODELS + HIGH_RES_MODELS + ARCHIVE_MODELS:
            self.storage.upsert_provider(Provider(name=m, kind="model"))
        self.storage.upsert_cells([(center.key, center.lat, center.lon)])

        collector = OpenMeteoCollector(self.config)
        samples = collector.fetch_forecast(center, forecast_days=1, past_days=92)
        self.storage.insert_forecasts(samples)

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=92)
        obs = collector.fetch_historical(center, start, end)
        self.storage.insert_observations(obs)

        forecasts = self.storage.forecasts_in_window(
            [center.key], list(ALL_VARIABLES), start, end
        )
        observations = self.storage.observations_in_window(
            [center.key], list(OBSERVABLE_VARIABLES), start, end
        )
        residuals = compute_residuals(forecasts, observations)
        self.storage.insert_residuals(residuals)

    def forecast(self, lat: float, lon: float, hours: int = 48) -> dict:
        """Return the fused forecast for a point, bucketed hourly."""
        now = datetime.now(timezone.utc)
        center = cell_from_point(lat, lon, self.config)
        self._ensure_cell_data(center)
        # Model forecasts are fetched per-cell; the fusion targets the point's
        # own cell (global NWP models do not vary meaningfully over ~11 km,
        # and spatial stats interpolation already covers the bias). Nearby
        # cells exist for future station observations, not for model fusion.
        cells = [center.key]
        variables = list(ALL_VARIABLES)

        kinds = {p.name: p.kind for p in self.storage.get_providers()}
        stats = self._load_stats()

        # Model forecasts for the cells around the point.
        samples = self.storage.latest_forecasts(cells, variables)
        results = fuse(samples, stats, kinds, self.config, now)

        # Filter to the requested window and hour-bucket the output.
        until = now + timedelta(hours=hours)
        out = []
        for r in results:
            if r.valid_at < now or r.valid_at > until:
                continue
            tol = _VARIABLE_TOLERANCE.get(r.variable, 1.5)
            out.append(
                {
                    "variable": r.variable,
                    "valid_at": r.valid_at.isoformat(),
                    "value": round(r.consensus, 2),
                    "confidence": round(r.confidence, 3),
                    "calibrated": r.calibrated,
                    "dispersion": round(r.dispersion, 3),
                    "bias_corrected": r.bias_corrected,
                    "tolerance": tol,
                    # Honest range: value +/- tolerance at the stated confidence.
                    "low": round(r.consensus - tol, 2),
                    "high": round(r.consensus + tol, 2),
                    # "Sure" only when calibration is active AND confidence is high.
                    "sure": r.calibrated and r.confidence >= 0.98,
                    "horizon_h": round(
                        (r.valid_at - now).total_seconds() / 3600.0, 1
                    ),
                    "contributors": [p for p, _, _ in r.contributors],
                }
            )
        return {
            "location": {"lat": lat, "lon": lon},
            "cell": center.key,
            "generated_at": now.isoformat(),
            "forecast": out,
            "summary": _summarize(out, hours),
        }
