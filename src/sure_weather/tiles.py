from __future__ import annotations

import math
import struct
import threading
import time
import zlib
from typing import Callable

import httpx
import json
import numpy as np


# ---- Minimal PNG encoder (stdlib + numpy, no Pillow) ----

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png_rgba(pixels: np.ndarray) -> bytes:
    """Encode an (H, W, 4) uint8 RGBA array as a PNG image."""
    h, w = pixels.shape[:2]
    rows = bytearray()
    for y in range(h):
        rows.append(0)  # filter type 0: none
        rows.extend(pixels[y].tobytes())
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _png_chunk(b"IEND", b"")
    )


# ---- Slippy map geometry ----

def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (lon_west, lat_south, lon_east, lat_north) for a slippy tile."""
    n = 2.0**z
    lon_west = x / n * 360.0 - 180.0
    lon_east = (x + 1) / n * 360.0 - 180.0
    lat_north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lon_west, lat_south, lon_east, lat_north


# ---- Colormaps ----

def _build_lut(stops: list[tuple[float, tuple[int, int, int, int]]]) -> np.ndarray:
    """Build a (256, 4) uint8 RGBA LUT from (value, rgba) stops."""
    values = np.array([s[0] for s in stops])
    rgba = np.array([s[1] for s in stops], dtype=float)
    lut = np.empty((256, 4))
    xs = np.linspace(values[0], values[-1], 256)
    for c in range(4):
        lut[:, c] = np.interp(xs, values, rgba[:, c])
    return np.clip(lut, 0, 255).astype(np.uint8)


_TEMPERATURE_STOPS = [
    (-10.0, (69, 117, 180, 200)),
    (0.0, (116, 173, 209, 200)),
    (10.0, (171, 221, 164, 200)),
    (18.0, (254, 224, 144, 200)),
    (25.0, (253, 174, 97, 215)),
    (33.0, (244, 109, 67, 225)),
    (40.0, (165, 0, 38, 230)),
]
_TEMPERATURE_LUT = _build_lut(_TEMPERATURE_STOPS)
_TEMP_VMIN, _TEMP_VMAX = -10.0, 40.0

_PRECIP_STOPS = [
    (0.0, (255, 255, 255, 0)),
    (0.1, (200, 230, 255, 120)),
    (0.5, (120, 190, 255, 165)),
    (2.0, (60, 140, 255, 205)),
    (6.0, (30, 90, 220, 235)),
    (12.0, (20, 40, 160, 245)),
    (20.0, (10, 10, 90, 250)),
]
_PRECIP_LUT = _build_lut(_PRECIP_STOPS)
_PRECIP_VMIN, _PRECIP_VMAX = 0.0, 20.0


def _apply_lut(vals: np.ndarray, lut: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """Map a (H, W) float array to an (H, W, 4) RGBA overlay."""
    h, w = vals.shape
    idx = np.full((h, w), 0, dtype=np.uint8)
    finite = np.isfinite(vals)
    idx[finite] = np.clip((vals[finite] - vmin) / (vmax - vmin) * 255.0, 0, 255).astype(np.uint8)
    rgba = lut[idx]
    rgba[~finite, 3] = 0  # no data → fully transparent
    return rgba


def temperature_overlay(vals: np.ndarray) -> np.ndarray:
    return _apply_lut(vals, _TEMPERATURE_LUT, _TEMP_VMIN, _TEMP_VMAX)


def precipitation_overlay(vals: np.ndarray) -> np.ndarray:
    return _apply_lut(vals, _PRECIP_LUT, _PRECIP_VMIN, _PRECIP_VMAX)


# ---- Open-Meteo sampling + cache ----

_SAMPLES = 8  # grid per tile axis (matches ~model resolution at map zooms)
_TILE_SIZE = 256
_CACHE_TTL_S = 10 * 60
_CACHE_MAX = 256

_tile_cache: dict[tuple[str, int, int, int], tuple[float, bytes]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: tuple[str, int, int, int]) -> bytes | None:
    now = time.time()
    with _cache_lock:
        hit = _tile_cache.get(key)
        if hit is None:
            return None
        ts, data = hit
        if now - ts > _CACHE_TTL_S:
            _tile_cache.pop(key, None)
            return None
        return data


def _cache_put(key: tuple[str, int, int, int], data: bytes) -> None:
    with _cache_lock:
        if len(_tile_cache) >= _CACHE_MAX:
            oldest = min(_tile_cache, key=lambda k: _tile_cache[k][0])
            _tile_cache.pop(oldest, None)
        _tile_cache[key] = (time.time(), data)


_CURRENT_VARS = "temperature_2m,precipitation"


def wind_grid(
    lat_n: float,
    lon_w: float,
    lat_s: float,
    lon_e: float,
    n: int = 6,
    *,
    base_url: str = "https://api.open-meteo.com/v1",
) -> list[dict] | None:
    """Sample wind speed + direction on a grid over the given bounds.

    Powers the map's wind-arrow overlay: one multi-location request for the
    whole viewport, cached briefly so panning doesn't hammer the API.
    """
    key = ("windgrid", round(lat_n, 2), round(lon_w, 2), round(lat_s, 2), round(lon_e, 2), n)
    cached = _cache_get(key)
    if cached is not None:
        return json.loads(cached)
    params: list[tuple[str, str]] = []
    lats = np.linspace(lat_n, lat_s, n)
    lons = np.linspace(lon_w, lon_e, n)
    for la in lats:
        for lo in lons:
            params.append(("latitude", f"{la:.4f}"))
            params.append(("longitude", f"{lo:.4f}"))
    params.append(("current", "wind_speed_10m,wind_direction_10m"))
    try:
        from .net import get_client

        r = get_client().get(f"{base_url}/forecast", params=params, timeout=30)
        r.raise_for_status()
        samples = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(samples, list):
        return None
    points = []
    for i, s in enumerate(samples):
        cur = s.get("current") or {}
        speed = cur.get("wind_speed_10m")
        deg = cur.get("wind_direction_10m")
        if speed is None or deg is None:
            continue
        points.append({
            "lat": round(float(lats[i // n]), 4),
            "lon": round(float(lons[i % n]), 4),
            "kmh": round(speed * 3.6),
            "deg": round(float(deg)),
        })
    data = json.dumps(points)
    _cache_put(key, data)
    return points


def _build_request_params(
    z: int, x: int, y: int, *, hour_offset: int = 0
) -> tuple[list[tuple[str, str]], str]:
    """Lat/lon grid sampling the tile, north→south rows, west→east columns.

    hour_offset=0 uses the live `current` block; offsets 1..23 request the
    hourly precipitation array instead — that is how the map shows where the
    model moves the rain AFTER the radar's last observed frame.
    """
    lon_w, lat_s, lon_e, lat_n = tile_bounds(z, x, y)
    lats = np.linspace(lat_n, lat_s, _SAMPLES)
    lons = np.linspace(lon_w, lon_e, _SAMPLES)
    params: list[tuple[str, str]] = []
    for la in lats:
        for lo in lons:
            params.append(("latitude", f"{la:.4f}"))
            params.append(("longitude", f"{lo:.4f}"))
    if hour_offset:
        params.append(("hourly", "precipitation"))
        params.append(("forecast_days", "2"))
        params.append(("timeformat", "unixtime"))
        return params, "h"
    params.append(("current", _CURRENT_VARS))
    return params, "now"


def _sample_to_grid(layer: str, samples: list[dict], hour_offset: int = 0) -> np.ndarray:
    """Map the flat list of per-location responses back to a value grid."""
    if hour_offset:
        target = int(time.time()) // 3600 * 3600 + hour_offset * 3600
        grid = np.full((_SAMPLES, _SAMPLES), np.nan)
        for i, s in enumerate(samples):
            times = (s.get("hourly") or {}).get("time") or []
            vals = (s.get("hourly") or {}).get("precipitation") or []
            best = None  # nearest forecast hour to the requested offset
            for t, v in zip(times, vals):
                if v is None:
                    continue
                d = abs(int(t) - target)
                if best is None or d < best[0]:
                    best = (d, v)
            if best is not None:
                grid[i // _SAMPLES, i % _SAMPLES] = float(best[1])
        return grid
    key = "temperature_2m" if layer == "temp" else "precipitation"
    grid = np.full((_SAMPLES, _SAMPLES), np.nan, dtype=np.float64)
    for i, s in enumerate(samples):
        cur = s.get("current") or {}
        val = cur.get(key)
        if val is not None:
            grid[i // _SAMPLES, i % _SAMPLES] = float(val)
    return grid


def render_tile(
    layer: str,
    z: int,
    x: int,
    y: int,
    *,
    hour_offset: int = 0,
    base_url: str = "https://api.open-meteo.com/v1",
    user_agent: str = "sure-weather/0.1",
) -> bytes | None:
    """Render a 256x256 PNG overlay tile for `layer` in ('temp', 'precip').

    `hour_offset` > 0 renders the model's precipitation that many hours from
    now: the "future frames" that extend the observed radar timeline.
    """
    if layer not in ("temp", "precip"):
        return None
    key = (layer, z, x, y, hour_offset)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        from .net import get_client

        params, _ = _build_request_params(z, x, y, hour_offset=hour_offset)
        r = get_client().get(
            f"{base_url}/forecast",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    samples = r.json()
    if not isinstance(samples, list):
        return None
    grid = _sample_to_grid(layer, samples, hour_offset=hour_offset)
    overlay = (
        temperature_overlay(grid)
        if layer == "temp"
        else precipitation_overlay(grid)
    )
    upscale = _TILE_SIZE // _SAMPLES
    pixels = np.repeat(np.repeat(overlay, upscale, axis=0), upscale, axis=1)
    data = encode_png_rgba(pixels)
    _cache_put(key, data)
    return data