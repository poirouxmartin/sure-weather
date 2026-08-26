from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..config import Config
from ..models import Cell, ForecastSample, Observation, Variable

# Open-Meteo parameter names map 1:1 to our Variable constants for these.
_FORECAST_VARS = [
    Variable.TEMPERATURE_2M,
    Variable.PRECIPITATION,
    Variable.PRECIPITATION_PROBABILITY,
    Variable.CLOUD_COVER,
    Variable.WIND_SPEED_10M,
    Variable.WIND_GUSTS_10M,
    Variable.WIND_DIRECTION_10M,
    Variable.RELATIVE_HUMIDITY_2M,
    Variable.PRESSURE_MSL,
    Variable.DEW_POINT_2M,
    Variable.VISIBILITY,
]

# NWP models served by Open-Meteo's keyless forecast API. All expose the same
# hourly variables. More independent centers = more robust consensus.
FORECAST_MODELS = [
    "gfs_seamless",
    "ecmwf_ifs025",
    "icon_seamless",
    "metno_seamless",
    "arpege_seamless",
    "gem_seamless",
    "ukmo_seamless",
    "jma_seamless",
    "knmi_seamless",
    "icon_eu",
]

# High-resolution regional models, fetched separately so their absence for a
# location (e.g. AROME outside France) never breaks the global-model request.
# They bring km-scale precision for the "micro" weather: AROME France is
# 1.3 km, ICON-D2 is 2.2 km over Europe.
HIGH_RES_MODELS = [
    "meteofrance_arome_france",
    "icon_d2",
]

# Reanalysis products on the archive host, used as calibration ground truth.
ARCHIVE_MODELS = [
    "era5_seamless",
    "era5_land",
    "cerra",
]

DEFAULT_MODEL = FORECAST_MODELS[0]
DEFAULT_ARCHIVE = ARCHIVE_MODELS[0]


def _parse_hourly(data: dict) -> tuple[list[datetime], dict[str, list[float | None]]]:
    """Return (times, {key: values}) where keys are '{variable}_{model}'."""
    hourly = data["hourly"]
    times = [
        datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in hourly["time"]
    ]
    return times, hourly


@dataclass(frozen=True)
class OpenMeteoCollector:
    """Zero-key collector for Open-Meteo forecast and historical data.

    Fetches several independent NWP models (GFS, ECMWF, ICON, MetNo, ARPEGE)
    in a single request per cell, plus reanalysis products (era5, cerra) for
    calibration ground truth. Each model becomes a separate provider so the
    fusion can weight them by learned accuracy.
    """

    config: Config
    client: httpx.Client | None = None

    def _get(self, url: str, params: dict) -> dict:
        from ..net import get_client

        resp = get_client().get(url, params=params)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as exc:
            raise httpx.HTTPError(
                f"invalid JSON from {url}: {resp.text[:120]!r}"
            ) from exc

    def fetch_forecast(
        self,
        cell: Cell,
        models: list[str] | None = None,
        forecast_days: int = 7,
        past_days: int = 0,
        include_high_res: bool = True,
    ) -> list[ForecastSample]:
        """Fetch the latest forecast run of every model for a cell.

        Timestamps are hourly. `past_days` requests each model's own analysis
        for recent past hours, stored with issued_at == valid_at so it can be
        calibrated against reanalysis immediately.

        High-resolution regional models (AROME, ICON-D2) are requested in a
        separate, best-effort call: they cover only Europe, so their absence
        for a cell elsewhere must not fail the whole fetch.
        """
        models = models or FORECAST_MODELS
        params = {
            "latitude": cell.lat,
            "longitude": cell.lon,
            "hourly": ",".join(_FORECAST_VARS),
            "models": ",".join(models),
            "forecast_days": forecast_days,
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        }
        if past_days:
            params["past_days"] = past_days
        data = self._get(f"{self.config.open_meteo_base}/forecast", params)
        samples = self._samples_from(data, models, cell)
        if include_high_res:
            # Regional models are separate requests: run them concurrently
            # with each other (best-effort) instead of serializing the
            # first-visit backfill behind two extra round-trips.
            from concurrent.futures import ThreadPoolExecutor

            hr_models = [m for m in HIGH_RES_MODELS if m not in models]
            if hr_models:
                with ThreadPoolExecutor(max_workers=len(hr_models)) as pool:
                    futs = {
                        m: pool.submit(
                            self._get,
                            f"{self.config.open_meteo_base}/forecast",
                            {**params, "models": m},
                        )
                        for m in hr_models
                    }
                    for m, fut in futs.items():
                        try:
                            samples.extend(
                                self._samples_from(fut.result(), [m], cell)
                            )
                        except httpx.HTTPError:
                            continue
        return samples

    def _samples_from(
        self, data: dict, models: list[str], cell: Cell
    ) -> list[ForecastSample]:
        """Parse an Open-Meteo forecast payload into ForecastSample objects."""
        times, hourly = _parse_hourly(data)
        now = datetime.now(timezone.utc)

        samples: list[ForecastSample] = []
        for var in _FORECAST_VARS:
            for model in models:
                # With a single model Open-Meteo omits the model suffix in
                # the hourly keys; with several models it uses {var}_{model}.
                values = hourly.get(f"{var}_{model}") or hourly.get(var)
                if not values:
                    continue
                for t, v in zip(times, values):
                    if v is None:
                        continue
                    issued = (
                        t if t < now else now
                    )  # past analysis: issued at valid time
                    samples.append(
                        ForecastSample(
                            provider=model,
                            cell_key=cell.key,
                            variable=var,
                            issued_at=issued,
                            valid_at=t,
                            value=float(v),
                        )
                    )
        return samples

    def fetch_sun(self, cell: Cell, days: int = 4) -> dict:
        """Daily sunrise/sunset (UTC ISO) for the cell, next `days` days.

        Drives the client's night bands and day/night icons with real solar
        times instead of a fixed 6h-21h guess.
        """
        data = self._get(
            f"{self.config.open_meteo_base}/forecast",
            {
                "latitude": cell.lat,
                "longitude": cell.lon,
                "daily": "sunrise,sunset",
                "timezone": "UTC",
                "forecast_days": days,
            },
        )
        daily = data.get("daily") or {}
        return {
            "sunrise": daily.get("sunrise") or [],
            "sunset": daily.get("sunset") or [],
        }

    def fetch_historical(
        self,
        cell: Cell,
        start: datetime,
        end: datetime,
        models: list[str] | None = None,
    ) -> list[Observation]:
        """Fetch past reanalysis values for a cell, per model, as ground truth.

        Uses the dedicated archive host (era5_seamless, era5_land, cerra).
        """
        models = models or ARCHIVE_MODELS
        host = self.config.open_meteo_archive_base
        data = self._get(
            f"{host}/v1/archive",
            {
                "latitude": cell.lat,
                "longitude": cell.lon,
                "hourly": ",".join(_FORECAST_VARS),
                "models": ",".join(models),
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
                "wind_speed_unit": "ms",
                "timezone": "UTC",
            },
        )
        times, hourly = _parse_hourly(data)
        obs: list[Observation] = []
        for var in _FORECAST_VARS:
            for model in models:
                values = hourly.get(f"{var}_{model}")
                if not values:
                    continue
                for t, v in zip(times, values):
                    if v is None:
                        continue
                    obs.append(
                        Observation(
                            provider=model,
                            cell_key=cell.key,
                            variable=var,
                            time=t,
                            value=float(v),
                        )
                    )
        return obs
