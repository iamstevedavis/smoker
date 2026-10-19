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
            self._create_cooks_table(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    name TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _create_cooks_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                meat TEXT,
                weight TEXT,
                target_internal_temp REAL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            )
            """
        )

    def load_settings(self) -> dict[str, Any]:
        """Return the settings saved from the dashboard, by field name."""
        with self.connect() as conn:
            rows = conn.execute("SELECT name, value_json FROM settings").fetchall()
        return {row["name"]: json.loads(row["value_json"]) for row in rows}

    def save_settings(self, values: dict[str, Any]) -> None:
        """Upsert several settings in one transaction."""
        now = _iso(utc_now())
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO settings (name, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                [(name, json.dumps(value), now) for name, value in values.items()],
            )

    def delete_setting(self, name: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM settings WHERE name = ?", (name,))
        return cursor.rowcount > 0

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
        until: datetime | None = None,
        newest_first: bool = False,
        bucket_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        """Readings in `[since, until]`, oldest first unless `newest_first`.

        With `newest_first` the *latest* `limit` rows are selected (returned
        newest first). With `bucket_seconds` one row is returned per bucket
        instead of one per reading: numeric fields are averaged and anything
        else takes the last value in the bucket.
        """
        where, params = self._history_window(since, until)
        direction = "DESC" if newest_first else "ASC"
        if bucket_seconds:
            return self._list_bucketed(where, params, direction, limit, bucket_seconds)
        query = (
            f"SELECT * FROM readings{where} "
            f"ORDER BY observed_at {direction}, id {direction} LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(query, [*params, limit]).fetchall()
        return [self._reading_row_to_dict(row) for row in rows]

    def prune(self, older_than: datetime) -> dict[str, int]:
        """Delete readings and health events observed before `older_than`."""
        cutoff = _iso(older_than)
        with self.connect() as conn:
            readings = conn.execute(
                "DELETE FROM readings WHERE observed_at < ?", (cutoff,)
            ).rowcount
            events = conn.execute(
                "DELETE FROM health_events WHERE at < ?", (cutoff,)
            ).rowcount
        return {"readings": readings, "health_events": events}

    @staticmethod
    def _history_window(
        since: datetime | None, until: datetime | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("observed_at >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("observed_at <= ?")
            params.append(_iso(until))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def _list_bucketed(
        self,
        where: str,
        params: list[Any],
        direction: str,
        limit: int,
        bucket_seconds: int,
    ) -> list[dict[str, Any]]:
        # Only the columns needed are read (not raw_json), and the bucket key is
        # computed in SQLite; the index on observed_at serves the range scan.
        query = (
            "SELECT id, observed_at, received_at, source, fields_json, "
            "CAST(strftime('%s', observed_at) AS INTEGER) / ? AS bucket "
            f"FROM readings{where} ORDER BY observed_at {direction}, id {direction}"
        )
        buckets: list[list[sqlite3.Row]] = []
        current_key: int | None = None
        with self.connect() as conn:
            for row in conn.execute(query, [bucket_seconds, *params]):
                if row["bucket"] != current_key:
                    if len(buckets) == limit:
                        break
                    current_key = row["bucket"]
                    buckets.append([])
                buckets[-1].append(row)
        return [self._bucket_to_dict(rows, bucket_seconds) for rows in buckets]

    @staticmethod
    def _bucket_to_dict(rows: list[sqlite3.Row], bucket_seconds: int) -> dict[str, Any]:
        rows = sorted(rows, key=lambda r: (r["observed_at"], r["id"]))
        samples = [json.loads(row["fields_json"]) for row in rows]
        fields: dict[str, Any] = {}
        for name in {name for sample in samples for name in sample}:
            values = [s[name] for s in samples if s.get(name) is not None]
            if values and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in values
            ):
                fields[name] = round(sum(values) / len(values), 3)
            else:
                fields[name] = values[-1] if values else None
        last = rows[-1]
        start = datetime.fromtimestamp(last["bucket"] * bucket_seconds, UTC)
        return {
            "id": last["id"],
            "observed_at": start.isoformat(),
            "received_at": _parse_dt(last["received_at"]).isoformat(),
            "source": last["source"],
            "fields": dict(sorted(fields.items())),
            "unknown": {},
            "raw": {},
            "samples": len(rows),
        }

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

    # Cooks: bridge-side sessions only, never written to the grill. ----------

    def start_cook(
        self,
        name: str,
        notes: str = "",
        meat: str | None = None,
        weight: str | None = None,
        target_internal_temp: float | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Start a cook, or return None if one is already active."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM cooks WHERE ended_at IS NULL").fetchone():
                return None
            cursor = conn.execute(
                """
                INSERT INTO cooks (name, notes, meat, weight, target_internal_temp,
                                   started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, notes, meat, weight, target_internal_temp, _iso(at or utc_now())),
            )
            cook_id = cursor.lastrowid
        return self.get_cook(cook_id)

    def stop_cook(self, cook_id: int, at: datetime | None = None) -> dict[str, Any] | None:
        """End a cook. Returns None if it is missing or already stopped."""
        with self.connect() as conn:
            updated = conn.execute(
                "UPDATE cooks SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (_iso(at or utc_now()), cook_id),
            ).rowcount
        return self.get_cook(cook_id) if updated else None

    def update_cook(self, cook_id: int, **changes: str) -> dict[str, Any] | None:
        columns = [name for name in changes if name in {"name", "notes"}]
        if columns:
            assignments = ", ".join(f"{name} = ?" for name in columns)
            with self.connect() as conn:
                conn.execute(
                    f"UPDATE cooks SET {assignments} WHERE id = ?",
                    [*(changes[name] for name in columns), cook_id],
                )
        return self.get_cook(cook_id)

    def get_cook(self, cook_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cooks WHERE id = ?", (cook_id,)).fetchone()
        return dict(row) if row else None

    def list_cooks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cooks ORDER BY started_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cook_summary(self, cook: dict[str, Any]) -> dict[str, Any]:
        """Duration and temperature stats over the cook's window."""
        started = cook["started_at"]
        ended = cook["ended_at"] or _iso(utc_now())
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS readings,
                       MIN(json_extract(fields_json, '$.grillTemp')) AS grill_min,
                       MAX(json_extract(fields_json, '$.grillTemp')) AS grill_max,
                       AVG(json_extract(fields_json, '$.grillTemp')) AS grill_avg,
                       MAX(json_extract(fields_json, '$.p1Temp')) AS probe1_max,
                       MAX(json_extract(fields_json, '$.p2Temp')) AS probe2_max
                FROM readings WHERE observed_at >= ? AND observed_at <= ?
                """,
                (started, ended),
            ).fetchone()
        grill_avg = row["grill_avg"]
        return {
            "duration_seconds": (_parse_dt(ended) - _parse_dt(started)).total_seconds(),
            "readings": row["readings"],
            "grill_temp": {
                "min": row["grill_min"],
                "max": row["grill_max"],
                "avg": round(grill_avg, 1) if grill_avg is not None else None,
            },
            "probe1_max": row["probe1_max"],
            "probe2_max": row["probe2_max"],
        }
