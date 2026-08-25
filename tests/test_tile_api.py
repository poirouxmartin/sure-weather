import struct

import pytest
from fastapi.testclient import TestClient

from sure_weather.api import app


def _png_dims(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_tile_temp_endpoint():
    c = TestClient(app)
    r = c.get("/tile/temp/9/259/176.png")
    if r.status_code == 502:
        pytest.skip("upstream rate-limited")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert "max-age=600" in r.headers["cache-control"]
    assert _png_dims(r.content) == (256, 256)


def test_tile_precip_endpoint():
    c = TestClient(app)
    r = c.get("/tile/precip/9/259/176.png")
    if r.status_code == 502:
        pytest.skip("upstream rate-limited")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert _png_dims(r.content) == (256, 256)


def test_tile_future_hour():
    c = TestClient(app)
    r = c.get("/tile/precip/9/259/176.png?h=3")
    if r.status_code == 502:
        pytest.skip("upstream rate-limited")
    assert r.status_code == 200
    assert _png_dims(r.content) == (256, 256)


def test_tile_temp_rejects_hour_offset():
    c = TestClient(app)
    assert c.get("/tile/temp/9/259/176.png?h=3").status_code == 422


def test_tile_unknown_layer():
    c = TestClient(app)
    assert c.get("/tile/clouds/9/259/176.png").status_code == 404


def test_tile_out_of_range():
    c = TestClient(app)
    assert c.get("/tile/temp/15/0/0.png").status_code == 404
    assert c.get("/tile/temp/3/999/0.png").status_code == 404
