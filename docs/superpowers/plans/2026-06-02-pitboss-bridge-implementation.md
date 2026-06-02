# Pit Boss Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the monitoring-only Pit Boss bridge from `docs/superpowers/specs/2026-06-02-pitboss-bridge-design.md`.

**Architecture:** The bridge is a Python package under `src/pitboss_bridge`. A background collector reads from a configured source, normalizes readings, writes SQLite history, updates an in-memory state hub, and publishes updates to FastAPI endpoints. Local development uses a fake source; server deployment can switch to a lazy-imported `pytboss` BLE source.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, stdlib SQLite, pytest, httpx, Docker for Linux deployment packaging.

---

## File Structure

- Create: `pyproject.toml` - package metadata, runtime dependencies, test configuration.
- Create: `.gitignore` - ignores local virtualenvs, caches, SQLite data, and the temporary `pytboss` checkout.
- Create: `README.md` - local development and Linux/Docker deployment notes.
- Create: `src/pitboss_bridge/__init__.py` - package marker and version.
- Create: `src/pitboss_bridge/__main__.py` - `python -m pitboss_bridge` entrypoint.
- Create: `src/pitboss_bridge/config.py` - environment-backed settings.
- Create: `src/pitboss_bridge/state.py` - normalized state model and serialization.
- Create: `src/pitboss_bridge/store.py` - SQLite schema and history/health persistence.
- Create: `src/pitboss_bridge/sources/__init__.py` - source exports.
- Create: `src/pitboss_bridge/sources/base.py` - source protocol and source event type.
- Create: `src/pitboss_bridge/sources/fake.py` - deterministic fake grill source.
- Create: `src/pitboss_bridge/sources/pytboss_ble.py` - real BLE source using lazy `pytboss`/`bleak` imports.
- Create: `src/pitboss_bridge/services/__init__.py` - service package marker.
- Create: `src/pitboss_bridge/services/collector.py` - collector, health state, and state hub.
- Create: `src/pitboss_bridge/api.py` - FastAPI app factory and routes.
- Create: `tests/test_state.py` - normalization tests.
- Create: `tests/test_store.py` - SQLite tests.
- Create: `tests/test_collector.py` - fake source and collector tests.
- Create: `tests/test_api.py` - HTTP and WebSocket API tests.
- Create: `Dockerfile` - Linux container image.
- Create: `docker-compose.yml` - deployment template using host networking and persistent data.

---

### Task 1: Project Bootstrap

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/pitboss_bridge/__init__.py`
- Create: `src/pitboss_bridge/__main__.py`
- Create directories: `src/pitboss_bridge/sources`, `src/pitboss_bridge/services`, `tests`

- [ ] **Step 1: Create bootstrap files**

Create `pyproject.toml`:

```toml
[project]
name = "pitboss-bridge"
version = "0.1.0"
description = "Local monitoring bridge for Pit Boss grills"
requires-python = ">=3.10"
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.30,<1",
]

[project.optional-dependencies]
ble = [
  "bleak>=0.22,<1",
  "pytboss>=0.4,<1",
]
test = [
  "httpx>=0.27,<1",
  "pytest>=8,<9",
  "pytest-asyncio>=0.24,<1",
]

[project.scripts]
pitboss-bridge = "pitboss_bridge.__main__:main"

