"""Background collection and live state publishing."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pitboss_bridge.sources.base import GrillSource
from pitboss_bridge.state import DEFAULT_MODEL, StateSnapshot, normalize_state, utc_now
from pitboss_bridge.store import Database

logger = logging.getLogger(__name__)


@dataclass
class CollectorHealth:
    """Mutable health state for the collector.

    `connected` only becomes true once a reading has actually arrived from the
    source, and `reconnect_count` only counts sessions that delivered data
    after an earlier session was lost -- failed attempts are tracked
    separately in `consecutive_failures`.
    """

    source: str
    stale_after_seconds: int
    connected: bool = False
    started_at: datetime | None = None
    last_update_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    reconnect_count: int = 0
    consecutive_failures: int = 0
    unauthorized_retries: int = 0
    _has_connected: bool = False

    def mark_started(self) -> None:
        if self.started_at is None:
            self.started_at = utc_now()

    def mark_update(self, at: datetime | None = None) -> bool:
        """Record a reading. Returns True if this begins a new session."""
        new_session = not self.connected
        if new_session and self._has_connected:
            self.reconnect_count += 1
        self.connected = True
        self._has_connected = True
        self.consecutive_failures = 0
        self.last_update_at = at or utc_now()
        return new_session

    def mark_error(self, error: str, at: datetime | None = None) -> None:
        self.connected = False
        self.last_error = error
        self.last_error_at = at or utc_now()
        self.consecutive_failures += 1

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
            "consecutive_failures": self.consecutive_failures,
            "unauthorized_retries": self.unauthorized_retries,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.astimezone(UTC).isoformat()
            if self.last_error_at
            else None,
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
            if queue.full():
                # A slow subscriber loses its oldest snapshot rather than
                # raising into (and stalling) the collector.
                queue.get_nowait()
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
        max_retry_seconds: float = 60.0,
        retention_days: float = 0,
        prune_interval_seconds: float = 86400.0,
    ) -> None:
        self.source = source
        self.store = store
        self.hub = hub
        self.health = health
        self.retry_seconds = retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self.retention_days = retention_days
        self.prune_interval_seconds = prune_interval_seconds
        self._stream = source.readings()
        self._stop = asyncio.Event()

    async def collect_next(self) -> StateSnapshot:
        self.health.mark_started()
        try:
            try:
                raw = await anext(self._stream)
            except StopAsyncIteration:
                self._stream = self.source.readings()
                raw = await anext(self._stream)
        finally:
            # Sync even when the read fails, so retries made before a
            # failed poll still show up in /api/health during backoff.
            self._sync_source_stats()
        snapshot = normalize_state(
            raw,
            source=self.source.name,
            model=getattr(self.source, "model", DEFAULT_MODEL),
        )
        self.store.insert_reading(snapshot)
        await self.hub.publish(snapshot)
        if self.health.mark_update(snapshot.received_at):
            logger.info("Receiving readings from %s source", self.source.name)
            self.store.insert_health_event(
                "connected",
                "source delivering readings",
                {
                    "source": self.source.name,
                    "reconnect_count": self.health.reconnect_count,
                },
            )
        return snapshot

    def _sync_source_stats(self) -> None:
        stats = getattr(self.source, "stats", None)
        if stats is not None:
            self.health.unauthorized_retries = stats.unauthorized_retries

    def retry_delay(self) -> float:
        """Exponential backoff based on consecutive failures."""
        failures = max(self.health.consecutive_failures, 1)
        # Cap the exponent so a grill that stays unreachable (about 17 hours
        # at the 60s cap) cannot overflow the float and kill the collector.
        exponent = min(failures - 1, 16)
        return min(self.retry_seconds * 2**exponent, self.max_retry_seconds)

    async def _close_stream(self) -> None:
        """Close the current stream so sources release connections promptly."""
        try:
            await self._stream.aclose()
        except Exception:
            logger.debug("Error while closing source stream", exc_info=True)

    def prune_once(self) -> dict[str, int]:
        """Delete history older than the retention window (no-op when 0)."""
        if self.retention_days <= 0:
            return {"readings": 0, "health_events": 0}
        cutoff = utc_now() - timedelta(days=self.retention_days)
        deleted = self.store.prune(cutoff)
        if deleted["readings"] or deleted["health_events"]:
            logger.info(
                "Pruned %s readings and %s health events older than %s days",
                deleted["readings"],
                deleted["health_events"],
                self.retention_days,
            )
        return deleted

    async def _prune_loop(self) -> None:
        # At startup, then once per interval.
        while True:
            try:
                await asyncio.to_thread(self.prune_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("History prune failed", exc_info=True)
            await asyncio.sleep(self.prune_interval_seconds)

    async def run(self) -> None:
        prune_task = (
            asyncio.create_task(self._prune_loop()) if self.retention_days > 0 else None
        )
        self.health.mark_started()
        logger.info("Collector started with %s source", self.source.name)
        self.store.insert_health_event(
            "collector_started",
            "collector started",
            {"source": self.source.name},
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.collect_next()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = str(exc) or type(exc).__name__
                    self.health.mark_error(message)
                    delay = self.retry_delay()
                    logger.warning(
                        "Source error (%s consecutive): %s; retrying in %.0fs",
                        self.health.consecutive_failures,
                        message,
                        delay,
                    )
                    self.store.insert_health_event(
                        "source_error",
                        message,
                        {
                            "source": self.source.name,
                            "consecutive_failures": self.health.consecutive_failures,
                            "retry_in_seconds": delay,
                        },
                    )
                    await self._close_stream()
                    await asyncio.sleep(delay)
                    self._stream = self.source.readings()
        finally:
            if prune_task is not None:
                prune_task.cancel()
            await self._close_stream()
            logger.info("Collector stopped")

    def stop(self) -> None:
        self._stop.set()
