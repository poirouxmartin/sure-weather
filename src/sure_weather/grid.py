from __future__ import annotations

import math
from typing import Iterator

from .config import Config
from .models import Cell


def _round_to_res(value: float, res: float) -> float:
    return round(round(value / res) * res, 6)


def cell_key(lat: float, lon: float, resolution: float) -> str:
    """Canonical key of the cell containing the point (lat, lon)."""
    flat = _round_to_res(lat, resolution)
    flon = _round_to_res(lon, resolution)
    return f"{flat:.4f},{flon:.4f}"


def cell_from_point(lat: float, lon: float, config: Config) -> Cell:
    flat = _round_to_res(lat, config.cell_resolution)
    flon = _round_to_res(lon, config.cell_resolution)
    return Cell(key=cell_key(lat, lon, config.cell_resolution), lat=flat, lon=flon)


def cells_in_bbox(
    min_lat: float, max_lat: float, min_lon: float, max_lon: float, config: Config
) -> list[Cell]:
    """Enumerate the cells covering a bounding box (inclusive bounds)."""
    res = config.cell_resolution
    out: list[Cell] = []
    lat = _round_to_res(min_lat, res)
    while lat <= max_lat + 1e-9:
        lon = _round_to_res(min_lon, res)
        while lon <= max_lon + 1e-9:
            out.append(Cell(key=f"{lat:.4f},{lon:.4f}", lat=lat, lon=lon))
            lon += res
        lat += res
    return out


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def iter_cells_nearby(
    lat: float, lon: float, radius_km: float, config: Config, max_cells: int = 64
) -> Iterator[Cell]:
    """Yield cells within `radius_km` of the point, closest first.

    Returns at most `max_cells` cells. Cells whose center is within the radius
    are yielded; the closest cell may be inside the radius even when the point
    is near its edge, because `cell_from_point` rounds the point into it.
    """
    res = config.cell_resolution
    center = cell_from_point(lat, lon, config)
    cell_km = max(0.1, res * 111.0)

    # Max steps along one axis to stay within radius (rounded up).
    steps = max(1, int(math.ceil(radius_km / cell_km)))
    candidates = sorted(
        (
            Cell(key=f"{la:.4f},{lo:.4f}", lat=la, lon=lo)
            for la in (center.lat + i * res for i in range(-steps, steps + 1))
            for lo in (center.lon + j * res for j in range(-steps, steps + 1))
        ),
        key=lambda c: haversine_km(lat, lon, c.lat, c.lon),
    )
    yielded = 0
    for c in candidates:
        if yielded >= max_cells:
            break
        if c.key == center.key or haversine_km(lat, lon, c.lat, c.lon) <= radius_km:
            yield c
            yielded += 1
