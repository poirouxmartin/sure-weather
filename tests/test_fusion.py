from datetime import datetime, timedelta, timezone

from sure_weather.config import Config
from sure_weather.fusion import (
    ProviderStat,
    fuse,
    horizon_bucket,
    spatial_stats,
)
from sure_weather.models import Cell, ForecastSample

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


def _sample_var(provider, variable, value, valid_offset_h, issued_offset_h=-1):
    return ForecastSample(
        provider=provider,
        cell_key=CELL,
        variable=variable,
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


def test_multi_model_weights_by_learned_rmse():
    cfg = Config()
    bucket = horizon_bucket(NOW + timedelta(hours=3), NOW)
    good = ProviderStat("ecmwf", CELL, "temperature_2m", bucket, 100, 0.1, 0.5, NOW)
    bad = ProviderStat("gfs", CELL, "temperature_2m", bucket, 100, 0.1, 3.0, NOW)
    stats = {
        ("ecmwf", CELL, "temperature_2m", bucket): good,
        ("gfs", CELL, "temperature_2m", bucket): bad,
    }
    samples = [
        _sample("ecmwf", 26.0, 3),
        _sample("gfs", 24.0, 3),
    ]
    results = fuse(samples, stats, {"ecmwf": "model", "gfs": "model"}, cfg, NOW)
    r = results[0]
    # ecmwf (rmse 0.5) should dominate over gfs (rmse 3.0).
    assert r.consensus > 25.5
    weights = {p: w for p, _, w in r.contributors}
    assert weights["ecmwf"] > weights["gfs"] * 10


def test_spatial_stats_borrows_from_neighbors():
    cfg = Config()
    bucket = 3.0
    cells = [Cell("48.8,2.4", 48.8, 2.4), Cell("48.9,2.4", 48.9, 2.4)]
    src = ProviderStat("ecmwf", "48.8,2.4", "temperature_2m", bucket, 200, 0.8, 1.2, NOW)
    stats = {("ecmwf", "48.8,2.4", "temperature_2m", bucket): src}
    out = spatial_stats(stats, cells, sigma_km=30.0)
    borrowed = out.get(("ecmwf", "48.9,2.4", "temperature_2m", bucket))
    assert borrowed is not None
    assert borrowed.samples >= 1
    assert abs(borrowed.bias - src.bias) < 1e-6
    # A distant cell borrows almost nothing.
    far = Cell("50.9,4.4", 50.9, 4.4)
    out2 = spatial_stats(stats, cells + [far], sigma_km=30.0)
    assert out2.get(("ecmwf", "50.9,4.4", "temperature_2m", bucket)) is None


def test_consensus_clipped_to_physical_bounds():
    cfg = Config()
    samples = [
        _sample_var("model_a", "cloud_cover", 101.9, 3),
        _sample_var("model_b", "cloud_cover", 102.0, 3),
    ]
    results = fuse(
        samples, {}, {"model_a": "model", "model_b": "model"}, cfg, NOW
    )
    assert results[0].consensus <= 100.0


def test_visibility_confidence_within_reasonable_range():
    """Visibility values are ~70 km; a 5 m tolerance would give ~0 confidence.

    The meteorologically defensible ±5 km tolerance must produce a usable,
    non-degenerate confidence for large visibility values.
    """
    cfg = Config()
    samples = [
        _sample_var("model_a", "visibility", 30000.0, 3),
        _sample_var("model_b", "visibility", 32000.0, 3),
    ]
    results = fuse(
        samples, {}, {"model_a": "model", "model_b": "model"}, cfg, NOW
    )
    r = results[0]
    assert r.variable == "visibility"
    assert r.confidence > 0.1  # would be ~0 with the old 5 m tolerance
    assert r.consensus > 20000.0
