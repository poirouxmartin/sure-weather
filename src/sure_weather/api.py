from __future__ import annotations

import html
import ipaddress
import os
import threading
import time
import urllib.parse
from datetime import datetime, timezone
import base64
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_config
from .net import get_client
from .service import WeatherService
from .storage import Storage
from .tiles import render_tile, wind_grid as render_wind_grid

app = FastAPI(title="Sure Weather", version="0.1.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)
# /now is consumed cross-origin by Shortcuts/KWGT widgets: allow browsers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
    max_age=3600,
)
_config = load_config()
_storage = Storage(_config.db_path)
_service = WeatherService(_storage, _config)


# ---- Push notifications (VAPID) ----
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _generate_vapid_keys() -> dict:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pub = priv.public_key()
    priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
    pub_nums = pub.public_numbers()
    pub_bytes = (
        b"\x04" + pub_nums.x.to_bytes(32, "big") + pub_nums.y.to_bytes(32, "big")
    )
    return {"public": _b64url(pub_bytes), "private": _b64url(priv_bytes)}


def _get_vapid_keys() -> dict:
    f = Path(_config.db_path).parent / "vapid.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    keys = _generate_vapid_keys()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(keys))
        try:
            os.chmod(f, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    return keys


# ---- Admin gate + endpoint validation for push (anti-spam / anti-SSRF) ----
def _require_admin(token: str | None) -> None:
    expected = os.environ.get("SURE_WEATHER_ADMIN_TOKEN", "")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="admin token required")


def _validate_push_endpoint(endpoint: str) -> None:
    """Reject non-HTTPS, oversized, or private-network push endpoints."""
    if not endpoint or len(endpoint) > 2048:
        raise HTTPException(status_code=422, detail="invalid endpoint")
    try:
        u = urllib.parse.urlparse(endpoint)
    except Exception:
        raise HTTPException(status_code=422, detail="invalid endpoint")
    if u.scheme != "https" or not u.hostname:
        raise HTTPException(status_code=422, detail="endpoint must be https")
    try:
        ip = ipaddress.ip_address(u.hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise HTTPException(status_code=422, detail="private endpoint refused")
    except ValueError:
        pass  # hostname, not a literal IP: allowed (push services)


# ---- Lightweight per-IP rate limiting for open proxies ----
_RATE_BUCKETS: dict[str, list[float]] = {}
_RATE_LOCK = threading.Lock()
_GEOCODE_CACHE: dict[tuple[str, int], tuple[float, list]] = {}


def _rate_limit(key: str, limit: int = 30, window_s: float = 60.0) -> None:
    now = time.time()
    with _RATE_LOCK:
        hits = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window_s]
        if len(hits) >= limit:
            raise HTTPException(status_code=429, detail="rate limited")
        hits.append(now)
        _RATE_BUCKETS[key] = hits[-limit:]


def _rate_limit_by_ip(
    request, endpoint: str, limit: int = 30, window_s: float = 60.0
) -> None:
    """Per-IP sliding-window limiter for open-proxy routes (geocode/radar/tiles)."""
    try:
        ip = request.client.host if request and request.client else "unknown"
    except Exception:
        ip = "unknown"
    _rate_limit(f"{endpoint}:{ip}", limit, window_s)


class PushSubscription(BaseModel):
    endpoint: str
    keys: dict  # {p256dh, auth}
    lat: float | None = None
    lon: float | None = None


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
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
    yield


app.router.lifespan_context = _lifespan


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
def geocode(
    request: Request,
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(5, ge=1, le=10),
) -> dict:
    """Forward an address/city query to Nominatim (OpenStreetMap).

    Open-Meteo's geocoder only knows cities; an exact street address needs a
    full address geocoder. Nominatim is proxied here so the browser gets a
    same-origin call, a proper User-Agent is sent (their usage policy), and
    results can be cached server-side. Returns a list of candidate points with
    precise lat/lon, which the weather service then uses to target the closest
    local METAR stations.
    """
    _rate_limit_by_ip(request, "geocode", limit=30)
    # Small in-process cache: repeated district/street searches reuse upstream.
    cache_key = (q.strip().lower(), limit)
    now = time.time()
    hit = _GEOCODE_CACHE.get(cache_key)
    if hit and now - hit[0] < 3600:
        return {"results": hit[1]}
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
    _GEOCODE_CACHE[cache_key] = (time.time(), results)
    if len(_GEOCODE_CACHE) > 256:
        _GEOCODE_CACHE.pop(next(iter(_GEOCODE_CACHE)))
    return {"results": results}


