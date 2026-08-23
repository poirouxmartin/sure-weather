from __future__ import annotations

import threading
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .net import get_client
from .service import WeatherService
from .storage import Storage
from .tiles import render_tile

app = FastAPI(title="Sure Weather", version="0.1.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)
_config = load_config()
_storage = Storage(_config.db_path)
_service = WeatherService(_storage, _config)


@app.on_event("startup")
def _warm_stats_cache() -> None:
    """Precompute calibration stats in the background at boot.

    The first forecast after a restart would otherwise pay the residual
    aggregation inside the user's request; warming it here keeps every
    search snappy from the first one.
    """

    def _warm() -> None:
        try:
            _service._load_stats()
        except Exception:
            # A failed warm-up must not prevent serving: the request path
            # will simply compute stats on demand.
            pass

    threading.Thread(target=_warm, daemon=True).start()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/weather")
def weather(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    hours: int = Query(48, ge=1, le=168),
) -> dict:
    return _service.forecast(lat, lon, hours=hours)


@app.get("/geocode")
def geocode(q: str = Query(..., min_length=2), limit: int = Query(5, ge=1, le=10)) -> dict:
    """Forward an address/city query to Nominatim (OpenStreetMap).

    Open-Meteo's geocoder only knows cities; an exact street address needs a
    full address geocoder. Nominatim is proxied here so the browser gets a
    same-origin call, a proper User-Agent is sent (their usage policy), and
    results can be cached server-side. Returns a list of candidate points with
    precise lat/lon, which the weather service then uses to target the closest
    local METAR stations.
    """
    try:
        r = get_client().get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": q,
                "format": "json",
                "limit": limit,
                "addressdetails": 0,
                "accept-language": "fr",
                "extratags": 0,
            },
            headers={"User-Agent": "sure-weather/0.1 (weather forecast app)"},
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"geocode upstream: {exc}") from exc
    results = [
        {
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "name": item.get("display_name", q),
            "type": item.get("type", ""),
        }
        for item in r.json()
    ]
    return {"results": results}


@app.get("/radar")
def radar() -> dict:
    """Proxy to RainViewer's radar frame index (past + nowcast, free, keyless).

    The frames give 256px XYZ tile URLs for precipitation radar; nowcast
    extends the last measured frame ~30 min ahead. The client animates them
    over a map. Proxying avoids CORS and keeps the tile host configurable.
    """
    try:
        r = get_client().get(
            "https://api.rainviewer.com/public/weather-maps.json", timeout=30
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"radar upstream: {exc}") from exc


@app.get("/tile/{layer}/{z}/{x}/{y}.png")
def tile(layer: str, z: int, x: int, y: int) -> Response:
    """Raster overlay tile (temperature or precipitation) from the Open-Meteo model.

    Open-Meteo no longer serves plain XYZ weather tiles (its current format is
    a WASM-decoded `om://` protocol, too heavy for this vanilla front-end).
    Instead the backend samples the multi-location forecast API on a grid over
    each tile, colorizes the result and returns a small PNG. Rendered tiles are
    cached in memory for a few minutes, so panning reuses the same requests.
    """
    if layer not in ("temp", "precip"):
        raise HTTPException(status_code=404, detail=f"unknown layer {layer!r}")
    if not (3 <= z <= 12):
        raise HTTPException(status_code=404, detail="zoom out of range")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise HTTPException(status_code=404, detail="tile out of range")
    data = render_tile(layer, z, x, y)
    if data is None:
        raise HTTPException(status_code=502, detail="tile upstream unavailable")
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=600"},
    )


_web_dir = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=_web_dir), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_web_dir / "index.html")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(_web_dir / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(_web_dir / "sw.js", media_type="application/javascript")