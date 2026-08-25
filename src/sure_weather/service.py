from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from .calibration import compute_residuals
from .collectors import (
    ARCHIVE_MODELS,
    FORECAST_MODELS,
    HIGH_RES_MODELS,
    OpenMeteoCollector,
)
from .config import Config
from .fusion import (
    ProviderStat,
    _VARIABLE_BOUNDS,
    _VARIABLE_TOLERANCE,
    fuse,
    horizon_bucket,
    spatial_stats,
)
from .grid import cell_from_point, iter_cells_nearby
from .models import (
    ALL_VARIABLES,
    OBSERVABLE_VARIABLES,
    Cell,
    ForecastSample,
    Provider,
)
from .storage import Storage, _parse_dt

logger = logging.getLogger(__name__)


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

# Forecast responses are memoized for a few minutes: weather evolves slowly,
# and repeat searches of the same zone (favorites, range switches, map
# clicks nearby) must feel instant instead of re-running the fusion.
_FC_TTL_S = 300.0
_FC_CACHE_MAX = 64


def _clamped_range(variable: str, consensus: float, tol: float) -> tuple[float, float]:
    """Honest range (consensus +/- tolerance) clipped to physical bounds.

    A humidity of "73-113%" or a wind of "-0.5-3.5 m/s" would read as a bug;
    the tolerance interval is clipped to the variable's valid domain.
    """
    lo_b, hi_b = _VARIABLE_BOUNDS.get(variable, (None, None))
    low, high = consensus - tol, consensus + tol
    if lo_b is not None:
        low = max(low, lo_b)
    if hi_b is not None:
        high = min(high, hi_b)
    return low, high


# Horizon prior extrapolation: the archive that grounds long-horizon
# residuals lags ~5 days, so 12h/48h/120h buckets fill over days. Until a
# bucket has real residuals, extrapolate the nearest learned bucket with a
# documented error-growth factor — conservative by construction (rmse grows,
# sample weight shrinks), it yields cautious calibrated confidence instead
# of an uncalibrated guess for the first days of a cell's life.
_HORIZON_INFLATION = {3.0: 1.0, 12.0: 1.35, 48.0: 1.8, 120.0: 2.3}


