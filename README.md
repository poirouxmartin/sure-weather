# sure-weather

Multi-source weather fusion with per-provider confidence, targeting hyperlocal nowcasts and J+7 forecasts.

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

- [x] Zero-key collection (Open-Meteo forecast + era5 archive)
- [x] Cell grid, storage, residuals, bias learning
- [x] Robust fusion with per-provider confidence
- [ ] Local station providers (Netatmo / PWS / OpenWeatherMap)
- [ ] Temporal blending (stations dominate short-term, models long-term)
- [ ] Nowcast radar / lightning
- [ ] Frontend (PWA)