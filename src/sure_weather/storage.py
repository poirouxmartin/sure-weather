from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Cell, ForecastSample, Observation, Provider, Residual

_SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    name  TEXT PRIMARY KEY,
    kind  TEXT NOT NULL CHECK (kind IN ('model', 'station'))
);

CREATE TABLE IF NOT EXISTS cells (
    key TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    provider  TEXT NOT NULL,
    cell_key  TEXT NOT NULL,
    variable  TEXT NOT NULL,
    time      TEXT NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (provider, cell_key, variable, time),
    FOREIGN KEY (provider) REFERENCES providers(name)
);
CREATE INDEX IF NOT EXISTS idx_obs_time  ON observations(time);
CREATE INDEX IF NOT EXISTS idx_obs_cell  ON observations(cell_key, variable, time);

CREATE TABLE IF NOT EXISTS forecasts (
    provider  TEXT NOT NULL,
    cell_key  TEXT NOT NULL,
    variable  TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    valid_at  TEXT NOT NULL,
    value     REAL NOT NULL,
    PRIMARY KEY (provider, cell_key, variable, issued_at, valid_at),
    FOREIGN KEY (provider) REFERENCES providers(name)
);
CREATE INDEX IF NOT EXISTS idx_fc_valid ON forecasts(valid_at);
CREATE INDEX IF NOT EXISTS idx_fc_cell  ON forecasts(cell_key, variable, issued_at);

