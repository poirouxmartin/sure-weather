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
    Observation,
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
            # Synthetic prior: deliberately < 3 samples so it NEVER counts
            # as calibrated (fusion requires samples >= 3). It only softens
            # the uncalibrated prior instead of faking learned confidence.
            out[key] = ProviderStat(
                provider=provider,
                cell_key=cell_key,
                variable=variable,
                horizon_h=bucket,
                samples=min(2, max(1, base.samples // 4)),
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
                if lo < item["horizon_h"] <= hi or (
                    lo == 0 and item["horizon_h"] <= hi
                ):
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
        self._stats_fingerprint: tuple[str, int, int] | None = None
        self._fc_cache: dict[tuple[str, int], tuple[float, dict]] = {}
        self._fc_lock = threading.Lock()
        self._refreshed_at: dict[str, float] = {}
        self._calibrated_at: dict[str, float] = {}
        self._refreshing: set[str] = set()
        self._calibrating: set[str] = set()
        self._sun_cache: dict[tuple[str, str], dict] = {}
        self._sun_lock = threading.Lock()
        self._backfilling: set[str] = set()
        self._state_lock = threading.Lock()
        # Serializes stats computation: the boot warm-up and a concurrent
        # request must never aggregate the residual window twice (the first
        # request would otherwise wait for BOTH computations).
        self._stats_lock = threading.Lock()

    # ---- forecast response cache ----

    def _forecast_cached(self, key: tuple[str, int]) -> dict | None:
        with self._fc_lock:
            hit = self._fc_cache.get(key)
            if hit is None:
                return None
            ts, data = hit
            if time.time() - ts > _FC_TTL_S:
                self._fc_cache.pop(key, None)
                return None
            return data

    def _forecast_store(self, key: tuple[str, int], data: dict) -> None:
        with self._fc_lock:
            if len(self._fc_cache) >= _FC_CACHE_MAX:
                oldest = min(self._fc_cache, key=lambda k: self._fc_cache[k][0])
                self._fc_cache.pop(oldest, None)
            self._fc_cache[key] = (time.time(), data)

    def _forecast_invalidate(self) -> None:
        """Drop memoized responses after new forecast data is ingested."""
        with self._fc_lock:
            self._fc_cache.clear()

    def _residuals_fingerprint(self, since: datetime) -> tuple[str, int, int]:
        """Fingerprint of the residual pool used to invalidate the stats cache.

        (MAX(valid_at), COUNT(*), SUM(rowid)) so an INSERT OR REPLACE that
        corrects a residual without changing MAX/COUNT still invalidates.
        Reads go through the storage lock (see Storage.tx / locked reads).
        """
        row = self.storage.locked_execute(
            "SELECT MAX(valid_at), COUNT(*), COALESCE(SUM(rowid),0) FROM residuals WHERE valid_at >= ?",
            (since.isoformat(),),
        ).fetchone()
        return (row[0] or "", int(row[1] or 0), int(row[2] or 0))

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
            # Persistent cache: a restart with an unchanged residual pool
            # skips the multi-second aggregation entirely.
            persisted = self.storage.load_stats_cache(str(fp))
            if persisted is not None:
                self._stats = persisted
                self._stats_fingerprint = fp
                return persisted
            stats: dict[tuple[str, str, str, float], ProviderStat] = {}
            now = datetime.now(timezone.utc)
            for (
                provider,
                cell_key,
                variable,
                horizon_h,
                samples,
                bias,
                mse,
            ) in self.storage.residual_bias_stats(since):
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
            try:
                self.storage.save_stats_cache(str(fp), stats)
            except Exception as exc:
                logger.warning("stats cache save failed: %s", exc)
            return stats

    def _ensure_cell_data(self, center: Cell, lat: float, lon: float) -> bool:
        """Make sure a cell can serve a forecast, blocking as little as possible.

        Returns True when the stored coverage reaches the requested window
        (the response is complete). Returns False when a background job is
        upgrading the zone (first visit, aged run): the caller marks the
        payload partial and the client re-fetches shortly after.
        - brand-new cell: one fast single-model 7-day request, then the full
          backfill (all models, 92-day calibration, stations) in background;
        - known cell: everything (run refresh, calibration) in background.
        """
        now = datetime.now(timezone.utc)
        # Coverage of the *latest* run (not any stale long-range row): an old
        # analysis row with a far valid_at must not pass for fresh coverage.
        reach = self.storage.locked_fetchone(
            "SELECT MAX(valid_at) FROM forecasts WHERE cell_key=? AND issued_at >= ?",
            (center.key, (now - timedelta(hours=24)).isoformat()),
        )
        has_data = bool(reach and reach[0])
        reach_ok = has_data and _parse_dt(reach[0]) >= now + timedelta(hours=24)

        if not has_data:
            # Brand-new cell: a FAST single-model 7-day fetch (one request,
            # ~1-2s) gives full hourly coverage immediately; the complete
            # multi-model + 92-day calibration backfill runs in background
            # and upgrades the zone afterwards.
            self.storage.upsert_cells([(center.key, center.lat, center.lon)])
            fast_ok = False
            try:
                fast = OpenMeteoCollector(self.config).fetch_forecast(
                    center,
                    models=["gfs_seamless"],
                    forecast_days=7,
                    include_high_res=False,
                )
                self.storage.insert_forecasts(fast)
                self._forecast_invalidate()
                fast_ok = bool(fast)
            except Exception as exc:
                logger.warning("fast first fetch failed for %s: %s", center.key, exc)
            self._schedule_full_backfill(center, lat, lon)
            # Only mark partial when the fast fetch actually failed: a filled
            # 7-day run serves the full window immediately.
            return fast_ok

        # Known cell: refresh local stations cheaply so the "now" consensus
        # stays honest, but skip when we just ingested this zone minutes ago.
        fresh = self.storage.locked_fetchone(
            "SELECT MAX(time) FROM observations WHERE cell_key=? "
            "AND provider LIKE 'metar_%'",
            (center.key,),
        )
        if not (fresh and fresh[0]):
            self._ingest_nearby_stations(lat, lon)
        elif (datetime.now(timezone.utc) - _parse_dt(fresh[0])) >= timedelta(hours=1):
            self._ingest_nearby_stations(lat, lon)
        # Run maintenance in the BACKGROUND (7-day refresh, calibration):
        # the user is served from what is already stored, stale-but-covering.
        self._schedule_refresh(center)
        self._schedule_calibration(center)
        return reach_ok

    def _schedule_refresh(self, center: Cell) -> None:
        """Run the 7-day refresh in background: serve stale meanwhile."""
        with self._state_lock:
            last = self._refreshed_at.get(center.key)
            if center.key in self._refreshing:
                return
            if last is not None and (time.time() - last) < 3600:
                return
            self._refreshing.add(center.key)
        threading.Thread(
            target=self._refresh_model_run_if_stale, args=(center,), daemon=True
        ).start()

    def _schedule_full_backfill(self, center: Cell, lat: float, lon: float) -> None:
        """Run the complete zone bootstrap in background (guarded)."""
        with self._state_lock:
            if center.key in self._backfilling:
                return
            self._backfilling.add(center.key)
        threading.Thread(
            target=self._full_backfill, args=(center, lat, lon), daemon=True
        ).start()

    def _full_backfill(self, center: Cell, lat: float, lon: float) -> None:
        """Complete zone bootstrap, off the request path.

        Upgrades the fast single-model fetch to the full multi-model run,
        then the 92-day analysis + reanalysis + residuals (calibration) and
        the local METAR stations.
        """
        try:
            collector = OpenMeteoCollector(self.config)
            for m in FORECAST_MODELS + HIGH_RES_MODELS + ARCHIVE_MODELS:
                self.storage.upsert_provider(Provider(name=m, kind="model"))
            try:
                full = collector.fetch_forecast(center, forecast_days=7)
                self.storage.insert_forecasts(full)
                self._forecast_invalidate()
            except Exception as exc:
                logger.warning("full run fetch failed for %s: %s", center.key, exc)

            end = datetime.now(timezone.utc)
            start = end - timedelta(days=92)
            try:
                obs = collector.fetch_historical(center, start, end)
                self.storage.insert_observations(obs)
            except Exception as exc:
                logger.warning("archive fetch failed for %s: %s", center.key, exc)

            try:
                forecasts = self.storage.forecasts_in_window(
                    [center.key], list(ALL_VARIABLES), start, end
                )
                observations = self.storage.observations_in_window(
                    [center.key], list(OBSERVABLE_VARIABLES), start, end
                )
                kinds = {pr.name: pr.kind for pr in self.storage.get_providers()}
                residuals = compute_residuals(forecasts, observations, kinds)
                self.storage.insert_residuals(residuals)
            except Exception as exc:
                logger.warning("residual matching failed for %s: %s", center.key, exc)

            self._ingest_nearby_stations(lat, lon)
            self._schedule_calibration(center)
            logger.info("full backfill done for %s", center.key)
        finally:
            with self._state_lock:
                self._backfilling.discard(center.key)

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
        try:
            now = datetime.now(timezone.utc)
            # A healthy 7-day run reaches ~5+ days ahead; anything shorter (an
            # old backfill, a truncated run) must be refreshed even though it
            # still technically covers the next few hours.
            future = self.storage.locked_fetchone(
                "SELECT COUNT(*) FROM forecasts WHERE cell_key=? AND valid_at > ?",
                (center.key, (now + timedelta(hours=3)).isoformat()),
            )
            has_future = bool(future and future[0] > 0)
            reach = self.storage.locked_fetchone(
                "SELECT MAX(valid_at) FROM forecasts WHERE cell_key=?",
                (center.key,),
            )
            reach_ok = bool(reach and reach[0]) and _parse_dt(
                reach[0]
            ) >= now + timedelta(hours=120)
            row = self.storage.locked_fetchone(
                "SELECT MAX(issued_at) FROM forecasts WHERE cell_key=?",
                (center.key,),
            )
            issued = _parse_dt(row[0]) if row and row[0] else None
            run_fresh = issued is not None and (now - issued) <= timedelta(hours=12)
            if has_future and reach_ok and run_fresh:
                return
            try:
                collector = OpenMeteoCollector(self.config)
                samples = collector.fetch_forecast(center, forecast_days=7)
                self.storage.insert_forecasts(samples)
                with self._state_lock:
                    self._refreshed_at[center.key] = time.time()
                self._forecast_invalidate()
            except Exception as exc:
                # Backoff 10 min on failure so every request doesn't respawn
                # a thread hammering a failing upstream.
                with self._state_lock:
                    self._refreshed_at[center.key] = time.time() - 3000
                logger.warning("run refresh failed for %s: %s", center.key, exc)
                return
        finally:
            with self._state_lock:
                self._refreshing.discard(center.key)

    def _schedule_calibration(self, center: Cell) -> None:
        """Spawn the hourly calibration cycle in a daemon thread.

        The timestamp is claimed before spawning so concurrent requests for
        the same zone start at most one cycle per hour.
        """
        with self._state_lock:
            now = time.time()
            last = self._calibrated_at.get(center.key)
            if center.key in self._calibrating:
                return
            if last is not None and (now - last) < 3600:
                return
            self._calibrated_at[center.key] = now
            self._calibrating.add(center.key)
        threading.Thread(
            target=self._calibrate_cell, args=(center,), daemon=True
        ).start()

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
            self.storage.insert_forecasts([s for s in analysis if s.valid_at <= end])

            # Loop 2 — archive catch-up, only when yesterday is missing.
            yesterday = (end - timedelta(days=1)).date().isoformat()
            have = self.storage.locked_fetchone(
                "SELECT MAX(time) FROM observations WHERE cell_key=? "
                "AND provider IN ('era5_seamless','era5_land','cerra')",
                (center.key,),
            )
            latest_obs = (
                _parse_dt(have[0]).date().isoformat() if have and have[0] else ""
            )
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
            logger.info("calibration %s: +%d residuals", center.key, len(residuals))
        except Exception as exc:
            # Calibration is a background concern: a failed cycle must never
            # break the forecast request; retry in ~10 minutes.
            with self._state_lock:
                self._calibrated_at[center.key] = time.time() - 3000
            logger.warning("calibration failed for %s: %s", center.key, exc)
        finally:
            with self._state_lock:
                self._calibrating.discard(center.key)

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
            for c in iter_cells_nearby(
                lat, lon, self.config.obs_search_radius_km, self.config
            )
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
            # Honest dating: issued_at stays the real report time so the
            # horizon bucket reflects the observation's age; valid_at is the
            # hourly bucket it constrains (models emit hourly timestamps).
            valid = now.replace(minute=0, second=0, microsecond=0)
            if now.minute > 0 or now.second > 0 or now.microsecond > 0:
                valid = valid + timedelta(hours=1)
            samples.append(
                ForecastSample(
                    provider=o.provider,
                    cell_key=o.cell_key,
                    variable=o.variable,
                    issued_at=o.time,
                    valid_at=valid,
                    value=o.value,
                )
            )
        return samples

    def forecast(self, lat: float, lon: float, hours: int = 48) -> dict:
        """Return the fused forecast for a point, bucketed hourly."""
        now = datetime.now(timezone.utc)
        center = cell_from_point(lat, lon, self.config)
        # Cache by cell key (not round(lat,2)): two nearby points in
        # different 0.1° cells must never share a response.
        key = (center.key, hours)
        cached = self._forecast_cached(key)
        if cached is not None:
            return cached
        sufficient = self._ensure_cell_data(center, lat, lon)
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
        # If a newly added variable (e.g. uv_index) is missing, refresh in
        # background so the next request carries it — keeps the map detailed
        # per neighbourhood immediately.
        present = {s.variable for s in samples}
        if any(v not in present for v in variables):
            self._schedule_refresh(center)
        # Live station observations near the point join the consensus for the
        # current hours: the "now" forecast leans on real local measurements.
        samples.extend(self._nearby_station_samples(lat, lon, now - timedelta(hours=6)))
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
                    "horizon_h": round((r.valid_at - now).total_seconds() / 3600.0, 1),
                    "contributors": [p for p, _, _ in r.contributors],
                }
            )
        # Detailed breakdown for the nearest hour (transparency): raw values per
        # provider, bias, weight, and the weighted calibrated mean (= consensus).
        breakdown = []
        breakdown_valid_at = None
        if results:
            var0 = "temperature_2m"
            # pick the earliest hour that actually has temperature_2m data
            by_time_var = {r.valid_at: r for r in results if r.variable == var0}
            for vat in sorted(by_time_var):
                cands = [s for s in samples if s.variable == var0 and s.valid_at == vat]
                if not cands:
                    continue
                breakdown_valid_at = vat
                from collections import defaultdict as _dd

                from .fusion import base_weight as _base_weight

                # Group per provider, keeping each sample's own bucket so the
                # displayed weights match the fusion (inverse-variance +
                # staleness), not a parallel formula.
                by_prov: dict[str, list] = _dd(list)
                for s in cands:
                    by_prov[s.provider].append(s)
                for prov, ss in by_prov.items():
                    raw = sum(s.value for s in ss) / len(ss)
                    # Representative stat: the sample closest to the hour.
                    rep = min(
                        ss,
                        key=lambda s: abs((s.valid_at - s.issued_at).total_seconds()),
                    )
                    bucket = horizon_bucket(rep.valid_at, rep.issued_at)
                    stat = stats.get((prov, rep.cell_key, var0, bucket))
                    bias = stat.bias if stat and stat.samples >= 3 else 0.0
                    w = _base_weight(stat, kinds.get(prov, "model"), bucket, now)
                    breakdown.append(
                        {
                            "provider": prov,
                            "raw": round(raw, 2),
                            "bias": round(bias, 2),
                            "corr": round(raw - bias, 2),
                            "weight": round(w, 3),
                        }
                    )
                breakdown.sort(key=lambda x: x["weight"], reverse=True)
                tot = sum(x["weight"] for x in breakdown) or 1
                for x in breakdown:
                    x["share"] = round(x["weight"] / tot * 100, 1)
                break
            if breakdown_valid_at is None and results:
                breakdown_valid_at = min(results, key=lambda r: r.valid_at).valid_at

        payload = {
            "location": {"lat": lat, "lon": lon},
            "cell": center.key,
            "generated_at": now.isoformat(),
            "forecast": out,
            "summary": _summarize(out, hours),
            "partial": (not sufficient) or (not out),
            "breakdown": breakdown,
            "breakdown_valid_at": breakdown_valid_at.isoformat()
            if breakdown_valid_at
            else None,
        }
        # Real solar times for the night bands / day-night icons: cached for
        # the process lifetime (they shift by minutes between model runs).
        # Contract: payload ALWAYS carries a "sun" key (possibly {"daily":
        # {} }) so the client never branches on its absence.
        try:
            day_key = now.date().isoformat()
            with self._sun_lock:
                sun = self._sun_cache.get((center.key, day_key))
            if sun is None:
                sun = OpenMeteoCollector(self.config).fetch_sun(center)
                with self._sun_lock:
                    self._sun_cache[(center.key, day_key)] = sun
            payload["sun"] = {"daily": sun}
        except Exception as exc:
            payload["sun"] = {"daily": {}}
            logger.warning("sun times unavailable for %s: %s", center.key, exc)
        if out:
            self._forecast_store(key, payload)
        return payload
