from __future__ import annotations

from fastapi import FastAPI, Query

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