@app.get("/radar")
def radar(request: Request) -> dict:
    """Proxy to RainViewer's radar frame index (past + nowcast, free, keyless).

    The frames give 256px XYZ tile URLs for precipitation radar; nowcast
    extends the last measured frame ~30 min ahead. The client animates them
    over a map. Proxying avoids CORS and keeps the tile host configurable.
    """
    _rate_limit_by_ip(request, "radar", limit=30)
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
    request: Request,
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
    _rate_limit_by_ip(request, "tile", limit=120, window_s=60.0)
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


def _is_night_sun(sun: dict | None, at: datetime | None = None) -> bool | None:
    """True/False from real sunrise/sunset when available, else None (unknown)."""
    try:
        daily = (sun or {}).get("daily") or {}
        sr_list = daily.get("sunrise") or []
        ss_list = daily.get("sunset") or []
        if not sr_list or not ss_list:
            return None
        at = at or datetime.now(timezone.utc)
        t = at.timestamp()
        for sr_iso, ss_iso in zip(sr_list, ss_list):
            sr = datetime.fromisoformat(sr_iso).timestamp()
            ss = datetime.fromisoformat(ss_iso).timestamp()
            if t < sr:
                return True
            if t < ss:
                return False
        return True
    except Exception:
        return None


def _sky_key(row: dict, hour: int, sun: dict | None = None) -> str:
    rain = row.get("precipitation", {}).get("value", 0) or 0
    prob = row.get("precipitation_probability", {}).get("value", 0) or 0
    cloud = row.get("cloud_cover", {}).get("value", 0) or 0
    if rain > 5:
        return "storm"
    if prob >= 70 or rain > 0.3:
        return "rain"
    if cloud >= 70:
        night = _is_night_sun(sun)
        return "night" if night else "cloud" if night is False else "cloud"
    if cloud >= 30:
        night = _is_night_sun(sun)
        return "night" if night else "partly" if night is False else "partly"
    night = _is_night_sun(sun)
    if night is None:
        return "night" if (hour < 6 or hour >= 21) else "day"
    return "night" if night else "day"


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
        (i["value"] or 0) for i in data["forecast"] if i["variable"] == "precipitation"
    )
    contributors = {p for i in data["forecast"] for p in i.get("contributors", [])}
    stations = {p for p in contributors if p.startswith("metar_")}
    hour = datetime.now(timezone.utc).hour
    sun = data.get("sun")
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
        "sky": _sky_key(cur, hour, sun),
        "stations": len(stations),
        "models": len(contributors) - len(stations),
    }
    return summary


