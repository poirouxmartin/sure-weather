from datetime import datetime, timedelta, timezone

from sure_weather.config import Config
from sure_weather.fusion import (
    ProviderStat,
    fuse,
    horizon_bucket,
)
from sure_weather.models import ForecastSample

NOW = datetime.now(timezone.utc)
CELL = "48.9,2.4"


def _sample(provider, value, valid_offset_h, issued_offset_h=-1):
    return ForecastSample(
        provider=provider,
        cell_key=CELL,
        variable="temperature_2m",
        issued_at=NOW + timedelta(hours=issued_offset_h),
        valid_at=NOW + timedelta(hours=valid_offset_h),
        value=value,
    )


def test_fuse_single_provider():
    cfg = Config()
    samples = [_sample("model_a", 12.0, 3)]
    results = fuse(samples, {}, {"model_a": "model"}, cfg, NOW)
    assert len(results) == 1
    r = results[0]
    assert r.consensus == 12.0
    assert 0 < r.confidence <= 1


def test_fuse_outlier_rejected():
    cfg = Config()
    samples = [
        _sample("model_a", 12.0, 3),
        _sample("model_b", 12.2, 3),
        _sample("model_c", 11.8, 3),
        _sample("model_d", 40.0, 3),  # gross outlier
    ]
    results = fuse(
        samples,
        {},
        {
            "model_a": "model",
            "model_b": "model",
            "model_c": "model",
            "model_d": "model",
        },
        cfg,
        NOW,
    )
    r = results[0]
    assert r.consensus < 13.0  # outlier pulled out
    assert len(r.contributors) <= 3


def test_fuse_station_weighted_higher():
    cfg = Config()
    samples = [
        _sample("model_a", 12.0, 3),
        _sample("station_x", 13.0, 3),
    ]
    results = fuse(
        samples,
        {},
        {"model_a": "model", "station_x": "station"},
        cfg,
        NOW,
    )
    r = results[0]
    # Station weight (2.0) > model (1.0), so consensus leans toward 13.0.
    assert r.consensus > 12.5
    assert r.confidence > 0.7  # kind coverage bonus


def test_bias_correction_applied():
    cfg = Config()
    stat = ProviderStat(
        provider="model_a",
        cell_key=CELL,
        variable="temperature_2m",
        horizon_h=horizon_bucket(NOW + timedelta(hours=3), NOW),
        samples=50,
        bias=1.5,  # model_a consistently predicts 1.5 too high
        rmse=1.8,
        updated_at=NOW,
    )
    stats = {(stat.provider, stat.cell_key, stat.variable, stat.horizon_h): stat}
    samples = [_sample("model_a", 12.0, 3)]
    results = fuse(samples, stats, {"model_a": "model"}, cfg, NOW)
    r = results[0]
    assert r.bias_corrected
    assert abs(r.consensus - 10.5) < 1e-6


def test_horizon_buckets():
    assert horizon_bucket(NOW + timedelta(hours=2), NOW) == 3.0
    assert horizon_bucket(NOW + timedelta(hours=12), NOW) == 12.0
    assert horizon_bucket(NOW + timedelta(hours=48), NOW) == 48.0
    assert horizon_bucket(NOW + timedelta(hours=120), NOW) == 120.0