[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
asyncio_mode = "auto"
testpaths = ["tests"]
```

Create `.gitignore`:

```gitignore
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
*.pyc
*.pyo
*.sqlite
*.sqlite-shm
*.sqlite-wal
data/
.tmp-pytboss/
```

Create `README.md`:

```markdown
# Pit Boss Bridge

Monitoring-only local bridge for a Pit Boss `PB850PS2` grill.

Milestone 1 uses a fake source for local development and exposes:

- `GET /api/health`
- `GET /api/state`
- `GET /api/history`
- `WS /api/live`

Real BLE validation should happen on a Linux host near the grill.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
pytest
PITBOSS_SOURCE=fake uvicorn pitboss_bridge.api:create_app --factory --reload
```

On Windows with WSL2, run these commands inside Ubuntu so Python 3.10 is used.

## Configuration

- `PITBOSS_SOURCE`: `fake` or `ble`
- `PITBOSS_MODEL`: grill model, default `PB850PS2`
- `PITBOSS_DEVICE_NAME`: BLE device name for real source
- `PITBOSS_PASSWORD`: optional grill password
- `PITBOSS_DB`: SQLite path, default `data/pitboss.sqlite`
- `PITBOSS_POLL_SECONDS`: source polling interval, default `2`
- `PITBOSS_STALE_AFTER_SECONDS`: age threshold for stale data, default `30`

## Docker Direction

The target server deployment is Linux with BlueZ. The container should use the host Bluetooth stack via host networking and D-Bus mounts.
```

Create `src/pitboss_bridge/__init__.py`:

```python
"""Local monitoring bridge for Pit Boss grills."""

__version__ = "0.1.0"
```

Create `src/pitboss_bridge/__main__.py`:

```python
"""Command-line entrypoint for the Pit Boss bridge."""

import uvicorn


def main() -> None:
    """Run the development server."""
    uvicorn.run("pitboss_bridge.api:create_app", factory=True, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run bootstrap verification**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m py_compile src/pitboss_bridge/__init__.py src/pitboss_bridge/__main__.py"
```

Expected: command exits with code `0`.

- [ ] **Step 3: Commit bootstrap**

Run:

```bash
git add .gitignore README.md pyproject.toml src/pitboss_bridge/__init__.py src/pitboss_bridge/__main__.py
git commit -m "Bootstrap pitboss bridge project"
```

---

### Task 2: State Normalization

**Files:**
- Create: `src/pitboss_bridge/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write failing state tests**

Create `tests/test_state.py`:

```python
from datetime import UTC, datetime, timedelta

from pitboss_bridge.state import KNOWN_STATE_FIELDS, normalize_state


def test_normalize_state_wraps_each_known_value_with_timestamp():
    observed_at = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    received_at = observed_at + timedelta(seconds=1)

    snapshot = normalize_state(
        {
            "grillTemp": 220,
            "grillSetTemp": 225,
            "p1Temp": 150,
            "fanState": True,
            "surprise": "kept for diagnostics",
        },
        source="fake",
        observed_at=observed_at,
        received_at=received_at,
    )

    assert set(snapshot.fields) == {"grillTemp", "grillSetTemp", "p1Temp", "fanState"}
    assert snapshot.fields["grillTemp"].value == 220
    assert snapshot.fields["grillTemp"].observed_at == observed_at
    assert snapshot.unknown == {"surprise": "kept for diagnostics"}
    assert snapshot.raw["surprise"] == "kept for diagnostics"
    assert "grillTemp" in KNOWN_STATE_FIELDS


def test_snapshot_api_dict_marks_stale_by_age():
    observed_at = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    now = observed_at + timedelta(seconds=45)
    snapshot = normalize_state(
        {"grillTemp": 220},
        source="fake",
        observed_at=observed_at,
        received_at=observed_at,
    )

    payload = snapshot.to_api_dict(now=now, stale_after_seconds=30)

    assert payload["source"] == "fake"
    assert payload["stale"] is True
    assert payload["age_seconds"] == 45.0
    assert payload["fields"]["grillTemp"]["value"] == 220
    assert payload["fields"]["grillTemp"]["age_seconds"] == 45.0
```

- [ ] **Step 2: Run state tests to verify failure**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_state.py -q"
```

Expected: FAIL because `pitboss_bridge.state` does not exist.

- [ ] **Step 3: Implement state model**

Create `src/pitboss_bridge/state.py`:

```python
"""State normalization for Pit Boss readings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

KNOWN_STATE_FIELDS = frozenset(
    {
        "grillTemp",
        "grillSetTemp",
        "smokerActTemp",
        "isFahrenheit",
        "p1Temp",
        "p1Target",
        "p2Temp",
        "p2Target",
        "p3Temp",
        "p4Temp",
        "moduleIsOn",
        "fanState",
        "hotState",
        "motorState",
        "primeState",
        "lightState",
        "err1",
        "err2",
        "err3",
        "highTempErr",
        "fanErr",
        "hotErr",
        "motorErr",
        "noPellets",
        "erL",
        "recipeStep",
        "recipeTime",
    }
)


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class FieldReading:
    """One normalized field value with freshness metadata."""

    value: Any
    observed_at: datetime

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or utc_now()
        return max((current - self.observed_at).total_seconds(), 0.0)

    def to_api_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "value": self.value,
            "observed_at": _iso(self.observed_at),
            "age_seconds": self.age_seconds(now),
        }


@dataclass(frozen=True)
class StateSnapshot:
    """Normalized grill state from one source update."""

    source: str
    observed_at: datetime
    received_at: datetime
    fields: dict[str, FieldReading]
    unknown: dict[str, Any]
    raw: dict[str, Any]

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or utc_now()
        return max((current - self.observed_at).total_seconds(), 0.0)

    def is_stale(self, now: datetime | None = None, stale_after_seconds: int = 30) -> bool:
        return self.age_seconds(now) > stale_after_seconds

    def values_dict(self) -> dict[str, Any]:
        return {name: reading.value for name, reading in self.fields.items()}

    def to_api_dict(
        self,
        now: datetime | None = None,
        stale_after_seconds: int = 30,
    ) -> dict[str, Any]:
        current = now or utc_now()
        return {
            "source": self.source,
            "observed_at": _iso(self.observed_at),
            "received_at": _iso(self.received_at),
            "age_seconds": self.age_seconds(current),
            "stale": self.is_stale(current, stale_after_seconds),
            "fields": {
                name: reading.to_api_dict(current)
                for name, reading in sorted(self.fields.items())
            },
            "unknown": self.unknown,
            "raw": self.raw,
        }


def normalize_state(
    raw: dict[str, Any],
    source: str,
    observed_at: datetime | None = None,
    received_at: datetime | None = None,
) -> StateSnapshot:
    """Normalize a raw source payload into timestamped field readings."""
    observed = observed_at or utc_now()
    received = received_at or utc_now()
    fields = {
        name: FieldReading(value=value, observed_at=observed)
        for name, value in raw.items()
        if name in KNOWN_STATE_FIELDS
    }
    unknown = {
        name: value
        for name, value in raw.items()
        if name not in KNOWN_STATE_FIELDS
    }
    return StateSnapshot(
        source=source,
        observed_at=observed,
        received_at=received,
        fields=fields,
        unknown=unknown,
        raw=dict(raw),
    )
```

- [ ] **Step 4: Run state tests to verify pass**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_state.py -q"
```

Expected: PASS.

- [ ] **Step 5: Commit state model**

Run:

```bash
git add src/pitboss_bridge/state.py tests/test_state.py
git commit -m "Add timestamped state normalization"
```

---

### Task 3: SQLite Store

**Files:**
- Create: `src/pitboss_bridge/store.py`
- Create: `tests/test_store.py`

- [ ] **Step 1: Write failing store tests**

Create `tests/test_store.py`:

```python
from datetime import UTC, datetime

from pitboss_bridge.state import normalize_state
from pitboss_bridge.store import Database


def test_database_persists_and_lists_history(tmp_path):
    db = Database(tmp_path / "pitboss.sqlite")
    db.initialize()
    snapshot = normalize_state(
        {"grillTemp": 220, "p1Temp": 150, "unexpected": "raw"},
        source="fake",
        observed_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 6, 2, 12, 0, 1, tzinfo=UTC),
    )

    db.insert_reading(snapshot)
    rows = db.list_history()

    assert len(rows) == 1
    assert rows[0]["source"] == "fake"
    assert rows[0]["fields"]["grillTemp"] == 220
    assert rows[0]["fields"]["p1Temp"] == 150
    assert rows[0]["unknown"] == {"unexpected": "raw"}
    assert rows[0]["raw"]["unexpected"] == "raw"


