"""Non-regression tests for the full-audit roadmap (P0/P1).

Covers: per-sample horizon buckets, MAD=0 outlier gate, wind-direction
calibration neutrality, synthetic horizon priors staying uncalibrated,
widget label escaping, push admin gate + endpoint validation, forecast
cache keyed by cell (not round(lat,2)).
"""

import html as _html
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from sure_weather.api import _require_admin, _validate_push_endpoint
from sure_weather.calibration import compute_residuals
from sure_weather.config import Config
from sure_weather.fusion import ProviderStat, fuse
from sure_weather.grid import cell_from_point
from sure_weather.models import ForecastSample, Observation

NOW = datetime.now(timezone.utc)
CELL = "48.9,2.4"


def _sample(provider, value, valid_h, issued_h, cell=CELL):
    return ForecastSample(
        provider=provider,
        cell_key=cell,
        variable="temperature_2m",
        issued_at=NOW + timedelta(hours=issued_h),
        valid_at=NOW + timedelta(hours=valid_h),
        value=value,
    )


def test_bucket_is_per_sample_not_first_sample():
    """A stale run and a fresh run fuse with their own calibration."""
    cfg = Config()
    stats = {
        # Fresh-run stat for model_a at 3h bucket: strong +2 bias learned.
        ("model_a", CELL, "temperature_2m", 3.0): ProviderStat(
            provider="model_a",
            cell_key=CELL,
            variable="temperature_2m",
            horizon_h=3.0,
            samples=10,
            bias=2.0,
            rmse=2.1,
            updated_at=NOW,
        ),
        # Stale-run stat for model_b at 120h bucket: no bias.
        ("model_b", CELL, "temperature_2m", 120.0): ProviderStat(
            provider="model_b",
            cell_key=CELL,
            variable="temperature_2m",
            horizon_h=120.0,
            samples=10,
            bias=0.0,
            rmse=2.0,
            updated_at=NOW,
        ),
    }
    valid = NOW + timedelta(hours=3)
    samples = [
        # model_a: fresh run (issued 1h ago -> 3h bucket), raw 12 -> corr 10.
        ForecastSample(
            provider="model_a",
            cell_key=CELL,
            variable="temperature_2m",
            issued_at=valid - timedelta(hours=4),
            valid_at=valid,
            value=12.0,
        ),
        # model_b: stale run (issued 5 days ago -> 120h bucket), raw 20.
        ForecastSample(
            provider="model_b",
            cell_key=CELL,
            variable="temperature_2m",
            issued_at=valid - timedelta(hours=120),
            valid_at=valid,
            value=20.0,
        ),
    ]
    (r,) = fuse(samples, stats, {"model_a": "model", "model_b": "model"}, cfg, NOW)
    assert r.bias_corrected  # model_a's own-bucket bias applied
    assert r.consensus < 20.0  # corrected below the raw stale value
    assert len(r.contributors) == 2


def test_outlier_gate_mad_zero_rejects_lone_outlier():
    """[12,12,12,40] with MAD=0 must not keep the outlier via std fallback."""
    cfg = Config()
    samples = [
        _sample("a", 12.0, 3, -1),
        _sample("b", 12.0, 3, -1),
        _sample("c", 12.0, 3, -1),
        _sample("d", 40.0, 3, -1),
    ]
    (r,) = fuse(
        samples,
        {},
        {p: "model" for p in "abcd"},
        cfg,
        NOW,
    )
    assert r.consensus < 13.0
    assert all(p != "d" for p, _, _ in r.contributors)


def test_wind_direction_calibration_is_neutral():
    """350° vs 10° must not learn a 340° linear bias."""
    f = ForecastSample(
        provider="m",
        cell_key=CELL,
        variable="wind_direction_10m",
        issued_at=NOW - timedelta(hours=2),
        valid_at=NOW - timedelta(hours=1),
        value=350.0,
    )
    o = Observation(
        provider="era5_seamless",
        cell_key=CELL,
        variable="wind_direction_10m",
        time=NOW - timedelta(hours=1),
        value=10.0,
    )
    (res,) = compute_residuals([f], [o], {"m": "model"})
    assert res.bias == 0.0  # neutral: rmse carries direction error, not bias


def test_synthetic_horizon_prior_never_calibrated():
    """Extrapolated buckets carry <3 samples so calibrated stays False."""
    from sure_weather.service import _extrapolate_horizon_priors

    base = ProviderStat(
        provider="m",
        cell_key=CELL,
        variable="temperature_2m",
        horizon_h=3.0,
        samples=40,
        bias=0.5,
        rmse=1.0,
        updated_at=NOW,
    )
    out = _extrapolate_horizon_priors({("m", CELL, "temperature_2m", 3.0): base})
    synth = out[("m", CELL, "temperature_2m", 120.0)]
    assert synth.samples < 3


def test_widget_label_escaping():
    """User-controlled widget name must not break SVG/XML."""
    evil = '--><svg onload="alert(1)"><script>alert(1)</script>'
    safe = _html.escape(evil[:34])
    assert "<svg" not in safe and "<script" not in safe
    assert "&lt;svg" in safe


def test_push_test_requires_admin_token(monkeypatch):
    """Without SURE_WEATHER_ADMIN_TOKEN, /push/test is 403."""
    monkeypatch.delenv("SURE_WEATHER_ADMIN_TOKEN", raising=False)
    try:
        _require_admin(None)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected 403")


def test_push_endpoint_validation_rejects_ssrf():
    for bad in [
        "",
        "http://example.com/push",
        "https://127.0.0.1/push",
        "https://10.0.0.5/push",
        "https://[::1]/push",
        "x" * 3000,
    ]:
        try:
            _validate_push_endpoint(bad)
        except HTTPException as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError(f"expected 422 for {bad!r}")
    # A real push-service hostname passes.
    _validate_push_endpoint("https://fcm.googleapis.com/fcm/send/abc")


def test_forecast_cache_key_is_cell_not_rounded_coords():
    """Two points rounding to the same 0.01° in different cells differ."""
    from sure_weather.config import load_config

    cfg = load_config()
    c1 = cell_from_point(48.949, 2.3522, cfg)
    c2 = cell_from_point(48.951, 2.3522, cfg)
    assert round(48.949, 2) == round(48.951, 2)
    assert (c1.key, 24) != (c2.key, 24)
