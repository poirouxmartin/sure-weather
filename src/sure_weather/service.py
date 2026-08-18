from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .calibration import learn_stats
from .config import Config
from .fusion import ProviderStat, fuse, horizon_bucket
from .grid import cell_from_point, iter_cells_nearby
from .models import ALL_VARIABLES, Observation, Variable
from .storage import Storage


class WeatherService:
    """Orchestrates storage + fusion into a single forecast for a point."""

    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    def _load_stats(self) -> dict[tuple[str, str, str, float], ProviderStat]:
        """Materialize learned stats into a lookup dict."""
        stats: dict[tuple[str, str, str, float], ProviderStat] = {}
        # Stats are recomputed from residuals each time calibration runs; here we
        since = datetime.now(timezone.utc) - timedelta(
            days=self.config.residual_window_days
        )
        residuals = self.storage.residuals_window(None, None, since)
        for s in learn_stats(residuals):
            stats[(s.provider, s.cell_key, s.variable, s.horizon_h)] = s
        return stats

    def forecast(self, lat: float, lon: float, hours: int = 48) -> dict:
        """Return the fused forecast for a point, bucketed hourly."""
        now = datetime.now(timezone.utc)
        center = cell_from_point(lat, lon, self.config)
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
