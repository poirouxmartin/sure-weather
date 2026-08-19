# AGENTS.md — sure-weather

## Description

App météo multi-fournisseurs avec un système de confiance par provider pour une estimation finale précise. Cross-référencement de sources multiples incluant des stations météo locales.

## Stack

- Python 3.12+
- FastAPI + Uvicorn
- httpx
- Pydantic
- NumPy

## Conventions

- `pip install -e ".[dev]"` — install dev
- `pytest` — tests (dossier `tests/`)
- Build : hatchling
- Source : `src/sure_weather/`
