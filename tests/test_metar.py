from datetime import datetime, timezone

from sure_weather.collectors.metar import (
    _decode_cloud_fraction,
    _decode_report,
    _decode_visibility,
    _humidity_from_dewpoint,
)


def _report(**overrides):
    base = {
        "icaoId": "LFPG",
        "obsTime": 1787175000,
        "temp": 17,
        "dewp": 15,
        "wspd": 10,
        "altim": 1008,
        "visib": "6+",
        "cover": "FEW",
        "clouds": [{"cover": "FEW", "base": 1300}],
        "lat": 49.015,
        "lon": 2.534,
        "name": "Paris/De Gaulle Arpt, ID, FR",
    }
    base.update(overrides)
    return base


def test_decode_report_maps_variables():
    obs = _decode_report(_report())
    assert obs is not None
    assert obs.icao == "LFPG"
    assert obs.time == datetime.fromtimestamp(1787175000, tz=timezone.utc)
    assert obs.variables["temperature_2m"] == 17.0
    assert obs.variables["dew_point_2m"] == 15.0
    assert obs.variables["wind_speed_10m"] == round(10 * 0.514444, 2)
    assert obs.variables["pressure_msl"] == 1008.0
    assert obs.variables["visibility"] == 10000.0
    assert obs.variables["cloud_cover"] == 25.0


def test_decode_report_requires_temp_and_time():
    assert _decode_report(_report(temp=None)) is None
    assert _decode_report(_report(obsTime=None)) is None


def test_decode_visibility():
    assert _decode_visibility("6+") == 10000.0
    assert _decode_visibility("9999") == 10000.0
    assert _decode_visibility("2") == round(2 * 1609.344, 0)
    assert _decode_visibility(None) is None
    assert _decode_visibility("xx") is None


def test_decode_cloud_fraction():
    assert _decode_cloud_fraction("CAVOK", []) == 0.0
    assert _decode_cloud_fraction("FEW", [{"cover": "FEW", "base": 1300}]) == 0.25
    assert _decode_cloud_fraction("OVC", [{"cover": "OVC", "base": 500}]) == 1.0
    assert _decode_cloud_fraction(None, []) is None


def test_humidity_from_dewpoint_bounds():
    # temp == dewp => 100% RH
    assert _humidity_from_dewpoint(20.0, 20.0) == 100.0
    rh = _humidity_from_dewpoint(20.0, 5.0)
    assert 0.0 < rh < 100.0