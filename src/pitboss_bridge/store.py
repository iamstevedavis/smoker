"""SQLite persistence for bridge history and health events."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pitboss_bridge.state import StateSnapshot, utc_now


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Database:
    """Small SQLite wrapper for one-grill bridge history."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    unknown_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_readings_observed_at
                ON readings (observed_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS health_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_health_events_at
                ON health_events (at)
                """
            )

    def insert_reading(self, snapshot: StateSnapshot) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO readings (
                    observed_at,
                    received_at,
                    source,
                    fields_json,
                    unknown_json,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(snapshot.observed_at),
                    _iso(snapshot.received_at),
                    snapshot.source,
                    json.dumps(snapshot.values_dict(), sort_keys=True),
                    json.dumps(snapshot.unknown, sort_keys=True),
                    json.dumps(snapshot.raw, sort_keys=True),
                ),
            )

    def list_history(
        self,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM readings"
        params: list[Any] = []
        if since is not None:
            query += " WHERE observed_at >= ?"
            params.append(_iso(since))
        query += " ORDER BY observed_at ASC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._reading_row_to_dict(row) for row in rows]

    def insert_health_event(
        self,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO health_events (at, event_type, message, data_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    _iso(at or utc_now()),
                    event_type,
                    message,
                    json.dumps(data or {}, sort_keys=True),
                ),
            )

    def list_health_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM health_events
                ORDER BY at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "at": row["at"],
                "event_type": row["event_type"],
                "message": row["message"],
                "data": json.loads(row["data_json"]),
            }
            for row in rows
        ]

    def _reading_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "observed_at": _parse_dt(row["observed_at"]).isoformat(),
            "received_at": _parse_dt(row["received_at"]).isoformat(),
            "source": row["source"],
            "fields": json.loads(row["fields_json"]),
            "unknown": json.loads(row["unknown_json"]),
            "raw": json.loads(row["raw_json"]),
        }
