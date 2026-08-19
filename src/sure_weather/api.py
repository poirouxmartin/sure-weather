from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
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