from __future__ import annotations

import threading
from datetime import datetime, timezone
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
from .tiles import render_tile, wind_grid as render_wind_grid

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
def tile(
    layer: str,
    z: int,
    x: int,
    y: int,
    h: int = Query(0, ge=0, le=23),
) -> Response:
    """Raster overlay tile (temperature or precipitation) from the Open-Meteo model.

    Open-Meteo no longer serves plain XYZ weather tiles (its current format is
    a WASM-decoded `om://` protocol, too heavy for this vanilla front-end).
    Instead the backend samples the multi-location forecast API on a grid over
    each tile, colorizes the result and returns a small PNG. Rendered tiles are
    cached in memory for a few minutes, so panning reuses the same requests.

    `h` shifts the precipitation layer into the future (1..23 h from now):
    the model-side "forecast frames" that extend the observed radar timeline.
    """
    if layer not in ("temp", "precip", "uv", "humidity", "cloud", "pressure"):
        raise HTTPException(status_code=404, detail=f"unknown layer {layer!r}")
    if layer in ("temp", "uv", "humidity", "cloud", "pressure") and h:
        raise HTTPException(status_code=422, detail="h applies to precip only")
    if not (3 <= z <= 12):
        raise HTTPException(status_code=404, detail="zoom out of range")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise HTTPException(status_code=404, detail="tile out of range")
    data = render_tile(layer, z, x, y, hour_offset=h)
    if data is None:
        raise HTTPException(status_code=502, detail="tile upstream unavailable")
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=600"},
    )


_web_dir = Path(__file__).parent / "web"


class _NoCacheStatic(StaticFiles):
    """Static assets must always revalidate.

    Without Cache-Control, browsers apply heuristic freshness and can pin a
    stale app.css/app.js for the rest of the day (the service worker's
    network-first fetch inherits that HTTP cache). `no-cache` keeps etag
    304s while guaranteeing freshness after every deploy.
    """

    def file_response(self, *args, **kwargs):  # noqa: D102
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _NoCacheStatic(directory=_web_dir), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_web_dir / "index.html")


@app.get("/mini", include_in_schema=False)
def mini() -> FileResponse:
    """Ultra-light widget-style page: one card, no map, 10-min auto refresh.

    Designed to be pinned to a phone home screen (per-place shortcut with
    ?lat=&lon=&name=) where native widgets are unavailable to PWAs.
    """
    return FileResponse(_web_dir / "mini.html")


# ---- Widget feed (home-screen widgets via Shortcuts iOS / KWGT Android) ----

def _sky_key(row: dict, hour: int) -> str:
    rain = row.get("precipitation", {}).get("value", 0) or 0
    prob = row.get("precipitation_probability", {}).get("value", 0) or 0
    cloud = row.get("cloud_cover", {}).get("value", 0) or 0
    if rain > 5:
        return "storm"
    if prob >= 70 or rain > 0.3:
        return "rain"
    if cloud >= 30:
        return "partly"
    return "night" if (hour < 6 or hour >= 21) else "day"


def _now_summary(lat: float, lon: float, hours: int = 24) -> dict:
    """Compact now+day summary for widgets, built on the cached forecast."""
    data = _service.forecast(lat, lon, hours=hours)
    by_time: dict[str, dict] = {}
    for item in data["forecast"]:
        by_time.setdefault(item["valid_at"], {})[item["variable"]] = item
    times = sorted(by_time)
    if not times:
        raise HTTPException(status_code=404, detail="no forecast data")
    cur = by_time[times[0]]
    temp = cur.get("temperature_2m", {})
    wind = cur.get("wind_speed_10m", {})
    gust = cur.get("wind_gusts_10m", {})
    temps = [i["value"] for i in data["forecast"] if i["variable"] == "temperature_2m"]
    rain_mm = sum(
        (i["value"] or 0)
        for i in data["forecast"]
        if i["variable"] == "precipitation"
    )
    contributors = {p for i in data["forecast"] for p in i.get("contributors", [])}
    stations = {p for p in contributors if p.startswith("metar_")}
    hour = datetime.now(timezone.utc).hour
    summary = {
        "location": {"lat": lat, "lon": lon},
        "cell": data["cell"],
        "updated_at": data["generated_at"],
        "temp": temp.get("value"),
        "low": temp.get("low"),
        "high": temp.get("high"),
        "confidence": temp.get("confidence"),
        "sure": temp.get("sure", False),
        "rain_probability": (cur.get("precipitation_probability") or {}).get("value"),
        "rain_24h_mm": round(rain_mm, 1),
        "wind_kmh": round(wind["value"] * 3.6) if wind else None,
        "gust_kmh": round(gust["value"] * 3.6) if gust else None,
        "tmax": round(max(temps), 1) if temps else None,
        "tmin": round(min(temps), 1) if temps else None,
        "sky": _sky_key(cur, hour),
        "stations": len(stations),
        "models": len(contributors) - len(stations),
    }
    return summary


