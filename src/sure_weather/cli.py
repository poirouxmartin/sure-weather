from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from .calibration import compute_residuals
from .collectors import OpenMeteoCollector
from .config import load_config
from .grid import cells_in_bbox, cell_from_point
from .models import ALL_VARIABLES, OBSERVABLE_VARIABLES, Provider
from .storage import Storage
from .service import WeatherService

DEFAULT_MODEL = "gfs_seamless"


def _parse_point(text: str) -> tuple[float, float]:
    parts = text.split(",")
    if len(parts) != 2:
        raise SystemExit("point must be 'lat,lon'")
    return float(parts[0]), float(parts[1])


def cmd_collect(args: argparse.Namespace) -> None:
    """Fetch the latest forecast for a zone (default: a small box around the point)."""
    config = load_config()
    storage = Storage(config.db_path)
    collector = OpenMeteoCollector(config)
    storage.upsert_provider(Provider(name=DEFAULT_MODEL, kind="model"))

    lat, lon = _parse_point(args.point)
    if args.bbox:
        (min_lat, min_lon), (max_lat, max_lon) = (
            _parse_point(args.bbox[0]),
            _parse_point(args.bbox[1]),
        )
        cells = cells_in_bbox(min_lat, max_lat, min_lon, max_lon, config)
    else:
        cells = [cell_from_point(lat, lon, config)]
    storage.upsert_cells((c.key, c.lat, c.lon) for c in cells)

    total = 0
    for c in cells:
        samples = collector.fetch_forecast(
            c, model=DEFAULT_MODEL, forecast_days=args.days, past_days=args.past_days
        )
        total += storage.insert_forecasts(samples)
        print(f"cell {c.key}: {len(samples)} samples")
    print(f"inserted {total} forecast samples")


def cmd_observe(args: argparse.Namespace) -> None:
    """Fetch historical/analysis data (Open-Meteo archive) for a zone, as ground truth."""
    config = load_config()
    storage = Storage(config.db_path)
    collector = OpenMeteoCollector(config)
    storage.upsert_provider(Provider(name=DEFAULT_MODEL, kind="model"))

    lat, lon = _parse_point(args.point)
    cells = [cell_from_point(lat, lon, config)]
    storage.upsert_cells((c.key, c.lat, c.lon) for c in cells)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    total = 0
    for c in cells:
        obs = collector.fetch_historical(c, start, end, model=DEFAULT_MODEL)
        total += storage.insert_observations(obs)
        print(f"cell {c.key}: {len(obs)} observations")
    print(f"inserted {total} observations")


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Match stored forecasts against stored observations and record residuals."""
    config = load_config()
    storage = Storage(config.db_path)

    # Pull forecasts and observations over the residual window.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=config.residual_window_days)

    # Gather cells that have data.
    cells = [
        r[0]
        for r in storage._conn.execute(
            "SELECT DISTINCT cell_key FROM forecasts"
        ).fetchall()
    ]
    forecasts = storage.forecasts_in_window(cells, list(ALL_VARIABLES), start, end)
    observations = storage.observations_in_window(
        cells, list(OBSERVABLE_VARIABLES), start, end
    )
    residuals = compute_residuals(forecasts, observations)
    n = storage.insert_residuals(residuals)
    print(
        f"matched {n} residuals from {len(forecasts)} forecasts x {len(observations)} obs"
    )


def cmd_forecast(args: argparse.Namespace) -> None:
    """Print the fused forecast for a point."""
    config = load_config()
    storage = Storage(config.db_path)
    service = WeatherService(storage, config)
    lat, lon = _parse_point(args.point)
    result = service.forecast(lat, lon, hours=args.hours)
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="sure-weather")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="fetch latest forecast for a zone")
    p_collect.add_argument("point", help="lat,lon of the zone center")
    p_collect.add_argument(
        "--bbox", nargs=2, metavar=("SW", "NE"), help="SW lat,lon NE lat,lon"
    )
    p_collect.add_argument("--days", type=int, default=7)
    p_collect.add_argument(
        "--past-days",
        type=int,
        default=1,
        help="include model analysis for past N days",
    )
    p_collect.set_defaults(func=cmd_collect)

    p_obs = sub.add_parser("observe", help="fetch historical analysis as ground truth")
    p_obs.add_argument("point", help="lat,lon")
    p_obs.add_argument("--days", type=int, default=30)
    p_obs.set_defaults(func=cmd_observe)

    p_cal = sub.add_parser(
        "calibrate", help="compute residuals forecast vs observation"
    )
    p_cal.set_defaults(func=cmd_calibrate)

    p_fc = sub.add_parser("forecast", help="print fused forecast for a point")
    p_fc.add_argument("point", help="lat,lon")
    p_fc.add_argument("--hours", type=int, default=48)
    p_fc.set_defaults(func=cmd_forecast)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
