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
        try:
            raw = await anext(self._stream)
        except StopAsyncIteration:
            self._stream = self.source.readings()
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