def _extrapolate_horizon_priors(
    stats: dict[tuple[str, str, str, float], ProviderStat],
) -> dict[tuple[str, str, str, float], ProviderStat]:
    by_series: dict[tuple[str, str, str], list[ProviderStat]] = {}
    for key, s in stats.items():
        if s.samples >= 3:
            by_series.setdefault((key[0], key[1], key[2]), []).append(s)
    out = dict(stats)
    for (provider, cell_key, variable), learned in by_series.items():
        learned.sort(key=lambda s: s.horizon_h)
        base = learned[0]
        for bucket, factor in _HORIZON_INFLATION.items():
            key = (provider, cell_key, variable, bucket)
            if key in out:
                continue
            out[key] = ProviderStat(
                provider=provider,
                cell_key=cell_key,
                variable=variable,
                horizon_h=bucket,
                samples=max(3, base.samples // 4),
                bias=base.bias,
                rmse=base.rmse * factor,
                updated_at=base.updated_at,
            )
    return out


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
        self._fc_cache: dict[tuple[float, float, int], tuple[float, dict]] = {}
        self._fc_lock = threading.Lock()
        self._refreshed_at: dict[str, float] = {}
        self._calibrated_at: dict[str, float] = {}
        # Serializes stats computation: the boot warm-up and a concurrent
        # request must never aggregate the residual window twice (the first
        # request would otherwise wait for BOTH computations).
        self._stats_lock = threading.Lock()

    # ---- forecast response cache ----

    def _forecast_cached(self, key: tuple[float, float, int]) -> dict | None:
        with self._fc_lock:
            hit = self._fc_cache.get(key)
            if hit is None:
                return None
            ts, data = hit
            if time.time() - ts > _FC_TTL_S:
                self._fc_cache.pop(key, None)
                return None
            return data

    def _forecast_store(self, key: tuple[float, float, int], data: dict) -> None:
        with self._fc_lock:
            if len(self._fc_cache) >= _FC_CACHE_MAX:
                oldest = min(self._fc_cache, key=lambda k: self._fc_cache[k][0])
                self._fc_cache.pop(oldest, None)
            self._fc_cache[key] = (time.time(), data)

    def _forecast_invalidate(self) -> None:
        """Drop memoized responses after new forecast data is ingested."""
        with self._fc_lock:
            self._fc_cache.clear()

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
        collect/calibrate cycle invalidate the fingerprint). The aggregation
        itself runs inside SQLite (GROUP BY over the residual window) so a
        cold start stays fast even with millions of stored residuals.
        """
        since = datetime.now(timezone.utc) - timedelta(
            days=self.config.residual_window_days
        )
        fp = self._residuals_fingerprint(since)
        if self._stats is not None and fp == self._stats_fingerprint:
            return self._stats
        with self._stats_lock:
            # Double-check: the boot warm-up may have finished while this
            # request waited on the lock.
            if self._stats is not None and fp == self._stats_fingerprint:
                return self._stats
            stats: dict[tuple[str, str, str, float], ProviderStat] = {}
            now = datetime.now(timezone.utc)
            for provider, cell_key, variable, horizon_h, samples, bias, mse in (
                self.storage.residual_bias_stats(since)
            ):
                stats[(provider, cell_key, variable, horizon_h)] = ProviderStat(
                    provider=provider,
                    cell_key=cell_key,
                    variable=variable,
                    horizon_h=horizon_h,
                    samples=samples,
                    bias=float(bias),
                    rmse=math.sqrt(mse) if mse is not None else 0.0,
                    updated_at=now,
                )
            cells = self.storage.get_cells()
            if cells:
                stats = spatial_stats(stats, cells)
            stats = _extrapolate_horizon_priors(stats)
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
                if (datetime.now(timezone.utc) - _parse_dt(fresh[0])) >= timedelta(
                    hours=1
                ):
                    self._ingest_nearby_stations(lat, lon)
            # Refresh the model run if the stored one no longer reaches into
            # the future (e.g. a backfill that only fetched 1 forecast day, or
            # a run that simply aged out). Otherwise the window filter in
            # forecast() drops every result and the zone shows "no data".
            self._refresh_model_run_if_stale(center)
            # Keep calibration alive in the background: the first cycle for a
            # cell is heavy (archive fetch + residual matching over days), it
            # must never sit inside the user's request.
            self._schedule_calibration(center)
            return
        for m in FORECAST_MODELS + HIGH_RES_MODELS + ARCHIVE_MODELS:
            self.storage.upsert_provider(Provider(name=m, kind="model"))
        self.storage.upsert_cells([(center.key, center.lat, center.lon)])

        collector = OpenMeteoCollector(self.config)

        # The 92-day model forecast and the reanalysis archive are independent
        # heavy HTTP requests: fetch them concurrently to cut the first-visit
        # latency of a brand-new zone roughly in half.
        from concurrent.futures import ThreadPoolExecutor

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=92)

        def _fetch_forecast():
            return collector.fetch_forecast(center, forecast_days=1, past_days=92)

        def _fetch_historical():
            return collector.fetch_historical(center, start, end)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_fc = pool.submit(_fetch_forecast)
            f_obs = pool.submit(_fetch_historical)
            samples = f_fc.result()
            obs = f_obs.result()

        self.storage.insert_forecasts(samples)
        self.storage.insert_observations(obs)
        self._forecast_invalidate()

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

    def _refresh_model_run_if_stale(self, center: Cell) -> None:
        """Re-collect a fast 7-day forecast when the stored run is stale.

        Two staleness modes must trigger a refresh:
        - the run no longer reaches into the future (old backfill with
          forecast_days=1): the window filter leaves the zone with no data;
        - the run itself aged: NWP models issue every ~6h, and a multi-day-old
          run pushes every near-hour forecast into the 120h horizon bucket,
          where no calibration applies (confidence collapses, everything reads
          "non cal."). Fresh runs keep near hours in the calibrated 3h/12h
          buckets.
        A cell is refreshed at most once per hour to spare the upstream API.
        """
        now = datetime.now(timezone.utc)
        # A healthy 7-day run reaches ~5+ days ahead; anything shorter (an
        # old backfill, a truncated run) must be refreshed even though it
        # still technically covers the next few hours.
        future = self.storage._conn.execute(
            "SELECT COUNT(*) FROM forecasts WHERE cell_key=? AND valid_at > ?",
            (center.key, (now + timedelta(hours=3)).isoformat()),
        ).fetchone()
        has_future = bool(future and future[0] > 0)
        reach = self.storage._conn.execute(
            "SELECT MAX(valid_at) FROM forecasts WHERE cell_key=?",
            (center.key,),
        ).fetchone()
        reach_ok = bool(reach and reach[0]) and _parse_dt(reach[0]) >= now + timedelta(
            hours=120
        )
        row = self.storage._conn.execute(
            "SELECT MAX(issued_at) FROM forecasts WHERE cell_key=?",
            (center.key,),
        ).fetchone()
        issued = _parse_dt(row[0]) if row and row[0] else None
        run_fresh = issued is not None and (now - issued) <= timedelta(hours=12)
        if has_future and reach_ok and run_fresh:
            return
        last = self._refreshed_at.get(center.key)
        if last is not None and (time.time() - last) < 3600:
            return
        try:
            collector = OpenMeteoCollector(self.config)
            samples = collector.fetch_forecast(center, forecast_days=7)
            self.storage.insert_forecasts(samples)
            self._refreshed_at[center.key] = time.time()
            self._forecast_invalidate()
        except Exception as exc:
            # Best-effort: a stale run is better than an empty forecast only
            # marginally; never break the request over a refresh failure.
            logger.warning("run refresh failed for %s: %s", center.key, exc)
            return

    def _schedule_calibration(self, center: Cell) -> None:
        """Spawn the hourly calibration cycle in a daemon thread.

        The timestamp is claimed before spawning so concurrent requests for
        the same zone start at most one cycle per hour.
        """
        now = time.time()
        last = self._calibrated_at.get(center.key)
        if last is not None and (now - last) < 3600:
            return
        self._calibrated_at[center.key] = now
        threading.Thread(target=self._calibrate_cell, args=(center,), daemon=True).start()

    def _calibrate_cell(self, center: Cell) -> None:
        """Refresh residuals for a cell so its calibration stays live.

        The backfill computes residuals once; without this loop the learned
        bias/rmse would freeze forever and horizons beyond the analysis bucket
        would never gain stats. Two catch-up loops per cell, at most hourly:
        - recent model analysis (past 2 days) lands in the forecasts table and
          is matched against stored observations (stations + reanalysis),
        - the archive (D-6..D-1, ~5-day publication latency) is fetched when
          missing; matching it against the stored 7-day runs is what turns
          past 12h/48h/120h forecasts into residuals, i.e. what calibrates
          the longer horizons over time.
        """
        try:
            collector = OpenMeteoCollector(self.config)
            end = datetime.now(timezone.utc)

            # Loop 1 — recent analysis (cheap single request). Only past
            # hours are stored: this fetch must never become the latest
            # issued run, otherwise its 1-day coverage would shadow the
            # 7-day run in latest_forecasts and truncate every forecast.
            analysis = collector.fetch_forecast(
                center, forecast_days=1, past_days=2, include_high_res=False
            )
            self.storage.insert_forecasts(
                [s for s in analysis if s.valid_at <= end]
            )

            # Loop 2 — archive catch-up, only when yesterday is missing.
            yesterday = (end - timedelta(days=1)).date().isoformat()
            have = self.storage._conn.execute(
                "SELECT MAX(time) FROM observations WHERE cell_key=? "
                "AND provider IN ('era5_seamless','era5_land','cerra')",
                (center.key,),
            ).fetchone()
            latest_obs = _parse_dt(have[0]).date().isoformat() if have and have[0] else ""
            if latest_obs < yesterday:
                obs = collector.fetch_historical(
                    center, end - timedelta(days=8), end - timedelta(days=1)
                )
                self.storage.insert_observations(obs)

            # Match everything stored in the window against the observations.
            kinds = {p.name: p.kind for p in self.storage.get_providers()}
            start = end - timedelta(days=9)
            forecasts = self.storage.forecasts_in_window(
                [center.key], list(ALL_VARIABLES), start, end
            )
            observations = self.storage.observations_in_window(
                [center.key], list(OBSERVABLE_VARIABLES), start, end
            )
            residuals = compute_residuals(forecasts, observations, kinds)
            self.storage.insert_residuals(residuals)
            # New residuals shift the fingerprint: drop cached responses so
            # the next request fuses with fresh calibration.
            self._forecast_invalidate()
            logger.info(
                "calibration %s: +%d residuals", center.key, len(residuals)
            )
        except Exception as exc:
            # Calibration is a background concern: a failed cycle must never
            # break the forecast request; retry in ~10 minutes.
            self._calibrated_at[center.key] = time.time() - 3000
            logger.warning(
                "calibration failed for %s: %s", center.key, exc
            )

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
        except Exception as exc:
            # Station discovery is best-effort: a network hiccup or an empty
            # area must never break a forecast request.
            logger.warning("station ingest failed near %s,%s: %s", lat, lon, exc)
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
        cells_near = [
            c.key
            for c in iter_cells_nearby(lat, lon, self.config.obs_search_radius_km, self.config)
        ]
        if not cells_near:
            return []
        obs = self.storage.observations_in_window(
            cells_near,
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
        key = (round(lat, 2), round(lon, 2), hours)
        cached = self._forecast_cached(key)
        if cached is not None:
            return cached
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
        # Fuse only the requested window: latest_forecasts returns every
        # valid time of the stored run (~7 days), while the response covers
        # `hours`. Filtering first cuts the fusion work by that ratio.
        until = now + timedelta(hours=hours)
        window_lo = now - timedelta(hours=1)
        samples = [s for s in samples if window_lo <= s.valid_at <= until]
        results = fuse(samples, stats, kinds, self.config, now)

        out = []
        for r in results:
            if r.valid_at < now or r.valid_at > until:
                continue
            tol = _VARIABLE_TOLERANCE.get(r.variable, 1.5)
            low, high = _clamped_range(r.variable, r.consensus, tol)
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
                    "low": round(low, 2),
                    "high": round(high, 2),
                    # "Sure" only when calibration is active AND confidence is high.
                    "sure": r.calibrated and r.confidence >= 0.98,
                    "horizon_h": round(
                        (r.valid_at - now).total_seconds() / 3600.0, 1
                    ),
                    "contributors": [p for p, _, _ in r.contributors],
                }
            )
        payload = {
            "location": {"lat": lat, "lon": lon},
            "cell": center.key,
            "generated_at": now.isoformat(),
            "forecast": out,
            "summary": _summarize(out, hours),
        }
        if out:
            self._forecast_store(key, payload)
        return payload