CREATE TABLE IF NOT EXISTS residuals (
    provider   TEXT NOT NULL,
    cell_key   TEXT NOT NULL,
    variable   TEXT NOT NULL,
    valid_at   TEXT NOT NULL,
    horizon_h  REAL NOT NULL,
    predicted  REAL NOT NULL,
    observed   REAL NOT NULL,
    PRIMARY KEY (provider, cell_key, variable, valid_at)
);
CREATE INDEX IF NOT EXISTS idx_res_provider ON residuals(provider, cell_key, variable, valid_at);
CREATE INDEX IF NOT EXISTS idx_res_valid ON residuals(valid_at);
"""


def _parse_dt(text: str) -> datetime:
    """Parse an ISO timestamp, normalizing naive values to UTC."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        # Read-path tuning: a bigger page cache and memory-mapped I/O keep
        # the hot queries (latest run per cell, residual aggregation) off the
        # OS page cache round-trips.
        self._conn.execute("PRAGMA cache_size=-16000;")  # ~16 MB
        self._conn.execute("PRAGMA mmap_size=268435456;")  # 256 MB
        self._conn.execute("PRAGMA temp_store=MEMORY;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- providers ----

    def upsert_provider(self, provider: Provider) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO providers(name, kind) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind",
                (provider.name, provider.kind),
            )

    def get_providers(self) -> list[Provider]:
        rows = self._conn.execute("SELECT name, kind FROM providers").fetchall()
        return [Provider(name=r[0], kind=r[1]) for r in rows]

    def get_providers_by_kind(self, kind: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM providers WHERE kind=?", (kind,)
        ).fetchall()
        return [r[0] for r in rows]

    # ---- cells ----

    def upsert_cells(self, cells: Iterable[tuple[str, float, float]]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT INTO cells(key, lat, lon) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                cells,
            )

    def get_cells(self) -> list[Cell]:
        rows = self._conn.execute("SELECT key, lat, lon FROM cells").fetchall()
        return [Cell(key=r[0], lat=r[1], lon=r[2]) for r in rows]

    # ---- observations ----

    def insert_observations(self, obs: Iterable[Observation]) -> int:
        rows = [
            (o.provider, o.cell_key, o.variable, o.time.isoformat(), o.value)
            for o in obs
        ]
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO observations"
                "(provider, cell_key, variable, time, value) VALUES(?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def observations_in_window(
        self,
        cells: Iterable[str],
        variables: Iterable[str],
        since: datetime,
        until: datetime | None = None,
    ) -> list[Observation]:
        cell_list = list(cells)
        var_list = list(variables)
        if not cell_list or not var_list:
            return []
        placeholders = ",".join("?" * len(cell_list))
        vplaceholders = ",".join("?" * len(var_list))
        sql = (
            "SELECT provider, cell_key, variable, time, value FROM observations "
            f"WHERE cell_key IN ({placeholders}) AND variable IN ({vplaceholders}) "
            "AND time >= ?"
        )
        params: list[Any] = cell_list + var_list + [since.isoformat()]
        if until is not None:
            sql += " AND time <= ?"
            params.append(until.isoformat())
        rows = self._conn.execute(sql, params).fetchall()
        return [
            Observation(
                provider=r[0],
                cell_key=r[1],
                variable=r[2],
                time=_parse_dt(r[3]),
                value=r[4],
            )
            for r in rows
        ]

    # ---- forecasts ----

    def insert_forecasts(self, samples: Iterable[ForecastSample]) -> int:
        rows = [
            (
                s.provider,
                s.cell_key,
                s.variable,
                s.issued_at.isoformat(),
                s.valid_at.isoformat(),
                s.value,
            )
            for s in samples
        ]
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO forecasts"
                "(provider, cell_key, variable, issued_at, valid_at, value) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def forecasts_in_window(
        self,
        cells: Iterable[str],
        variables: Iterable[str],
        since: datetime,
        until: datetime | None = None,
    ) -> list[ForecastSample]:
        """All forecasts whose valid time falls in [since, until], regardless of issue."""
        cell_list = list(cells)
        var_list = list(variables)
        if not cell_list or not var_list:
            return []
        placeholders = ",".join("?" * len(cell_list))
        vplaceholders = ",".join("?" * len(var_list))
        sql = (
            "SELECT provider, cell_key, variable, issued_at, valid_at, value "
            "FROM forecasts "
            f"WHERE cell_key IN ({placeholders}) AND variable IN ({vplaceholders}) "
            "AND valid_at >= ?"
        )
        params: list[Any] = cell_list + var_list + [since.isoformat()]
        if until is not None:
            sql += " AND valid_at <= ?"
            params.append(until.isoformat())
        rows = self._conn.execute(sql, params).fetchall()
        return [
            ForecastSample(
                provider=r[0],
                cell_key=r[1],
                variable=r[2],
                issued_at=_parse_dt(r[3]),
                valid_at=_parse_dt(r[4]),
                value=r[5],
            )
            for r in rows
        ]

    def latest_forecasts(
        self,
        cells: Iterable[str],
        variables: Iterable[str],
        providers: Iterable[str] | None = None,
    ) -> list[ForecastSample]:
        """Most recent issue for each (provider, cell, variable), all valid times."""
        cell_list = list(cells)
        var_list = list(variables)
        if not cell_list or not var_list:
            return []
        placeholders = ",".join("?" * len(cell_list))
        vplaceholders = ",".join("?" * len(var_list))
        params: list[Any] = cell_list + var_list
        provider_clause = ""
        provider_list = list(providers) if providers else []
        if provider_list:
            pplaceholders = ",".join("?" * len(provider_list))
            provider_clause = f"AND provider IN ({pplaceholders})"
            params += provider_list
        sql = (
            "SELECT provider, cell_key, variable, issued_at, valid_at, value "
            "FROM forecasts f "
            f"WHERE cell_key IN ({placeholders}) AND variable IN ({vplaceholders}) "
            f"{provider_clause} "
            "AND issued_at = (SELECT MAX(issued_at) FROM forecasts f2 "
            "  WHERE f2.provider = f.provider AND f2.cell_key = f.cell_key "
            "  AND f2.variable = f.variable)"
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [
            ForecastSample(
                provider=r[0],
                cell_key=r[1],
                variable=r[2],
                issued_at=_parse_dt(r[3]),
                valid_at=_parse_dt(r[4]),
                value=r[5],
            )
            for r in rows
        ]

    # ---- residuals ----

    def residual_bias_stats(
        self, since: datetime
    ) -> list[tuple[str, str, str, float, int, float, float]]:
        """SQL-side aggregation of residual biases.

        Groups the whole residual window in one pass inside SQLite (C speed)
        instead of materializing millions of Residual objects in Python:
        per (provider, cell, variable, horizon) it returns sample count, mean
        bias and mean squared bias (rmse is the square root of that).
        """
        sql = (
            "SELECT provider, cell_key, variable, horizon_h, COUNT(*), "
            "AVG(predicted - observed), "
            "AVG((predicted - observed) * (predicted - observed)) "
            "FROM residuals WHERE valid_at >= ? "
            "GROUP BY provider, cell_key, variable, horizon_h"
        )
        return self._conn.execute(sql, (since.isoformat(),)).fetchall()

    def insert_residuals(self, residuals: Iterable[Residual]) -> int:
        rows = [
            (
                r.provider,
                r.cell_key,
                r.variable,
                r.valid_at.isoformat(),
                r.horizon_h,
                r.predicted,
                r.observed,
            )
            for r in residuals
        ]
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO residuals"
                "(provider, cell_key, variable, valid_at, horizon_h, predicted, observed) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def residuals_window(
        self,
        providers: Iterable[str] | None,
        variables: Iterable[str] | None,
        since: datetime,
        until: datetime | None = None,
    ) -> list[Residual]:
        sql = "SELECT provider, cell_key, variable, valid_at, horizon_h, predicted, observed FROM residuals WHERE valid_at >= ?"
        params: list[Any] = [since.isoformat()]
        provider_list = list(providers) if providers else []
        if provider_list:
            pplaceholders = ",".join("?" * len(provider_list))
            sql += f" AND provider IN ({pplaceholders})"
            params += provider_list
        if variables:
            vplaceholders = ",".join("?" * len(list(variables)))
            sql += f" AND variable IN ({vplaceholders})"
            params += list(variables)
        if until is not None:
            sql += " AND valid_at <= ?"
            params.append(until.isoformat())
        rows = self._conn.execute(sql, params).fetchall()
        return [
            Residual(
                provider=r[0],
                cell_key=r[1],
                variable=r[2],
                valid_at=_parse_dt(r[3]),
                horizon_h=r[4],
                predicted=r[5],
                observed=r[6],
            )
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()


def ensure_known_provider(storage: Storage, name: str, kind: str) -> None:
    storage.upsert_provider(Provider(name=name, kind=kind))