def test_database_filters_history_by_since(tmp_path):
    db = Database(tmp_path / "pitboss.sqlite")
    db.initialize()
    old = normalize_state(
        {"grillTemp": 200},
        source="fake",
        observed_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
    )
    new = normalize_state(
        {"grillTemp": 225},
        source="fake",
        observed_at=datetime(2026, 6, 2, 12, 5, tzinfo=UTC),
        received_at=datetime(2026, 6, 2, 12, 5, tzinfo=UTC),
    )

    db.insert_reading(old)
    db.insert_reading(new)
    rows = db.list_history(since=datetime(2026, 6, 2, 12, 1, tzinfo=UTC))

    assert len(rows) == 1
    assert rows[0]["fields"]["grillTemp"] == 225


def test_database_records_health_events(tmp_path):
    db = Database(tmp_path / "pitboss.sqlite")
    db.initialize()

    db.insert_health_event("connect", "fake source connected", {"source": "fake"})
    events = db.list_health_events()

    assert events[0]["event_type"] == "connect"
    assert events[0]["message"] == "fake source connected"
    assert events[0]["data"] == {"source": "fake"}
```

- [ ] **Step 2: Run store tests to verify failure**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_store.py -q"
```

Expected: FAIL because `pitboss_bridge.store` does not exist.

- [ ] **Step 3: Implement SQLite store**

Create `src/pitboss_bridge/store.py`:

```python
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
```

