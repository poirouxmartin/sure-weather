from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


class Variable:
    TEMPERATURE_2M = "temperature_2m"
    PRECIPITATION = "precipitation"
    PRECIPITATION_PROBABILITY = "precipitation_probability"
    CLOUD_COVER = "cloud_cover"
    WIND_SPEED_10M = "wind_speed_10m"
    WIND_GUSTS_10M = "wind_gusts_10m"
    WIND_DIRECTION_10M = "wind_direction_10m"
    UV_INDEX = "uv_index"
    RELATIVE_HUMIDITY_2M = "relative_humidity_2m"
    PRESSURE_MSL = "pressure_msl"
    DEW_POINT_2M = "dew_point_2m"
    VISIBILITY = "visibility"


# Variables that only make sense as forecasts (no physical observation feeds it directly).
PROBABILISTIC_VARIABLES = frozenset({Variable.PRECIPITATION_PROBABILITY})

ALL_VARIABLES = (
    Variable.TEMPERATURE_2M,
    Variable.PRECIPITATION,
    Variable.PRECIPITATION_PROBABILITY,
    Variable.CLOUD_COVER,
    Variable.WIND_SPEED_10M,
    Variable.WIND_GUSTS_10M,
    Variable.WIND_DIRECTION_10M,
    Variable.UV_INDEX,
    Variable.RELATIVE_HUMIDITY_2M,
    Variable.PRESSURE_MSL,
    Variable.DEW_POINT_2M,
    Variable.VISIBILITY,
)

OBSERVABLE_VARIABLES = tuple(
    v for v in ALL_VARIABLES if v not in PROBABILISTIC_VARIABLES
)


@dataclass(frozen=True)
class Cell:
    """A cell of the learning grid. `key` is the canonical identifier."""

    key: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Provider:
    """A weather data source. `kind` is 'model' or 'station'."""

    name: str
    kind: str


@dataclass(frozen=True)
class Observation:
    """A measured value at a point, from a station or a model's analysis."""

    provider: str
    cell_key: str
    variable: str
    time: datetime
    value: float


@dataclass(frozen=True)
class ForecastSample:
    """A single provider's forecast for one cell, variable and valid time."""

    provider: str
    cell_key: str
    variable: str
    issued_at: datetime
    valid_at: datetime
    value: float


@dataclass(frozen=True)
class Residual:
    """Prediction error measured later: forecast valid_at vs observation at valid_at."""

    provider: str
    cell_key: str
    variable: str
    valid_at: datetime
    horizon_h: float
    predicted: float
    observed: float

    @property
    def bias(self) -> float:
        return self.predicted - self.observed

    @property
    def abs_error(self) -> float:
        return abs(self.bias)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