@app.get("/wind-grid")
def wind_grid_endpoint(
    request: Request,
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
    _rate_limit_by_ip(request, "wind-grid", limit=60)
    if lat_s >= lat_n or lon_e <= lon_w:
        raise HTTPException(status_code=422, detail="invalid bounds")
    points = render_wind_grid(lat_n, lon_w, lat_s, lon_e, n)
    if points is None:
        raise HTTPException(status_code=502, detail="wind upstream unavailable")
    return {"points": points}


@app.get("/push/key")
def push_key() -> dict:
    """VAPID public key for the frontend to subscribe with PushManager."""
    return {"publicKey": _get_vapid_keys()["public"]}


@app.post("/push/subscribe")
def push_subscribe(sub: PushSubscription) -> dict:
    if not sub.endpoint or not sub.keys.get("p256dh") or not sub.keys.get("auth"):
        raise HTTPException(status_code=422, detail="invalid subscription")
    _validate_push_endpoint(sub.endpoint)
    if len(sub.keys.get("p256dh", "")) > 512 or len(sub.keys.get("auth", "")) > 256:
        raise HTTPException(status_code=422, detail="invalid subscription keys")
    _storage.add_push_subscription(
        sub.endpoint, sub.keys["p256dh"], sub.keys["auth"], sub.lat, sub.lon
    )
    return {"ok": True}


@app.delete("/push/subscribe")
def push_unsubscribe(endpoint: str = Query(...)) -> dict:
    _storage.remove_push_subscription(endpoint)
    return {"ok": True}


@app.post("/push/test")
def push_test(
    request: Request,
    lat: float | None = Query(None),
    lon: float | None = Query(None),
    token: str | None = Query(None),
) -> dict:
    """Send a test push to all (or location-filtered) subscribers.

    Admin-gated: requires ?token=SURE_WEATHER_ADMIN_TOKEN, else 403.
    """
    _require_admin(token)
    _rate_limit_by_ip(request, "push-test", limit=5, window_s=300.0)
    subs = _storage.list_push_subscriptions()
    if lat is not None and lon is not None:
        # rough filter: subscribers near the requested location
        subs = [
            s
            for s in subs
            if s["lat"] is not None
            and abs(s["lat"] - lat) < 1.5
            and abs(s["lon"] - lon) < 1.5
        ]
    if not subs:
        raise HTTPException(status_code=404, detail="no subscribers")
    keys = _get_vapid_keys()
    from pywebpush import WebPushException, webpush

    sent = 0
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=json.dumps(
                    {
                        "title": "Sure Weather — test",
                        "body": "Pluie prévue dans l'heure ☔",
                        "url": f"/?lat={lat or 48.85}&lon={lon or 2.35}",
                    }
                ),
                vapid_private_key=keys["private"],
                vapid_claims={"sub": "mailto:sure-weather@example.com"},
            )
            sent += 1
        except WebPushException as exc:
            if exc.response and exc.response.status_code in (404, 410):
                _storage.remove_push_subscription(s["endpoint"])
    return {"sent": sent}


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
    # Escape user-controlled label: raw interpolation would allow SVG/XML XSS.
    label = html.escape((name or f"{lat:.4f}, {lon:.4f}")[:34])
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
  <text x="24" y="40" font-family="system-ui,sans-serif" font-size="17" font-weight="700" fill="#ffffff" opacity=".92">{label}</text>
  <text x="24" y="150" font-family="system-ui,sans-serif" font-size="64" font-weight="800" fill="#ffffff">{s["temp"] is not None and f"{s['temp']:.0f}°" or "—"}</text>
  <text x="250" y="86" font-family="system-ui,sans-serif" font-size="44">{icon}</text>
  <text x="24" y="182" font-family="system-ui,sans-serif" font-size="15" fill="#ffffff" opacity=".9">↑{s["tmax"]}° ↓{s["tmin"]}° · ☔ {s["rain_probability"] is not None and f"{s['rain_probability']:.0f}%" or "—"}</text>
  <text x="24" y="204" font-family="system-ui,sans-serif" font-size="12" fill="#ffffff" opacity=".75">{s["wind_kmh"] or "—"} km/h · {sure_txt} · {s["stations"]} station{s["stations"] > 1 and "s" or ""}</text>
</svg>"""
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(
        _web_dir / "manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(_web_dir / "sw.js", media_type="application/javascript")


@app.middleware("http")
async def _security_headers(request, call_next):
    """Content-Security-Policy + no-cache for the service worker script."""
    response = await call_next(request)
    # Leaflet CDN is pinned with SRI in index.html; allow it + fonts + OSM/radar tiles.
    csp = (
        "default-src 'self'; "
        "script-src 'self' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://tile.openstreetmap.org https://*.rainviewer.com; "
        "connect-src 'self' https://geocoding-api.open-meteo.com;"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    if request.url.path == "/sw.js":
        response.headers["Cache-Control"] = "no-cache"
    return response