- [ ] **Step 4: Run store tests to verify pass**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_store.py -q"
```

Expected: PASS.

- [ ] **Step 5: Commit SQLite store**

Run:

```bash
git add src/pitboss_bridge/store.py tests/test_store.py
git commit -m "Add SQLite history store"
```

---

### Task 4: Sources and Collector

**Files:**
- Create: `src/pitboss_bridge/sources/__init__.py`
- Create: `src/pitboss_bridge/sources/base.py`
- Create: `src/pitboss_bridge/sources/fake.py`
- Create: `src/pitboss_bridge/services/__init__.py`
- Create: `src/pitboss_bridge/services/collector.py`
- Create: `tests/test_collector.py`

- [ ] **Step 1: Write failing collector tests**

Create `tests/test_collector.py`:

```python
import asyncio

from pitboss_bridge.services.collector import Collector, CollectorHealth, StateHub
from pitboss_bridge.sources.fake import FakeGrillSource
from pitboss_bridge.store import Database


async def test_fake_source_emits_realistic_state():
    source = FakeGrillSource(interval_seconds=0)
    stream = source.readings()

    first = await anext(stream)
    second = await anext(stream)

    assert first["grillSetTemp"] == 225
    assert first["isFahrenheit"] is True
    assert "p1Temp" in first
    assert second["grillTemp"] != first["grillTemp"]


async def test_collector_stores_snapshot_and_updates_health(tmp_path):
    source = FakeGrillSource(interval_seconds=0)
    db = Database(tmp_path / "pitboss.sqlite")
    db.initialize()
    hub = StateHub(stale_after_seconds=30)
    health = CollectorHealth(source="fake", stale_after_seconds=30)
    collector = Collector(source=source, store=db, hub=hub, health=health)

    await collector.collect_next()

    assert hub.current is not None
    assert hub.current.fields["grillTemp"].value is not None
    assert health.connected is True
    assert health.last_update_at is not None
    assert db.list_history()[0]["fields"]["grillSetTemp"] == 225


async def test_state_hub_publishes_updates_to_subscribers():
    hub = StateHub(stale_after_seconds=30)
    queue = hub.subscribe()
    source = FakeGrillSource(interval_seconds=0)
    raw = await anext(source.readings())

    await hub.publish_raw(raw, source="fake")
    snapshot = await asyncio.wait_for(queue.get(), timeout=1)

    assert snapshot.fields["grillSetTemp"].value == 225
    hub.unsubscribe(queue)
```

- [ ] **Step 2: Run collector tests to verify failure**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_collector.py -q"
```

Expected: FAIL because source and collector modules do not exist.

- [ ] **Step 3: Implement source protocol**

Create `src/pitboss_bridge/sources/base.py`:

```python
"""Source interfaces for grill readings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class GrillSource(Protocol):
    """A stream of raw grill state dictionaries."""

    name: str

    def readings(self) -> AsyncIterator[dict[str, Any]]:
        """Yield raw grill states until cancelled or disconnected."""
        ...
```

Create `src/pitboss_bridge/sources/__init__.py`:

```python
"""Grill state sources."""

from pitboss_bridge.sources.fake import FakeGrillSource

__all__ = ["FakeGrillSource"]
```

- [ ] **Step 4: Implement fake source**

Create `src/pitboss_bridge/sources/fake.py`:

```python
"""Fake grill source for local development and tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class FakeGrillSource:
    """Emit deterministic, realistic grill readings."""

    name = "fake"

    def __init__(self, interval_seconds: float = 2.0) -> None:
        self.interval_seconds = interval_seconds
        self._tick = 0

    async def readings(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield self._reading()
            self._tick += 1
            if self.interval_seconds > 0:
                await asyncio.sleep(self.interval_seconds)

    def _reading(self) -> dict[str, Any]:
        grill_temp = 215 + (self._tick % 12)
        probe_1 = 140 + min(self._tick, 35)
        probe_2 = 128 + min(self._tick // 2, 30)
        return {
            "grillTemp": grill_temp,
            "grillSetTemp": 225,
            "smokerActTemp": grill_temp,
            "isFahrenheit": True,
            "p1Temp": probe_1,
            "p1Target": 165,
            "p2Temp": probe_2,
            "moduleIsOn": True,
            "fanState": self._tick % 3 != 0,
            "hotState": self._tick < 6,
            "motorState": self._tick % 5 == 0,
            "primeState": False,
            "lightState": False,
            "err1": False,
            "err2": False,
            "err3": False,
            "highTempErr": False,
            "fanErr": False,
            "hotErr": False,
            "motorErr": False,
            "noPellets": False,
            "erL": False,
            "recipeStep": 1,
            "recipeTime": max(0, 3600 - self._tick * 2),
        }
```

