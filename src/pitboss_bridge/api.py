"""FastAPI app for the Pit Boss bridge."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from pitboss_bridge.config import Settings
from pitboss_bridge.services.alerts import (
    AlertConfig,
    AlertEngine,
    AlertService,
    NtfySender,
)
from pitboss_bridge.services.collector import Collector, CollectorHealth, StateHub
from pitboss_bridge.services.mqtt import MqttConfig, MqttPublisher
from pitboss_bridge.sources.fake import FakeGrillSource
from pitboss_bridge.store import Database
from pitboss_bridge.web import mount_dashboard


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        head, separator, tail = value.rpartition(" ")
        if separator and tail.count(":") == 1 and tail[0].isdigit():
            return datetime.fromisoformat(f"{head}+{tail}")
        raise


class CookCreate(BaseModel):
    name: str = Field(min_length=1)
    notes: str = ""
    meat: str | None = None
    weight: str | None = None
    target_internal_temp: float | None = None


class CookUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    notes: str | None = None


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
    if settings.source == "http":
        from pitboss_bridge.sources.pytboss_http import PytbossHttpSource

        return PytbossHttpSource(
            model=settings.model,
            host=settings.host,
            password=settings.password,
            poll_seconds=settings.poll_seconds,
        )
    raise ValueError(f"Unsupported PITBOSS_SOURCE={settings.source!r}")


def _configure_logging(level: str) -> None:
    # uvicorn only configures its own loggers, so without this the bridge's
    # INFO logs (connects, disconnects, retries) would be silently dropped.
    logger = logging.getLogger("pitboss_bridge")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.propagate = False


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI app."""
    app_settings = settings or Settings.from_env()
    _configure_logging(app_settings.log_level)
    store = Database(app_settings.db_path)
    store.initialize()
    hub = StateHub(stale_after_seconds=app_settings.stale_after_seconds)
    source = _make_source(app_settings)
    health = CollectorHealth(
        source=source.name,
        stale_after_seconds=app_settings.stale_after_seconds,
    )
    collector = Collector(
        source=source,
        store=store,
        hub=hub,
        health=health,
        retention_days=app_settings.retention_days,
    )

    mqtt: MqttPublisher | None = None
    if app_settings.mqtt_host:
        mqtt = MqttPublisher(
            MqttConfig(
                host=app_settings.mqtt_host,
                port=app_settings.mqtt_port,
                user=app_settings.mqtt_user,
                password=app_settings.mqtt_password,
                prefix=app_settings.mqtt_prefix,
                model=app_settings.model,
            ),
            hub=hub,
            health=health,
        )

    alerts: AlertService | None = None
    if app_settings.ntfy_url:
        alerts = AlertService(
            engine=AlertEngine(
                AlertConfig(
                    probe1_target=app_settings.alert_probe1_target,
                    probe2_target=app_settings.alert_probe2_target,
                    drift_degrees=app_settings.alert_drift_degrees,
                    drift_seconds=app_settings.alert_drift_minutes * 60,
                    stale_seconds=app_settings.alert_stale_seconds,
                    cooldown_seconds=app_settings.alert_cooldown_seconds,
                )
            ),
            hub=hub,
            health=health,
            sender=NtfySender(app_settings.ntfy_url, app_settings.ntfy_token),
            store=store,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task[None]] = []
        # Alerts first, so they subscribe before the collector's first reading.
        if alerts is not None:
            tasks.append(asyncio.create_task(alerts.run()))
        if mqtt is not None:
            tasks.append(asyncio.create_task(mqtt.run()))
        if app_settings.start_collector:
            tasks.append(asyncio.create_task(collector.run()))
        try:
            yield
        finally:
            collector.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
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
        until: str | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=10000),
        order: Literal["asc", "desc"] = Query(default="asc"),
        bucket_seconds: int | None = Query(default=None, ge=1, le=86400),
    ) -> dict[str, Any]:
        rows = store.list_history(
            since=_parse_time(since),
            until=_parse_time(until),
            limit=limit,
            newest_first=order == "desc",
            bucket_seconds=bucket_seconds,
        )
        return {"count": len(rows), "items": rows}

    def _require_cook(cook_id: int) -> dict[str, Any]:
        cook = store.get_cook(cook_id)
        if cook is None:
            raise HTTPException(status_code=404, detail="cook not found")
        return cook

    @app.post("/api/cooks", status_code=201)
    async def start_cook(body: CookCreate) -> dict[str, Any]:
        cook = store.start_cook(**body.model_dump())
        if cook is None:
            raise HTTPException(status_code=409, detail="a cook is already active")
        return cook

    @app.get("/api/cooks")
    async def list_cooks(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
        cooks = store.list_cooks(limit=limit)
        return {"count": len(cooks), "items": cooks}

    @app.get("/api/cooks/{cook_id}")
    async def get_cook(cook_id: int) -> dict[str, Any]:
        cook = _require_cook(cook_id)
        return {**cook, "summary": store.cook_summary(cook)}

    @app.post("/api/cooks/{cook_id}/stop")
    async def stop_cook(cook_id: int) -> dict[str, Any]:
        _require_cook(cook_id)
        cook = store.stop_cook(cook_id)
        if cook is None:
            raise HTTPException(status_code=409, detail="cook is already stopped")
        return cook

    @app.patch("/api/cooks/{cook_id}")
    async def update_cook(cook_id: int, body: CookUpdate) -> dict[str, Any]:
        _require_cook(cook_id)
        return store.update_cook(cook_id, **body.model_dump(exclude_none=True))

    @app.get("/api/cooks/{cook_id}/history")
    async def cook_history(
        cook_id: int,
        limit: int = Query(default=1000, ge=1, le=10000),
        order: Literal["asc", "desc"] = Query(default="asc"),
        bucket_seconds: int | None = Query(default=None, ge=1, le=86400),
    ) -> dict[str, Any]:
        cook = _require_cook(cook_id)
        rows = store.list_history(
            since=_parse_time(cook["started_at"]),
            until=_parse_time(cook["ended_at"]),
            limit=limit,
            newest_first=order == "desc",
            bucket_seconds=bucket_seconds,
        )
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

    mount_dashboard(app)

    return app