@app.get("/wind-grid")
def wind_grid_endpoint(
    lat_n: float = Query(..., ge=-90, le=90),
    lon_w: float = Query(..., ge=-180, le=180),
    lat_s: float = Query(..., ge=-90, le=90),
    lon_e: float = Query(..., ge=-180, le=180),
    n: int = Query(6, ge=2, le=10),
) -> dict:
    """Wind arrows for the map overlay: a small grid of speed + direction.

    The client renders rotating arrows colored by speed on top of the radar.
    Cached like tiles, so map panning reuses the same upstream request.
    """
    if lat_s >= lat_n or lon_e <= lon_w:
        raise HTTPException(status_code=422, detail="invalid bounds")
    points = render_wind_grid(lat_n, lon_w, lat_s, lon_e, n)
    if points is None:
        raise HTTPException(status_code=502, detail="wind upstream unavailable")
    return {"points": points}


@app.get("/now")
def now_endpoint(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> dict:
    """Compact current-weather JSON for smartphone widgets.

    Consumed by iOS Shortcuts and Android KWGT/Tasker (PWAs cannot ship
    native home-screen widgets). Reuses the forecast TTL cache, so polling
    every few minutes costs nothing.
    """
    return _now_summary(lat, lon)


_SKY_COLORS = {
    "day": ("#7cc3f5", "#b8ddf7"),
    "night": ("#0f1b3a", "#3a4a74"),
    "rain": ("#4b6275", "#7a8fa3"),
    "storm": ("#2b3647", "#4d5c72"),
    "partly": ("#6fb2e8", "#a8d3f3"),
}
_SKY_ICONS = {"day": "☀️", "night": "🌙", "rain": "🌧", "storm": "⛈", "partly": "⛅"}


@app.get("/widget.svg")
def widget_svg(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    name: str = Query(""),
) -> Response:
    """Rendered SVG weather card for widget tools that accept images."""
    s = _now_summary(lat, lon)
    top, bottom = _SKY_COLORS.get(s["sky"], _SKY_COLORS["day"])
    icon = _SKY_ICONS.get(s["sky"], "☀️")
    label = name or f"{lat:.4f}, {lon:.4f}"
    conf = round((s["confidence"] or 0) * 100)
    sure_txt = "sûr" if s["sure"] else f"conf. {conf}%"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="400" height="220" viewBox="0 0 400 220">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{top}"/>
      <stop offset="1" stop-color="{bottom}"/>
    </linearGradient>
  </defs>
  <rect width="400" height="220" rx="24" fill="url(#bg)"/>
  <text x="24" y="40" font-family="system-ui,sans-serif" font-size="17" font-weight="700" fill="#ffffff" opacity=".92">{label[:34]}</text>
  <text x="24" y="150" font-family="system-ui,sans-serif" font-size="64" font-weight="800" fill="#ffffff">{s["temp"] is not None and f'{s["temp"]:.0f}°' or '—'}</text>
  <text x="250" y="86" font-family="system-ui,sans-serif" font-size="44">{icon}</text>
  <text x="24" y="182" font-family="system-ui,sans-serif" font-size="15" fill="#ffffff" opacity=".9">↑{s["tmax"]}° ↓{s["tmin"]}° · ☔ {s["rain_probability"] is not None and f'{s["rain_probability"]:.0f}%' or '—'}</text>
  <text x="24" y="204" font-family="system-ui,sans-serif" font-size="12" fill="#ffffff" opacity=".75">{s["wind_kmh"] or '—'} km/h · {sure_txt} · {s["stations"]} station{s["stations"] > 1 and 's' or ''}</text>
</svg>"""
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(_web_dir / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(_web_dir / "sw.js", media_type="application/javascript")