- [ ] **Step 5: Implement collector and state hub**

Create `src/pitboss_bridge/services/__init__.py`:

```python
"""Bridge service components."""
```

Create `src/pitboss_bridge/services/collector.py`:

```python
"""Background collection and live state publishing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pitboss_bridge.sources.base import GrillSource
from pitboss_bridge.state import StateSnapshot, normalize_state, utc_now
from pitboss_bridge.store import Database


@dataclass
class CollectorHealth:
    """Mutable health state for the collector."""

    source: str
    stale_after_seconds: int
    connected: bool = False
    started_at: datetime | None = None
    last_update_at: datetime | None = None
    last_error: str | None = None
    reconnect_count: int = 0

    def mark_started(self) -> None:
        if self.started_at is None:
            self.started_at = utc_now()

    def mark_connected(self) -> None:
        self.connected = True
        self.last_error = None

    def mark_update(self, at: datetime | None = None) -> None:
        self.connected = True
        self.last_update_at = at or utc_now()
        self.last_error = None

    def mark_error(self, error: str) -> None:
        self.connected = False
        self.last_error = error
        self.reconnect_count += 1

    def to_api_dict(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or utc_now()
        age = None
        stale = True
        if self.last_update_at is not None:
            age = max((current - self.last_update_at).total_seconds(), 0.0)
            stale = age > self.stale_after_seconds
        return {
            "source": self.source,
            "connected": self.connected,
            "started_at": self.started_at.astimezone(UTC).isoformat()
            if self.started_at
            else None,
            "last_update_at": self.last_update_at.astimezone(UTC).isoformat()
            if self.last_update_at
            else None,
            "last_update_age_seconds": age,
            "stale": stale,
            "stale_after_seconds": self.stale_after_seconds,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
        }


class StateHub:
    """Holds current state and broadcasts updates to subscribers."""

    def __init__(self, stale_after_seconds: int) -> None:
        self.stale_after_seconds = stale_after_seconds
        self.current: StateSnapshot | None = None
        self._subscribers: set[asyncio.Queue[StateSnapshot]] = set()

    async def publish_raw(self, raw: dict[str, Any], source: str) -> StateSnapshot:
        snapshot = normalize_state(raw, source=source)
        await self.publish(snapshot)
        return snapshot

    async def publish(self, snapshot: StateSnapshot) -> None:
        self.current = snapshot
        for queue in list(self._subscribers):
            queue.put_nowait(snapshot)

    def subscribe(self) -> asyncio.Queue[StateSnapshot]:
        queue: asyncio.Queue[StateSnapshot] = asyncio.Queue(maxsize=10)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[StateSnapshot]) -> None:
        self._subscribers.discard(queue)

    def current_api_dict(self) -> dict[str, Any]:
        if self.current is None:
            return {
                "available": False,
                "stale": True,
                "stale_after_seconds": self.stale_after_seconds,
                "fields": {},
                "unknown": {},
                "raw": {},
            }
        payload = self.current.to_api_dict(
            stale_after_seconds=self.stale_after_seconds
        )
        payload["available"] = True
        return payload


class Collector:
    """Collect source readings, persist them, and publish live state."""

    def __init__(
        self,
        source: GrillSource,
        store: Database,
        hub: StateHub,
        health: CollectorHealth,
        retry_seconds: float = 5.0,
    ) -> None:
        self.source = source
        self.store = store
        self.hub = hub
        self.health = health
        self.retry_seconds = retry_seconds
        self._stream = source.readings()
        self._stop = asyncio.Event()

    async def collect_next(self) -> StateSnapshot:
        self.health.mark_started()
        self.health.mark_connected()
        raw = await anext(self._stream)
        snapshot = normalize_state(raw, source=self.source.name)
        self.store.insert_reading(snapshot)
        await self.hub.publish(snapshot)
        self.health.mark_update(snapshot.received_at)
        return snapshot

    async def run(self) -> None:
        self.health.mark_started()
        self.store.insert_health_event(
            "collector_started",
            "collector started",
            {"source": self.source.name},
        )
        while not self._stop.is_set():
            try:
                await self.collect_next()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.mark_error(str(exc))
                self.store.insert_health_event(
                    "source_error",
                    str(exc),
                    {"source": self.source.name},
                )
                await asyncio.sleep(self.retry_seconds)
                self._stream = self.source.readings()

    def stop(self) -> None:
        self._stop.set()
```

