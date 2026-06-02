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
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        head, separator, tail = value.rpartition(" ")
        if separator and tail.count(":") == 1 and tail[0].isdigit():
            return datetime.fromisoformat(f"{head}+{tail}")
        raise


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
