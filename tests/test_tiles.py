import struct

import numpy as np

from sure_weather.tiles import (
    encode_png_rgba,
    precipitation_overlay,
    temperature_overlay,
    tile_bounds,
)


def test_tile_bounds_z0_covers_world():
    lon_w, lat_s, lon_e, lat_n = tile_bounds(0, 0, 0)
    assert lon_w == -180.0
    assert lon_e == 180.0
    assert abs(lat_n - 85.05) < 0.01
    assert abs(lat_s + 85.05) < 0.01


def test_tile_bounds_paris_region():
    # Tile covering ~Paris at z9: west longitude 2.109, north latitude ~48.92.
    lon_w, lat_s, lon_e, lat_n = tile_bounds(9, 259, 176)
    assert 2.0 < lon_w < 2.2 < lon_e < 3.0
    assert 48.0 < lat_s < 48.5 < lat_n < 49.0


def test_png_encoder_produces_valid_png():
    px = np.zeros((16, 16, 4), dtype=np.uint8)
    px[..., 3] = 255
    px[4:8, 4:8] = [200, 60, 60, 255]
    data = encode_png_rgba(px)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == (16, 16)
    assert b"IHDR" in data and b"IDAT" in data and b"IEND" in data


def test_temperature_overlay_color_and_alpha():
    grid = np.array([[0.0, 20.0], [35.0, np.nan]])
    out = temperature_overlay(grid)
    assert out.shape == (2, 2, 4)
    # NaN (no data) must be fully transparent.
    assert out[1, 1, 3] == 0
    # Cold is bluer than warm: blue channel falls from 0° to 35°.
    assert out[0, 0, 2] > out[1, 0, 2]
    # Red channel rises with temperature.
    assert out[0, 0, 0] < out[1, 0, 0]


def test_precipitation_overlay_transparent_when_dry():
    grid = np.array([[0.0, 0.0], [5.0, np.nan]])
    out = precipitation_overlay(grid)
    assert out.shape == (2, 2, 4)
    assert out[0, 0, 3] == 0  # dry → transparent
    assert out[0, 1, 3] == 0
    assert out[1, 0, 3] > 0  # rainy → visible
    assert out[1, 1, 3] == 0  # no data → transparent