- [ ] **Step 6: Run collector tests to verify pass**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_collector.py -q"
```

Expected: PASS.

- [ ] **Step 7: Commit sources and collector**

Run:

```bash
git add src/pitboss_bridge/sources src/pitboss_bridge/services tests/test_collector.py
git commit -m "Add fake source and collector"
```

---

### Task 5: Configuration and FastAPI HTTP API

**Files:**
- Create: `src/pitboss_bridge/config.py`
- Create: `src/pitboss_bridge/api.py`
- Create: `tests/test_api.py`

- [ ] **Step 1: Write failing HTTP API tests**

Create `tests/test_api.py`:

```python
from fastapi.testclient import TestClient

from pitboss_bridge.api import create_app
from pitboss_bridge.config import Settings


def test_health_state_and_history_endpoints(tmp_path):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.collector_sync_collect_next()

        health = client.get("/api/health").json()
        state = client.get("/api/state").json()
        history = client.get("/api/history").json()

    assert health["source"] == "fake"
    assert health["connected"] is True
    assert state["available"] is True
    assert state["fields"]["grillSetTemp"]["value"] == 225
    assert history["count"] == 1
    assert history["items"][0]["fields"]["grillSetTemp"] == 225


def test_history_since_filter(tmp_path):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        app.state.collector_sync_collect_next()
        since = client.get("/api/history").json()["items"][1]["observed_at"]
        filtered = client.get(f"/api/history?since={since}").json()

    assert filtered["count"] == 1
```

- [ ] **Step 2: Run API tests to verify failure**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_api.py -q"
```

Expected: FAIL because `pitboss_bridge.api` and `pitboss_bridge.config` do not exist.

- [ ] **Step 3: Implement settings**

Create `src/pitboss_bridge/config.py`:

```python
"""Environment-backed settings for the bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings."""

    source: str = "fake"
    model: str = "PB850PS2"
    device_name: str = ""
    password: str = ""
    db_path: Path = Path("data/pitboss.sqlite")
    poll_seconds: float = 2.0
    stale_after_seconds: int = 30
    start_collector: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=os.getenv("PITBOSS_SOURCE", "fake"),
            model=os.getenv("PITBOSS_MODEL", "PB850PS2"),
            device_name=os.getenv("PITBOSS_DEVICE_NAME", ""),
            password=os.getenv("PITBOSS_PASSWORD", ""),
            db_path=Path(os.getenv("PITBOSS_DB", "data/pitboss.sqlite")),
            poll_seconds=float(os.getenv("PITBOSS_POLL_SECONDS", "2")),
            stale_after_seconds=int(os.getenv("PITBOSS_STALE_AFTER_SECONDS", "30")),
            start_collector=_bool_env("PITBOSS_START_COLLECTOR", True),
        )
```

- [ ] **Step 4: Implement FastAPI app and HTTP routes**

Create `src/pitboss_bridge/api.py`:

```python
"""FastAPI app for the Pit Boss bridge."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from pitboss_bridge.config import Settings
from pitboss_bridge.services.collector import Collector, CollectorHealth, StateHub
from pitboss_bridge.sources.fake import FakeGrillSource
from pitboss_bridge.store import Database


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _make_source(settings: Settings) -> Any:
    if settings.source == "fake":
        return FakeGrillSource(interval_seconds=settings.poll_seconds)
    if settings.source == "ble":
        from pitboss_bridge.sources.pytboss_ble import PytbossBleSource

        return PytbossBleSource(
            model=settings.model,
            device_name=settings.device_name,
            password=settings.password,
            poll_seconds=settings.poll_seconds,
        )
    raise ValueError(f"Unsupported PITBOSS_SOURCE={settings.source!r}")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI app."""
    app_settings = settings or Settings.from_env()
    store = Database(app_settings.db_path)
    store.initialize()
    hub = StateHub(stale_after_seconds=app_settings.stale_after_seconds)
    source = _make_source(app_settings)
    health = CollectorHealth(
        source=source.name,
        stale_after_seconds=app_settings.stale_after_seconds,
    )
    collector = Collector(source=source, store=store, hub=hub, health=health)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task: asyncio.Task[None] | None = None
        if app_settings.start_collector:
            task = asyncio.create_task(collector.run())
        try:
            yield
        finally:
            collector.stop()
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Pit Boss Bridge", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.store = store
    app.state.hub = hub
    app.state.health = health
    app.state.collector = collector

    def collector_sync_collect_next() -> None:
        asyncio.run(collector.collect_next())

    app.state.collector_sync_collect_next = collector_sync_collect_next

    @app.get("/api/health")
    async def health_endpoint() -> dict[str, Any]:
        return health.to_api_dict()

    @app.get("/api/state")
    async def state_endpoint() -> dict[str, Any]:
        return hub.current_api_dict()

    @app.get("/api/history")
    async def history_endpoint(
        since: str | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=10000),
    ) -> dict[str, Any]:
        rows = store.list_history(since=_parse_since(since), limit=limit)
        return {"count": len(rows), "items": rows}

    @app.websocket("/api/live")
    async def live_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = hub.subscribe()
        try:
            if hub.current is not None:
                await websocket.send_json(hub.current_api_dict())
            while True:
                snapshot = await queue.get()
                await websocket.send_json(
                    snapshot.to_api_dict(
                        stale_after_seconds=app_settings.stale_after_seconds
                    )
                )
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(queue)

    return app
```

