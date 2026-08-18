from sure_weather.config import Config
from sure_weather.grid import (
    cell_from_point,
    cells_in_bbox,
    iter_cells_nearby,
    haversine_km,
)


def test_cell_key_rounds_to_resolution():
    cfg = Config()
    c = cell_from_point(48.8566, 2.3522, cfg)
    assert c.lat == 48.9
    assert c.lon == 2.4


def test_cell_key_rounding_boundary():
    cfg = Config()
    c1 = cell_from_point(48.949, 2.349, cfg)
    c2 = cell_from_point(48.951, 2.351, cfg)
    # 48.949 rounds to 48.9; 48.951 rounds to 49.0 (banker's rounding applies).
    assert c1.key != c2.key


def test_cells_in_bbox():
    cfg = Config()
    cells = cells_in_bbox(48.0, 48.2, 2.0, 2.2, cfg)
    assert len(cells) == 9  # 3x3 at 0.1 resolution


def test_haversine_paris_london():
    d = haversine_km(48.8566, 2.3522, 51.5074, -0.1278)
    assert 330 < d < 350


def test_iter_cells_nearby_includes_center():
    cfg = Config()
    cells = list(iter_cells_nearby(48.8566, 2.3522, 30.0, cfg))
    assert cells[0].key == cell_from_point(48.8566, 2.3522, cfg).key
    # All yielded cells should be within the radius.
    for c in cells:
        assert haversine_km(48.8566, 2.3522, c.lat, c.lon) <= 30.0
