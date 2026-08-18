from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    db_path: Path = Path("data/weather.db")
    # Resolution of the learning grid, in degrees (~0.1° ≈ 11 km at the equator).
    cell_resolution: float = 0.1
    # Radius (in km) used to find observations around a target point for nowcasting.
    obs_search_radius_km: float = 30.0
    # Half-life of the recency decay applied to observations, in hours.
    obs_recency_halflife_h: float = 2.0
    # Hard cap on the number of nearby observations fused for one variable.
    max_obs_per_variable: int = 8
    # Provider weights are recalibrated from residuals at least every N hours.
    calibration_interval_h: float = 24.0
    # Residual window, in days, used to estimate provider bias / rmse.
    residual_window_days: int = 30

    open_meteo_base: str = "https://api.open-meteo.com/v1"
    open_meteo_archive_base: str = "https://archive-api.open-meteo.com"
    openweather_api_key: str = ""
    netatmo_client_id: str = ""
    netatmo_client_secret: str = ""
    netatmo_username: str = ""
    netatmo_password: str = ""


def load_config() -> Config:
    import os

    def _env(name: str, default: str) -> str:
        return os.environ.get(name, default)

    return Config(
        db_path=Path(_env("SURE_WEATHER_DB", str(Config.db_path))),
        cell_resolution=float(
            _env("SURE_WEATHER_CELL_RES", str(Config.cell_resolution))
        ),
        obs_search_radius_km=float(
            _env("SURE_WEATHER_OBS_RADIUS_KM", str(Config.obs_search_radius_km))
        ),
        obs_recency_halflife_h=float(
            _env("SURE_WEATHER_OBS_HALFLIFE_H", str(Config.obs_recency_halflife_h))
        ),
        max_obs_per_variable=int(
            _env("SURE_WEATHER_MAX_OBS", str(Config.max_obs_per_variable))
        ),
        calibration_interval_h=float(
            _env("SURE_WEATHER_CALIB_INTERVAL_H", str(Config.calibration_interval_h))
        ),
        residual_window_days=int(
            _env("SURE_WEATHER_RESIDUAL_WINDOW_DAYS", str(Config.residual_window_days))
        ),
        open_meteo_base=_env("SURE_WEATHER_OPEN_METEO_BASE", Config.open_meteo_base),
        open_meteo_archive_base=_env(
            "SURE_WEATHER_OPEN_METEO_ARCHIVE_BASE", Config.open_meteo_archive_base
        ),
        openweather_api_key=_env(
            "SURE_WEATHER_OPENWEATHER_KEY", Config.openweather_api_key
        ),
        netatmo_client_id=_env("NETATMO_CLIENT_ID", Config.netatmo_client_id),
        netatmo_client_secret=_env(
            "NETATMO_CLIENT_SECRET", Config.netatmo_client_secret
        ),
        netatmo_username=_env("NETATMO_USERNAME", Config.netatmo_username),
        netatmo_password=_env("NETATMO_PASSWORD", Config.netatmo_password),
    )