- [ ] **Step 5: Run HTTP API tests to verify pass**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_api.py -q"
```

Expected: PASS.

- [ ] **Step 6: Commit HTTP API**

Run:

```bash
git add src/pitboss_bridge/config.py src/pitboss_bridge/api.py tests/test_api.py
git commit -m "Add bridge HTTP API"
```

---

### Task 6: WebSocket Streaming

**Files:**
- Modify: `tests/test_api.py`
- Modify: `src/pitboss_bridge/api.py` only if the test reveals a mismatch

- [ ] **Step 1: Add failing WebSocket test**

Append to `tests/test_api.py`:

```python

def test_live_websocket_streams_current_and_next_state(tmp_path):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        with client.websocket_connect("/api/live") as websocket:
            first = websocket.receive_json()
            app.state.collector_sync_collect_next()
            second = websocket.receive_json()

    assert first["fields"]["grillSetTemp"]["value"] == 225
    assert second["fields"]["grillSetTemp"]["value"] == 225
    assert second["fields"]["grillTemp"]["value"] != first["fields"]["grillTemp"]["value"]
```

- [ ] **Step 2: Run WebSocket test**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_api.py::test_live_websocket_streams_current_and_next_state -q"
```

Expected: PASS if Task 5 WebSocket implementation is correct. If it fails because the second update is not delivered, adjust `StateHub.publish` to drop the oldest queue item when a subscriber queue is full:

```python
for queue in list(self._subscribers):
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(snapshot)
```

- [ ] **Step 3: Run all API tests**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_api.py -q"
```

Expected: PASS.

- [ ] **Step 4: Commit WebSocket coverage**

Run:

```bash
git add src/pitboss_bridge/api.py src/pitboss_bridge/services/collector.py tests/test_api.py
git commit -m "Cover live state streaming"
```

---

### Task 7: BLE Source Stub

**Files:**
- Create: `src/pitboss_bridge/sources/pytboss_ble.py`
- Create: `tests/test_ble_source.py`

- [ ] **Step 1: Write failing BLE source configuration tests**

Create `tests/test_ble_source.py`:

```python
import pytest

from pitboss_bridge.sources.pytboss_ble import PytbossBleSource


def test_ble_source_requires_device_name():
    source = PytbossBleSource(model="PB850PS2", device_name="", password="", poll_seconds=2)

    with pytest.raises(ValueError, match="device_name"):
        source.validate()


def test_ble_source_exposes_name_and_settings():
    source = PytbossBleSource(
        model="PB850PS2",
        device_name="PBL-EC6260C77A8C",
        password="secret",
        poll_seconds=2,
    )

    source.validate()

    assert source.name == "ble"
    assert source.model == "PB850PS2"
    assert source.device_name == "PBL-EC6260C77A8C"
```

- [ ] **Step 2: Run BLE source tests to verify failure**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_ble_source.py -q"
```

Expected: FAIL because `pytboss_ble.py` does not exist.

- [ ] **Step 3: Implement lazy BLE source**

Create `src/pitboss_bridge/sources/pytboss_ble.py`:

```python
"""Real Pit Boss BLE source using pytboss.

This source is lazy-imported so local fake-source development does not require
Bluetooth dependencies.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass
class PytbossBleSource:
    """Poll a Pit Boss grill over BLE using pytboss."""

    model: str
    device_name: str
    password: str
    poll_seconds: float = 2.0
    name: str = "ble"

    def validate(self) -> None:
        if not self.device_name:
            raise ValueError("device_name is required for PITBOSS_SOURCE=ble")

    async def readings(self) -> AsyncIterator[dict[str, Any]]:
        self.validate()
        try:
            from bleak import BleakScanner
            from pytboss import BleConnection, PitBoss
        except ImportError as exc:
            raise RuntimeError(
                "BLE source requires installing the bridge with the 'ble' extra"
            ) from exc

        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: d.name == self.device_name
            or ad.local_name == self.device_name
        )
        if device is None:
            raise RuntimeError(f"Pit Boss BLE device not found: {self.device_name}")

        boss = PitBoss(BleConnection(device), self.model, password=self.password)
        await boss.start()
        try:
            while True:
                state = await boss.get_state()
                yield dict(state)
                await asyncio.sleep(self.poll_seconds)
        finally:
            await boss.stop()
```

