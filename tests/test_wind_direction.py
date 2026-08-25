import numpy as np
import pytest
from datetime import datetime, timezone

from sure_weather.fusion import _circular_mean, _fuse_one
from sure_weather.models import ForecastSample
from sure_weather.config import Config


CFG = Config()
ISS = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
VAL = datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc)


def _dir_samples(values):
    return [
        ForecastSample(
            provider="gfs_seamless", cell_key="c", variable="wind_direction_10m",
            issued_at=ISS, valid_at=VAL, value=v,
        )
        for v in values
    ]


def test_circular_mean_wraps_north():
    # 350° and 10° average to ~0°, never 180°.
    assert _circular_mean(np.array([350.0, 10.0]), np.array([1.0, 1.0])) == 0.0


def test_circular_mean_basic():
    assert round(_circular_mean(np.array([90.0, 90.0]), np.array([1.0, 1.0])), 1) == 90.0


def test_direction_fusion_stays_on_circle():
    res = _fuse_one(_dir_samples([350.0, 10.0, 5.0]), {}, {}, CFG, VAL)
    assert 0 <= res.consensus <= 20
    assert not res.bias_corrected  # angular bias correction is meaningless


def test_direction_fusion_bounds():
    res = _fuse_one(_dir_samples([200.0, 210.0]), {}, {}, CFG, VAL)
    assert 0 <= res.consensus <= 360
    assert res.dispersion >= 0