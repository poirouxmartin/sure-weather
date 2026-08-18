from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    Variable.RELATIVE_HUMIDITY_2M,
    Variable.PRESSURE_MSL,
    Variable.DEW_POINT_2M,
    Variable.VISIBILITY,
]

# Default model used when none is specified (best grid available free).
DEFAULT_MODEL = "gfs_seamless"


@dataclass(frozen=True)
class OpenMeteoCollector:
    """Zero-key collector for Open-Meteo forecast and historical data.

    Open-Meteo exposes a public, keyless API with several NWP models
    (gfs_seamless, ecmwf_ifs025, icon_seamless, metno_seamless, arpege_seamless).
    """

    config: Config
    client: httpx.Client | None = None

    def _get(self, path: str, params: dict) -> dict:
        client = self.client or httpx.Client(timeout=30)
        resp = client.get(f"{self.config.open_meteo_base}/{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def fetch_forecast(
        self,
        cell: Cell,
        model: str = DEFAULT_MODEL,
        forecast_days: int = 7,
        past_days: int = 0,
    ) -> list[ForecastSample]:
        """Fetch the latest forecast run for a cell. Timestamps are hourly.

        `past_days` requests the model's own analysis for recent past hours,
        which is stored with issued_at == valid_at so it can be calibrated
        against reanalysis immediately (analysis-vs-reanalysis residual).
        """
        params = {
            "latitude": cell.lat,
            "longitude": cell.lon,
            "hourly": ",".join(_FORECAST_VARS),
            "models": model,
            "forecast_days": forecast_days,
            "timezone": "UTC",
        }
        if past_days:
            params["past_days"] = past_days
        data = self._get("forecast", params)
        hourly = data["hourly"]
        times = [
            datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            for t in hourly["time"]
        ]
        now = datetime.now(timezone.utc)
        samples: list[ForecastSample] = []
        for var in _FORECAST_VARS:
            values = hourly[var]
            for t, v in zip(times, values):
                if v is None:
                    continue
                issued = t if t < now else now  # past analysis: issued at valid time
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

    def fetch_historical(
        self,
        cell: Cell,
        start: datetime,
        end: datetime,
        model: str = DEFAULT_MODEL,
    ) -> list[Observation]:
        """Fetch past observed/analyzed values for a cell.

        Uses the dedicated archive host, which serves reanalysis data
        (era5_seamless by default) that can act as ground truth for
        calibrating model bias.
        """
        host = self.config.open_meteo_archive_base
        client = self.client or httpx.Client(timeout=60)
        resp = client.get(
            f"{host}/v1/archive",
            params={
                "latitude": cell.lat,
                "longitude": cell.lon,
                "hourly": ",".join(_FORECAST_VARS),
                "models": model if model != DEFAULT_MODEL else "era5_seamless",
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
                "timezone": "UTC",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        hourly = data["hourly"]
        times = [
            datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            for t in hourly["time"]
        ]
        obs: list[Observation] = []
        for var in _FORECAST_VARS:
            values = hourly[var]
            for t, v in zip(times, values):
                if v is None:
                    continue
                obs.append(
                    Observation(
                        provider="era5_seamless",
                        cell_key=cell.key,
                        variable=var,
                        time=t,
                        value=float(v),
                    )
                )
        return obs
