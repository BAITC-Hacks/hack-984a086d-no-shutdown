"""Durable forecast state and audit log backed by SQLite."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ForecastStore:
    """Small concurrency-safe store. Each operation owns its SQLite connection."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    input_hash TEXT,
                    model_hash TEXT,
                    payload_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_forecasts_origin ON forecasts(origin, id DESC);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    forecast_id INTEGER REFERENCES forecasts(id),
                    occurred_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                """
            )

    def start(self, origin: str, horizon: int) -> int:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO forecasts(origin,horizon,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (origin, horizon, "running", now, now),
            )
            return int(cur.lastrowid)

    def event(self, forecast_id: int | None, stage: str, status: str, detail: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_events(forecast_id,occurred_at,stage,status,detail_json) VALUES(?,?,?,?,?)",
                (forecast_id, _now(), stage, status, json.dumps(detail, sort_keys=True, default=str)),
            )

    def finish(self, forecast_id: int, payload: dict, input_hash: str, model_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE forecasts SET status='succeeded', input_hash=?, model_hash=?, payload_json=?, error=NULL, updated_at=? WHERE id=?",
                (input_hash, model_hash, json.dumps(payload, sort_keys=True, default=str), _now(), forecast_id),
            )

    def fail(self, forecast_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE forecasts SET status='failed', error=?, updated_at=? WHERE id=?", (error, _now(), forecast_id))

    def find_reusable(self, origin: str, horizon: int, input_hash: str, model_hash: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM forecasts WHERE origin=? AND horizon=? AND status='succeeded' AND input_hash=? AND model_hash=? ORDER BY id DESC LIMIT 1",
                (origin, horizon, input_hash, model_hash),
            ).fetchone()
            return self._row(row) if row else None

    def get(self, forecast_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
            if not row:
                return None
            result = self._row(row)
            result["audit"] = [dict(r) | {"detail": json.loads(r["detail_json"])} for r in conn.execute(
                "SELECT * FROM audit_events WHERE forecast_id=? ORDER BY id", (forecast_id,)
            )]
            return result

    def list(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM forecasts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        out = dict(row)
        if out.get("payload_json"):
            out["result"] = json.loads(out.pop("payload_json"))
        else:
            out.pop("payload_json", None)
        return out