- [ ] **Step 4: Run BLE source tests to verify pass**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m pytest tests/test_ble_source.py -q"
```

Expected: PASS without installing `pytboss` or `bleak`.

- [ ] **Step 5: Commit BLE source**

Run:

```bash
git add src/pitboss_bridge/sources/pytboss_ble.py tests/test_ble_source.py
git commit -m "Add lazy BLE source"
```

---

### Task 8: Docker Packaging

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Modify: `README.md`

- [ ] **Step 1: Create Dockerfile**

Create `Dockerfile`:

```dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bluez dbus \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir ".[ble]"

EXPOSE 8000

CMD ["uvicorn", "pitboss_bridge.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Create compose template**

Create `docker-compose.yml`:

```yaml
services:
  pitboss-bridge:
    build: .
    network_mode: host
    environment:
      PITBOSS_SOURCE: fake
      PITBOSS_MODEL: PB850PS2
      PITBOSS_DEVICE_NAME: PBL-EC6260C77A8C
      PITBOSS_DB: /data/pitboss.sqlite
      PITBOSS_POLL_SECONDS: "2"
      PITBOSS_STALE_AFTER_SECONDS: "30"
    volumes:
      - ./data:/data
      - /run/dbus:/run/dbus:ro
      - /var/run/dbus:/var/run/dbus:ro
```

- [ ] **Step 3: Update README Docker section**

Replace the README Docker section with:

```markdown
## Docker Direction

The target server deployment is Linux with BlueZ. Start with the fake source:

```bash
docker compose up --build
curl http://localhost:8000/api/health
```

After the Python BLE probe works on the Linux host, switch:

```yaml
environment:
  PITBOSS_SOURCE: ble
  PITBOSS_MODEL: PB850PS2
  PITBOSS_DEVICE_NAME: PBL-EC6260C77A8C
```

The compose file uses host networking and read-only D-Bus mounts so `bleak`
can talk to the host Bluetooth stack.
```

- [ ] **Step 4: Verify Docker files are syntactically readable**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 - <<'PY'
from pathlib import Path
assert 'FROM python:3.11-slim' in Path('Dockerfile').read_text()
assert 'network_mode: host' in Path('docker-compose.yml').read_text()
PY"
```

Expected: PASS with exit code `0`.

- [ ] **Step 5: Commit Docker packaging**

Run:

```bash
git add Dockerfile docker-compose.yml README.md
git commit -m "Add Docker deployment template"
```

---

### Task 9: End-to-End Verification

**Files:**
- Modify: no files unless verification reveals a bug.

- [ ] **Step 1: Install test dependencies in WSL**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && python3 -m venv .venv && . .venv/bin/activate && python -m pip install -U pip && python -m pip install -e '.[test]'"
```

Expected: installs package and test dependencies.

- [ ] **Step 2: Run full test suite**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && . .venv/bin/activate && pytest -q"
```

Expected: all tests pass.

- [ ] **Step 3: Smoke-run API with fake source**

Run:

```bash
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Users/steve/Documents/Pitboss && . .venv/bin/activate && PITBOSS_SOURCE=fake PITBOSS_DB=data/dev.sqlite timeout 8s uvicorn pitboss_bridge.api:create_app --factory --host 127.0.0.1 --port 8000"
```

Expected: Uvicorn starts, runs for 8 seconds, then exits from `timeout`.

- [ ] **Step 4: Query API during smoke run**

Run this in a second shell while Step 3 is running:

```bash
wsl -d Ubuntu -- bash -lc "curl -s http://127.0.0.1:8000/api/health && echo && curl -s http://127.0.0.1:8000/api/state && echo && curl -s http://127.0.0.1:8000/api/history"
```

Expected: JSON responses show source `fake`, state fields, and at least one history row.

- [ ] **Step 5: Commit any verification fixes**

If no files changed, do not commit. If a bug was found and fixed, run:

```bash
git add <fixed-files>
git commit -m "Fix bridge verification issue"
```

