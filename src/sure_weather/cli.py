from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from .calibration import compute_residuals
from .collectors import (
    ARCHIVE_MODELS,
    FORECAST_MODELS,
    HIGH_RES_MODELS,
    OpenMeteoCollector,
)
from .collectors.metar import MetarCollector
from .config import load_config
from .grid import cells_in_bbox, cell_from_point, iter_cells_nearby
from .models import ALL_VARIABLES, OBSERVABLE_VARIABLES, Provider
from .storage import Storage
from .service import WeatherService


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
    for m in FORECAST_MODELS + HIGH_RES_MODELS:
        storage.upsert_provider(Provider(name=m, kind="model"))

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
            c, forecast_days=args.days, past_days=args.past_days
        )
        total += storage.insert_forecasts(samples)
        print(f"cell {c.key}: {len(samples)} samples")
    print(f"inserted {total} forecast samples")


def cmd_observe(args: argparse.Namespace) -> None:
    """Fetch historical/analysis data (Open-Meteo archive) for a zone, as ground truth."""
    config = load_config()
    storage = Storage(config.db_path)
    collector = OpenMeteoCollector(config)
    for m in ARCHIVE_MODELS:
        storage.upsert_provider(Provider(name=m, kind="model"))

    lat, lon = _parse_point(args.point)
    cells = [cell_from_point(lat, lon, config)]
    storage.upsert_cells((c.key, c.lat, c.lon) for c in cells)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    total = 0
    for c in cells:
        obs = collector.fetch_historical(c, start, end)
        total += storage.insert_observations(obs)
        print(f"cell {c.key}: {len(obs)} observations")
    print(f"inserted {total} observations")


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Match stored forecasts against stored observations and record residuals."""
    config = load_config()
    storage = Storage(config.db_path)

    # Pull forecasts and observations over the residual window.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

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
    kinds = {p.name: p.kind for p in storage.get_providers()}
    residuals = compute_residuals(forecasts, observations, kinds)
    n = storage.insert_residuals(residuals)
    print(
        f"matched {n} residuals from {len(forecasts)} forecasts x {len(observations)} obs"
    )


def cmd_stations(args: argparse.Namespace) -> None:
    """Discover local METAR stations around a point and ingest their reports.

    Stations are registered as 'station' providers and their recent reports are
    stored as observations. The calibration then prefers these real measurements
    over reanalysis when matching residuals, making the learned bias/rmse and
    the fused confidence more honest for the zone.
    """
    config = load_config()
    storage = Storage(config.db_path)
    collector = MetarCollector(config)
    lat, lon = _parse_point(args.point)

    radius = args.radius or config.obs_search_radius_km
    reports = collector.fetch_observations(lat, lon, radius_km=radius, hours=args.hours)
    stations = {r.icao for r in reports}
    print(f"discovered {len(stations)} stations: {', '.join(sorted(stations)) or 'none'}")

    for p in collector.providers(reports):
        storage.upsert_provider(p)
    cells: dict[str, tuple[float, float]] = {}
    observations = []
    for r in reports:
        obs = collector.to_observations(r, config.cell_resolution)
        observations.extend(obs)
        if obs:
            cells[obs[0].cell_key] = (None, None)  # key already carries coords
    storage.insert_observations(observations)
    print(f"inserted {len(observations)} station observations ({len(reports)} reports)")
    if args.calibrate:
        print("calibrating against station observations...")
        cells_keys = [k for k in cells] or [
            r[0] for r in storage._conn.execute("SELECT DISTINCT cell_key FROM forecasts")
        ]
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.window)
        forecasts = storage.forecasts_in_window(
            cells_keys, list(ALL_VARIABLES), start, end
        )
        observations_all = storage.observations_in_window(
            cells_keys, list(OBSERVABLE_VARIABLES), start, end
        )
        kinds = {p.name: p.kind for p in storage.get_providers()}
        residuals = compute_residuals(forecasts, observations_all, kinds)
        n = storage.insert_residuals(residuals)
        print(f"  {n} residuals")


def cmd_forecast(args: argparse.Namespace) -> None:
    """Print the fused forecast for a point."""
    config = load_config()
    storage = Storage(config.db_path)
    service = WeatherService(storage, config)
    lat, lon = _parse_point(args.point)
    result = service.forecast(lat, lon, hours=args.hours)
    print(json.dumps(result, indent=2))


def cmd_backfill(args: argparse.Namespace) -> None:
    """Backfill cells: 92 days of model analysis + archive, then calibrate.

    Gives ~3 months of residuals per cell, the maximum Open-Meteo exposes for
    past model analysis. This makes learned bias/rmse statistically solid.
    Supports a single point, a bounding box (--bbox) or a radius (--radius).
    """
    config = load_config()
    storage = Storage(config.db_path)
    collector = OpenMeteoCollector(config)
    for m in FORECAST_MODELS + HIGH_RES_MODELS + ARCHIVE_MODELS:
        storage.upsert_provider(Provider(name=m, kind="model"))

    lat, lon = _parse_point(args.point)
    if args.bbox:
        (min_lat, min_lon), (max_lat, max_lon) = (
            _parse_point(args.bbox[0]),
            _parse_point(args.bbox[1]),
        )
        cells = cells_in_bbox(min_lat, max_lat, min_lon, max_lon, config)
    elif args.radius:
        cells = list(iter_cells_nearby(lat, lon, args.radius, config))
    else:
        cells = [cell_from_point(lat, lon, config)]
    storage.upsert_cells((c.key, c.lat, c.lon) for c in cells)
    print(f"backfilling {len(cells)} cells")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=92)

    total_fc = total_obs = 0
    for i, c in enumerate(cells, 1):
        samples = collector.fetch_forecast(c, forecast_days=1, past_days=92)
        total_fc += storage.insert_forecasts(samples)
        obs = collector.fetch_historical(c, start, end)
        total_obs += storage.insert_observations(obs)
        print(f"  [{i}/{len(cells)}] {c.key}: {len(samples)} fc, {len(obs)} obs")
    print(f"inserted {total_fc} forecast samples, {total_obs} observations")

    print("calibrating...")
    cells_all = [
        r[0]
        for r in storage._conn.execute(
            "SELECT DISTINCT cell_key FROM forecasts"
        ).fetchall()
    ]
    forecasts = storage.forecasts_in_window(cells_all, list(ALL_VARIABLES), start, end)
    observations = storage.observations_in_window(
        cells_all, list(OBSERVABLE_VARIABLES), start, end
    )
    kinds = {p.name: p.kind for p in storage.get_providers()}
    residuals = compute_residuals(forecasts, observations, kinds)
    n = storage.insert_residuals(residuals)
    print(f"  {n} residuals")
    print(f"backfill done")


def cmd_verify(args: argparse.Namespace) -> None:
    """Replay fusion on past data and compare empirical hit rate to confidence."""
    config = load_config()
    storage = Storage(config.db_path)
    lat, lon = _parse_point(args.point)
    cell = cell_from_point(lat, lon, config)

    from .verify import report_by_variable, verify

    print("== reliability by confidence band (out-of-sample) ==")
    rep = verify(storage, config, cell.key, split_ratio=args.split)
    for b in rep["bands"]:
        print(
            f"band {b['band']:<12} n={b['n']:<4} "
            f"conf={b['avg_confidence']:.3f} empirical={b['empirical_rate']:.3f} "
            f"gap={b['gap']:+.3f}"
        )

    print("\n== per variable ==")
    for var, s in report_by_variable(
        storage, config, cell.key, split_ratio=args.split
    ).items():
        print(
            f"{var:<28} n={s['n']:<4} conf={s['avg_confidence']:.3f} "
            f"empirical={s['empirical_rate']:.3f} gap={s['gap']:+.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="sure-weather")
    sub = parser.add_subparsers(dest="command", required=True)
    config_defaults = load_config()

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
    p_cal.add_argument(
        "--days",
        type=int,
        default=config_defaults.residual_window_days,
        help="lookback window in days (default: config)",
    )
    p_cal.set_defaults(func=cmd_calibrate)

    p_fc = sub.add_parser("forecast", help="print fused forecast for a point")
    p_fc.add_argument("point", help="lat,lon")
    p_fc.add_argument("--hours", type=int, default=48)
    p_fc.set_defaults(func=cmd_forecast)

    p_st = sub.add_parser(
        "stations",
        help="discover local METAR stations and ingest their observations",
    )
    p_st.add_argument("point", help="lat,lon")
    p_st.add_argument(
        "--radius", type=float, default=0.0, help="search radius in km (default: config)"
    )
    p_st.add_argument(
        "--hours", type=int, default=3, help="hours of reports to fetch per station"
    )
    p_st.add_argument(
        "--calibrate",
        action="store_true",
        help="recompute residuals against station observations",
    )
    p_st.add_argument(
        "--window",
        type=int,
        default=30,
        help="calibration lookback window in days (with --calibrate)",
    )
    p_st.set_defaults(func=cmd_stations)

    p_verify = sub.add_parser(
        "verify",
        help="replay fusion on past data, compare empirical hit rate to confidence",
    )
    p_verify.add_argument("point", help="lat,lon")
    p_verify.add_argument(
        "--split",
        type=float,
        default=0.7,
        help="train/validate split ratio (default 0.7)",
    )
    p_verify.set_defaults(func=cmd_verify)

    p_backfill = sub.add_parser(
        "backfill",
        help="fetch 92d of model analysis + reanalysis for cells, then calibrate",
    )
    p_backfill.add_argument("point", help="lat,lon of the zone center")
    p_backfill.add_argument(
        "--bbox", nargs=2, metavar=("SW", "NE"), help="SW lat,lon NE lat,lon"
    )
    p_backfill.add_argument(
        "--radius", type=float, default=0.0, help="grid radius around the point in km"
    )
    p_backfill.set_defaults(func=cmd_backfill)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
