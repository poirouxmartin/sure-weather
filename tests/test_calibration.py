from datetime import datetime, timedelta, timezone

from sure_weather.calibration import compute_residuals, learn_stats
from sure_weather.config import Config
from sure_weather.models import ForecastSample, Observation
from sure_weather.storage import Storage

CELL = "48.9,2.4"
NOW = datetime.now(timezone.utc)


def _fc(provider, value, valid_at):
    return ForecastSample(
        provider=provider,
        cell_key=CELL,
        variable="temperature_2m",
        issued_at=NOW - timedelta(hours=1),
        valid_at=valid_at,
        value=value,
    )


def _obs(provider, value, at):
    return Observation(
        provider=provider,
        cell_key=CELL,
        variable="temperature_2m",
        time=at,
        value=value,
    )


def test_compute_residuals_matches_forecast_to_observation(tmp_path):
    valid = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    forecasts = [_fc("model_a", 12.0, valid)]
    observations = [_obs("model_a", 10.0, valid)]
    residuals = compute_residuals(forecasts, observations)
    assert len(residuals) == 1
    assert residuals[0].bias == 2.0
    assert residuals[0].abs_error == 2.0


def test_compute_residuals_skips_unmatched():
    valid = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    forecasts = [_fc("model_a", 12.0, valid)]
    residuals = compute_residuals(forecasts, [])  # no observations
    assert residuals == []


def test_learn_stats_aggregates():
    valid = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    forecasts = [
        _fc("model_a", 12.0, valid),
        _fc("model_a", 10.0, valid + timedelta(hours=1)),
    ]
    observations = [
        _obs("model_a", 10.0, valid),
        _obs("model_a", 11.0, valid + timedelta(hours=1)),
    ]
    residuals = compute_residuals(forecasts, observations)
    stats = learn_stats(residuals)
    assert len(stats) == 1
    s = stats[0]
    assert s.samples == 2
    assert abs(s.bias - 0.5) < 1e-9  # mean of (2.0, -1.0)
    assert s.rmse > 0


def test_storage_roundtrip(tmp_path):
    storage = Storage(tmp_path / "test.db")
    from sure_weather.models import Provider

    storage.upsert_provider(Provider(name="model_a", kind="model"))
    valid = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    storage.insert_forecasts([_fc("model_a", 12.0, valid)])
    cells = [CELL]
    samples = storage.latest_forecasts(cells, ["temperature_2m"], ["model_a"])
    assert len(samples) == 1
    assert samples[0].value == 12.0
    storage.close()


def test_samples_from_single_model_uses_unsuffixed_keys():
    from sure_weather.collectors.open_meteo import OpenMeteoCollector
    from sure_weather.models import Cell

    data = {
        "hourly": {
            "time": ["2026-08-19T10:00", "2026-08-19T11:00"],
            "temperature_2m": [20.5, 21.0],
            "cloud_cover": [10, 15],
        }
    }
    coll = OpenMeteoCollector(Config())
    cell = Cell("48.9,2.4", 48.9, 2.4)
    samples = coll._samples_from(data, ["meteofrance_arome_france"], cell)
    vars_found = {s.variable for s in samples}
    assert {"temperature_2m", "cloud_cover"} <= vars_found
    assert all(s.provider == "meteofrance_arome_france" for s in samples)
    assert all(s.cell_key == "48.9,2.4" for s in samples)
