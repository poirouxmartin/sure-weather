from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from sure_weather import service as service_module
from sure_weather.service import WeatherService, _clamped_range
from sure_weather.storage import Storage
from sure_weather.config import Config
from sure_weather.models import Cell, ForecastSample


def test_clamped_range_respects_bounds():
    # Humidity tolerance would exceed 100%: the range is clipped.
    low, high = _clamped_range("relative_humidity_2m", 95.0, 10.0)
    assert low == 85.0
    assert high == 100.0
    # Wind cannot be negative.
    low, high = _clamped_range("wind_speed_10m", 0.2, 2.0)
    assert low == 0.0
    # Unbounded variable passes through untouched.
    low, high = _clamped_range("temperature_2m", 20.0, 2.0)
    assert (low, high) == (18.0, 22.0)


def _fresh_service(tmp_path) -> WeatherService:
    cfg = Config(db_path=tmp_path / "test.db")
    return WeatherService(Storage(cfg.db_path), cfg)


def _sample(cell: str, issued: datetime, valid: datetime, value: float = 10.0) -> ForecastSample:
    return ForecastSample(
        provider="gfs_seamless",
        cell_key=cell,
        variable="temperature_2m",
        issued_at=issued,
        valid_at=valid,
        value=value,
    )


def test_refresh_triggers_on_aged_run(tmp_path, monkeypatch):
    """A run that still covers the future but was issued days ago is stale."""
    svc = _fresh_service(tmp_path)
    now = datetime.now(timezone.utc)
    cell = Cell(key="48.9000,2.4000", lat=48.9, lon=2.4)
    svc.storage.upsert_cells([(cell.key, cell.lat, cell.lon)])
    # A 5-day-old run whose 7-day window still reaches into the future.
    old_issue = now - timedelta(days=5)
    svc.storage.insert_forecasts(
        [_sample(cell.key, old_issue, old_issue + timedelta(hours=h)) for h in range(1, 60)]
    )
    called = {}

    class FakeCollector:
        def __init__(self, config):
            pass

        def fetch_forecast(self, cell, **kwargs):
            called["cell"] = cell.key
            return [_sample(cell.key, now, now + timedelta(hours=1))]

    monkeypatch.setattr(service_module, "OpenMeteoCollector", FakeCollector)
    svc._refresh_model_run_if_stale(cell)
    assert called.get("cell") == cell.key


def test_refresh_skips_fresh_run(tmp_path, monkeypatch):
    """A recent 7-day run must not be re-fetched."""
    svc = _fresh_service(tmp_path)
    now = datetime.now(timezone.utc)
    cell = Cell(key="48.9000,2.4000", lat=48.9, lon=2.4)
    svc.storage.upsert_cells([(cell.key, cell.lat, cell.lon)])
    svc.storage.insert_forecasts(
        [_sample(cell.key, now - timedelta(hours=1), now + timedelta(hours=h)) for h in range(1, 200)]
    )
    called = {"n": 0}

    class FakeCollector:
        def __init__(self, config):
            pass

        def fetch_forecast(self, cell, **kwargs):
            called["n"] += 1
            return []

    monkeypatch.setattr(service_module, "OpenMeteoCollector", FakeCollector)
    svc._refresh_model_run_if_stale(cell)
    assert called["n"] == 0


def test_now_endpoint_shape(tmp_path, monkeypatch):
    from sure_weather.api import app, _service

    def fake_forecast(lat, lon, hours=24):
        return {
            "cell": "48.9000,2.4000",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "forecast": [
                {
                    "variable": "temperature_2m",
                    "valid_at": datetime.now(timezone.utc).isoformat(),
                    "value": 21.0,
                    "confidence": 0.99,
                    "calibrated": True,
                    "low": 19.0,
                    "high": 23.0,
                    "sure": True,
                    "contributors": ["gfs_seamless", "metar_LFPO"],
                },
                {
                    "variable": "precipitation",
                    "valid_at": datetime.now(timezone.utc).isoformat(),
                    "value": 0.2,
                    "confidence": 0.9,
                    "calibrated": True,
                    "low": 0.0,
                    "high": 0.7,
                    "sure": False,
                    "contributors": ["gfs_seamless"],
                },
            ],
            "summary": {"horizons": {}},
        }

    monkeypatch.setattr(_service, "forecast", fake_forecast)
    c = TestClient(app)
    r = c.get("/now?lat=48.85&lon=2.35")
    assert r.status_code == 200
    body = r.json()
    assert body["temp"] == 21.0
    assert body["stations"] == 1
    assert body["models"] == 1
    assert body["sky"] in ("day", "night", "rain", "storm", "partly")


def test_widget_svg_renders(tmp_path, monkeypatch):
    from sure_weather.api import app, _service

    monkeypatch.setattr(
        _service,
        "forecast",
        lambda lat, lon, hours=24: {
            "cell": "c",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "forecast": [
                {
                    "variable": "temperature_2m",
                    "valid_at": datetime.now(timezone.utc).isoformat(),
                    "value": 21.0,
                    "confidence": 0.99,
                    "calibrated": True,
                    "low": 19.0,
                    "high": 23.0,
                    "sure": True,
                    "contributors": ["gfs_seamless"],
                }
            ],
            "summary": {"horizons": {}},
        },
    )
    c = TestClient(app)
    r = c.get("/widget.svg?lat=48.85&lon=2.35&name=Paris")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"Paris" in r.content
    assert b"21" in r.content