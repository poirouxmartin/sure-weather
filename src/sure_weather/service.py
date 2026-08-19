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
from .models import (
    ALL_VARIABLES,
    OBSERVABLE_VARIABLES,
    Cell,
    ForecastSample,
    Provider,
)
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

    def _ensure_cell_data(self, center: Cell, lat: float, lon: float) -> None:
        """Fetch model forecast + reanalysis for a cell on first demand.

        A cell with no stored data gets a 92-day analysis backfill so it is
        immediately calibrated: model analysis (past) + reanalysis (ground
        truth) are collected, residuals computed, and the cell joins the grid.
        Local METAR stations around the queried point are ingested too.
        """
        row = self.storage._conn.execute(
            "SELECT COUNT(*) FROM forecasts WHERE cell_key=?", (center.key,)
        ).fetchone()
        if row and row[0] > 0:
            # Cell is known: refresh local stations cheaply so the "now"
            # consensus stays honest on repeat searches of the same zone,
            # but skip when we just ingested this zone minutes ago.
            fresh = self.storage._conn.execute(
                "SELECT MAX(time) FROM observations WHERE cell_key=? "
                "AND provider LIKE 'metar_%'",
                (center.key,),
            ).fetchone()
            if not (fresh and fresh[0]):
                self._ingest_nearby_stations(lat, lon)
            else:
                from .storage import _parse_dt

                if (datetime.now(timezone.utc) - _parse_dt(fresh[0])) >= timedelta(
                    hours=1
                ):
                    self._ingest_nearby_stations(lat, lon)
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
        kinds = {p.name: p.kind for p in self.storage.get_providers()}
        residuals = compute_residuals(forecasts, observations, kinds)
        self.storage.insert_residuals(residuals)

        # Discover and ingest local METAR stations around the cell: the "as
        # many sources as possible in the corner" promise. Their observations
        # feed the live consensus and the station-first residual matching.
        self._ingest_nearby_stations(lat, lon)

    def _ingest_nearby_stations(self, lat: float, lon: float) -> None:
        """Fetch recent METAR reports near (lat, lon) and store observations."""
        try:
            from .collectors.metar import MetarCollector

            collector = MetarCollector(self.config)
            reports = collector.fetch_observations(
                lat, lon, radius_km=self.config.obs_search_radius_km, hours=6
            )
            if not reports:
                return
            for p in collector.providers(reports):
                self.storage.upsert_provider(p)
            observations = []
            for r in reports:
                observations.extend(
                    collector.to_observations(r, self.config.cell_resolution)
                )
            self.storage.insert_observations(observations)
        except Exception:
            # Station discovery is best-effort: a network hiccup or an empty
            # area must never break a forecast request.
            return

    def _nearby_station_samples(
        self, lat: float, lon: float, since: datetime
    ) -> list[ForecastSample]:
        """Recent station observations near the point, as live 'analysis' samples.

        A station report at time T becomes a sample with issued_at == valid_at
        == T. Because its horizon bucket is 3h (<=6h), the fusion at the current
        hour treats it as a strong, honest measurement alongside the model
        forecasts: stations have a higher base weight and lower prior noise than
        global models, so the "now" consensus leans toward the real thermometer.
        """
        cells_near = [c for c in self.storage.get_cells()]
        if not cells_near:
            return []
        from .grid import haversine_km

        # Only cells within the station search radius around the point.
        near = [
            c
            for c in cells_near
            if haversine_km(lat, lon, c.lat, c.lon) <= self.config.obs_search_radius_km
        ]
        if not near:
            return []
        obs = self.storage.observations_in_window(
            [c.key for c in near],
            list(OBSERVABLE_VARIABLES),
            since,
        )
        now = datetime.now(timezone.utc)
        samples: list[ForecastSample] = []
        # Keep the most recent report per station+variable, and only recent
        # ones (within the station recency window). Older reports would just
        # re-verify the past, not constrain the "now".
        latest: dict[tuple[str, str], Observation] = {}
        for o in obs:
            if o.provider in FORECAST_MODELS or o.provider in ARCHIVE_MODELS:
                continue  # only stations carry live truth into the consensus
            key = (o.provider, o.variable)
            if key not in latest or o.time > latest[key].time:
                latest[key] = o
        for o in latest.values():
            if (now - o.time) > timedelta(hours=self.config.obs_recency_halflife_h * 3):
                continue
            # The report is treated as a measurement at the current full hour
            # (models emit hourly timestamps starting at the next full hour),
            # so it joins the same consensus bucket as the model forecasts.
            valid = now.replace(minute=0, second=0, microsecond=0)
            if now.minute > 0 or now.second > 0 or now.microsecond > 0:
                valid = valid + timedelta(hours=1)
            samples.append(
                ForecastSample(
                    provider=o.provider,
                    cell_key=o.cell_key,
                    variable=o.variable,
                    issued_at=valid,
                    valid_at=valid,
                    value=o.value,
                )
            )
        return samples

    def forecast(self, lat: float, lon: float, hours: int = 48) -> dict:
        """Return the fused forecast for a point, bucketed hourly."""
        now = datetime.now(timezone.utc)
        center = cell_from_point(lat, lon, self.config)
        self._ensure_cell_data(center, lat, lon)
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
        # Live station observations near the point join the consensus for the
        # current hours: the "now" forecast leans on real local measurements.
        samples.extend(
            self._nearby_station_samples(
                lat, lon, now - timedelta(hours=6)
            )
        )
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
