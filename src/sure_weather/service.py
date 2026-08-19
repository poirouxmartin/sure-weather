from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .calibration import compute_residuals, learn_stats
from .collectors import ARCHIVE_MODELS, FORECAST_MODELS, OpenMeteoCollector
from .config import Config
from .fusion import ProviderStat, fuse, horizon_bucket, spatial_stats
from .grid import cell_from_point, iter_cells_nearby
from .models import ALL_VARIABLES, OBSERVABLE_VARIABLES, Cell, Provider
from .storage import Storage


class WeatherService:
    """Orchestrates storage + fusion into a single forecast for a point."""

    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    def _load_stats(self) -> dict[tuple[str, str, str, float], ProviderStat]:
        """Materialize learned stats, spatially interpolated across cells."""
        stats: dict[tuple[str, str, str, float], ProviderStat] = {}
        # Stats are recomputed from residuals each time calibration runs; here we
        since = datetime.now(timezone.utc) - timedelta(
            days=self.config.residual_window_days
        )
        residuals = self.storage.residuals_window(None, None, since)
        learned = learn_stats(residuals)
        for s in learned:
            stats[(s.provider, s.cell_key, s.variable, s.horizon_h)] = s
        cells = self.storage.get_cells()
        if cells:
            stats = spatial_stats(stats, cells)
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
        for m in FORECAST_MODELS + ARCHIVE_MODELS:
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
        cells = [
            c.key
            for c in iter_cells_nearby(
                lat, lon, self.config.obs_search_radius_km, self.config
            )
        ]
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
            out.append(
                {
                    "variable": r.variable,
                    "valid_at": r.valid_at.isoformat(),
                    "value": round(r.consensus, 2),
                    "confidence": round(r.confidence, 3),
                    "calibrated": r.calibrated,
                    "dispersion": round(r.dispersion, 3),
                    "bias_corrected": r.bias_corrected,
                    "contributors": [p for p, _, _ in r.contributors],
                }
            )
        return {
            "location": {"lat": lat, "lon": lon},
            "cell": center.key,
            "generated_at": now.isoformat(),
            "forecast": out,
        }
