from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from ..config import Config
from ..models import Cell, Observation, Provider

# NOAA Aviation Weather METAR endpoint: free, keyless, worldwide airport
# stations with hourly (or better) reports. This is our local-station source.
METAR_BASE = "https://aviationweather.gov/api/data/metar"

# Fraction of a degree to pad around a point for a bbox of ~radius km.
_RADIUS_TO_DEG = 1.0 / 111.0

# METAR covers map to a cloud fraction: 0 = clear, 1 = overcast.
_COVER_FRACTION = {
    "CAVOK": 0.0,
    "SKC": 0.0,
    "CLR": 0.0,
    "NSC": 0.0,
    "FEW": 0.25,
    "SCT": 0.5,
    "BKN": 0.75,
    "OVC": 1.0,
}


def _station_name(icao: str) -> str:
    return f"metar_{icao}"


@dataclass(frozen=True)
class MetarStation:
    """A discovered METAR station (airport)."""

    icao: str
    lat: float
    lon: float
    name: str


@dataclass(frozen=True)
class MetarObservation:
    """One METAR report mapped to our variables."""

    icao: str
    lat: float
    lon: float
    time: datetime
    variables: dict[str, float] = field(default_factory=dict)


def _decode_visibility(visib: str) -> float | None:
    """METAR visibility -> meters. US fields are miles; '6+' means >=6 mi."""
    if visib is None:
        return None
    s = str(visib).strip()
    if s in ("6+", "P6SM", "9999"):
        return 10000.0
    if s.endswith("+") or s.endswith("SM"):
        s = s.rstrip("+SM")
    try:
        miles = float(s)
    except ValueError:
        return None
    return round(miles * 1609.344, 0)


def _decode_cloud_fraction(cover: str | None, clouds: list[dict]) -> float | None:
    """METAR cloud layers -> average cloud cover fraction (0..1)."""
    if cover is None:
        return None
    if cover == "CAVOK":
        return 0.0
    if clouds:
        # Opaque layers dominate: take the highest layer's fraction.
        fracs = [_COVER_FRACTION.get(c.get("cover", ""), 0.0) for c in clouds]
        if fracs:
            return max(fracs)
    return _COVER_FRACTION.get(cover, 0.0)


def _humidity_from_dewpoint(temp: float, dewp: float) -> float:
    """Relative humidity from temperature and dew point (Magnus formula)."""
    a = 17.625
    b = 243.04
    e_t = math.exp((a * temp) / (b + temp))
    e_d = math.exp((a * dewp) / (b + dewp))
    return round(min(100.0, max(0.0, 100.0 * e_d / e_t)), 1)


def _decode_report(report: dict) -> MetarObservation | None:
    """Map one METAR JSON record to our variable set."""
    icao = report.get("icaoId")
    if not icao or report.get("temp") is None:
        return None
    obs_time = report.get("obsTime")
    if obs_time is None:
        return None
    temp = float(report["temp"])
    vars_: dict[str, float] = {
        "temperature_2m": temp,
    }
    if report.get("dewp") is not None:
        dewp = float(report["dewp"])
        vars_["dew_point_2m"] = dewp
        vars_["relative_humidity_2m"] = _humidity_from_dewpoint(temp, dewp)
    if report.get("wspd") is not None:
        vars_["wind_speed_10m"] = round(float(report["wspd"]) * 0.514444, 2)
    if report.get("altim") is not None:
        vars_["pressure_msl"] = float(report["altim"])
    vis = _decode_visibility(report.get("visib"))
    if vis is not None:
        vars_["visibility"] = vis
    frac = _decode_cloud_fraction(report.get("cover"), report.get("clouds") or [])
    if frac is not None:
        vars_["cloud_cover"] = round(frac * 100.0, 1)
    if not vars_:
        return None
    return MetarObservation(
        icao=icao,
        lat=float(report["lat"]),
        lon=float(report["lon"]),
        time=datetime.fromtimestamp(obs_time, tz=timezone.utc),
        variables=vars_,
    )


@dataclass(frozen=True)
class MetarCollector:
    """Zero-key collector for NOAA METAR airport observations.

    Discovers stations inside a bounding box around a point (density from
    NOAA's hourly cache), then maps each report to our variable names so the
    observations can be stored and used as real-time ground truth for the
    fusion and its calibration.
    """

    config: Config
    client: httpx.Client | None = None

    def _get(self, params: dict) -> list[dict]:
        client = self.client or httpx.Client(timeout=30)
        resp = client.get(METAR_BASE, params=params)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise httpx.HTTPError(f"METAR upstream: {data['error']}")
        if not isinstance(data, list):
            return []
        return data

    def discover(self, lat: float, lon: float, radius_km: float) -> list[MetarStation]:
        """Find airport stations within `radius_km` of (lat, lon)."""
        deg = _RADIUS_TO_DEG * radius_km
        bbox = (
            f"{max(-90.0, lat - deg):.3f},{max(-180.0, lon - deg * 1.5):.3f},"
            f"{min(90.0, lat + deg):.3f},{min(180.0, lon + deg * 1.5):.3f}"
        )
        reports = self._get(
            {"bbox": bbox, "format": "json", "hours": "3", "taf": "false"}
        )
        seen: dict[str, MetarStation] = {}
        for r in reports:
            icao = r.get("icaoId")
            if not icao or r.get("lat") is None or r.get("lon") is None:
                continue
            seen[icao] = MetarStation(
                icao=icao,
                lat=float(r["lat"]),
                lon=float(r["lon"]),
                name=r.get("name") or icao,
            )
        return sorted(seen.values(), key=lambda s: s.icao)

    def fetch_observations(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        hours: int = 3,
    ) -> list[MetarObservation]:
        """Fetch recent reports from all stations near (lat, lon)."""
        deg = _RADIUS_TO_DEG * radius_km
        bbox = (
            f"{max(-90.0, lat - deg):.3f},{max(-180.0, lon - deg * 1.5):.3f},"
            f"{min(90.0, lat + deg):.3f},{min(180.0, lon + deg * 1.5):.3f}"
        )
        reports = self._get(
            {"bbox": bbox, "format": "json", "hours": str(hours), "taf": "false"}
        )
        out: list[MetarObservation] = []
        for r in reports:
            decoded = _decode_report(r)
            if decoded is not None:
                out.append(decoded)
        return out

    def to_observations(
        self,
        report: MetarObservation,
        cell_resolution: float,
        kind: str = "station",
    ) -> list[Observation]:
        """Map a decoded report to stored observations on the cell grid."""
        from ..grid import cell_from_point
        from ..config import Config

        cfg = Config(cell_resolution=cell_resolution)
        cell = cell_from_point(report.lat, report.lon, cfg)
        provider = _station_name(report.icao)
        return [
            Observation(
                provider=provider,
                cell_key=cell.key,
                variable=var,
                time=report.time,
                value=value,
            )
            for var, value in report.variables.items()
        ]

    def providers(self, reports: list[MetarObservation]) -> list[Provider]:
        icaos = sorted({r.icao for r in reports})
        return [Provider(name=_station_name(icao), kind="station") for icao in icaos]