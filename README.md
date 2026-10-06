# sure-weather

Multi-source weather fusion with per-provider confidence, targeting hyperlocal nowcasts and 7-day forecasts.

Project page: [martinpoiroux.com/projets/sure-weather/](https://martinpoiroux.com/projets/sure-weather/)

## Concept

Cross-reference many weather sources (NWP models, local stations) and learn each provider's bias per point. Every prediction is later compared to the actually observed weather, which recalibrates provider weights in a continuous feedback loop.

## Architecture

```
[collect]    fetch forecasts + observations from providers -> SQLite
[observe]    historical reanalysis (era5) as ground truth
[calibrate]  match forecasts to observations -> residuals -> provider bias/rmse
[forecast]   robust consensus over providers, bias-corrected, per (variable, cell, horizon)
[api]        FastAPI /weather?lat=&lon=&hours=
```

The world is divided into learning cells (~0.1°). Each cell accumulates residual history; fusion weights providers by inverse variance learned in that cell.

## Usage

```bash
uv venv
uv pip install -e ".[dev]"

# collect forecasts for a point (zero-key, Open-Meteo)
sure-weather collect 48.8566,2.3522 --past-days 7

# fetch historical ground truth
sure-weather observe 48.8566,2.3522 --days 10

# learn provider bias from residuals
sure-weather calibrate

# print fused forecast
sure-weather forecast 48.8566,2.3522 --hours 48

# serve the API
uvicorn sure_weather.api:app
```

## Tests

```bash
.venv/Scripts/python -m pytest
```

## Status

- [x] Zero-key collection (Open-Meteo forecast + ERA5 archive, METAR observations)
- [x] Cell grid, storage, residuals, bias learning
- [x] Robust fusion with per-provider confidence
- [x] Multi-model providers (GFS, ECMWF, ICON, MetNo, ARPEGE) weighted by learned RMSE
- [x] Installable PWA served by the API (radar, wind grid, push notifications)
- [x] Android shell with a home-screen widget and in-app updates (APK in Releases)
- [ ] Local station providers (Netatmo / PWS)
- [ ] Temporal blending (stations dominate short-term, models long-term)

## License

MIT
