from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .service import WeatherService
from .storage import Storage

app = FastAPI(title="Sure Weather", version="0.1.0")
_config = load_config()
_storage = Storage(_config.db_path)
_service = WeatherService(_storage, _config)


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


@app.get("/radar")
def radar() -> dict:
    """Proxy to RainViewer's radar frame index (past + nowcast, free, keyless).

    The frames give 256px XYZ tile URLs for precipitation radar; nowcast
    extends the last measured frame ~30 min ahead. The client animates them
    over a map. Proxying avoids CORS and keeps the tile host configurable.
    """
    try:
        r = httpx.get(
            "https://api.rainviewer.com/public/weather-maps.json", timeout=30
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"radar upstream: {exc}") from exc


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