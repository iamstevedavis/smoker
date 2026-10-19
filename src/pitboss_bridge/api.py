"""FastAPI app for Smokestack."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from pitboss_bridge import auth, overrides
from pitboss_bridge.config import EDITABLE_FIELDS, Settings
from pitboss_bridge.runtime import Runtime, configure_logging, make_source
from pitboss_bridge.services.collector import StateHub
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


class LoginBody(BaseModel):
    password: str


# Re-exported under its old name; tests import it from here.
_make_source = make_source


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI app."""
    base_settings = settings or Settings.from_env()
    store = Database(base_settings.db_path)
    store.initialize()
    saved = overrides.clean_overrides(store.load_settings())
    app_settings = base_settings.with_overrides(saved)
    configure_logging(app_settings.log_level)
    hub = StateHub(stale_after_seconds=app_settings.stale_after_seconds)
    runtime = Runtime(app_settings, store, hub)

    def use_runtime(new: Runtime, new_overrides: dict[str, Any]) -> None:
        # Handlers read app.state, so they always see the current runtime.
        app.state.runtime = new
        app.state.overrides = new_overrides
        app.state.settings = new.settings
        app.state.health = new.health
        app.state.collector = new.collector
        hub.stale_after_seconds = new.settings.stale_after_seconds

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.runtime.start()
        try:
            yield
        finally:
            await app.state.runtime.stop()

    signer: auth.SessionSigner | None = None
    if base_settings.ui_password:
        signer = auth.SessionSigner(
            auth.load_or_create_key(base_settings.db_path.parent / auth.KEY_FILENAME),
            password=base_settings.ui_password,
            max_age_seconds=base_settings.session_days * 86400,
        )

    app = FastAPI(title="Smokestack", lifespan=lifespan)
    if signer is not None:
        app.add_middleware(auth.AuthGate, signer=signer)
    app.state.store = store
    app.state.hub = hub
    use_runtime(runtime, saved)

    def collector_sync_collect_next() -> None:
        asyncio.run(app.state.collector.collect_next())

    app.state.collector_sync_collect_next = collector_sync_collect_next

    @app.get("/api/health")
    async def health_endpoint() -> dict[str, Any]:
        return app.state.health.to_api_dict()

    @app.get("/api/session")
    async def session_endpoint() -> dict[str, Any]:
        return {"auth_enabled": signer is not None}

    if signer is not None:
        # Registered only with a password set, so auth-off deployments keep
        # exactly the routes they had before.
        # Attempts are checked one at a time and a failure holds the lock
        # through its delay, so concurrent guesses can't share one wait.
        login_lock = asyncio.Lock()

        @app.post("/api/login", status_code=204)
        async def login_endpoint(body: LoginBody) -> Response:
            async with login_lock:
                if not auth.password_matches(body.password, base_settings.ui_password):
                    await asyncio.sleep(auth.LOGIN_FAILURE_DELAY_SECONDS)
                    raise HTTPException(status_code=401, detail="wrong password")
            response = Response(status_code=204)
            # No `Secure`: the bridge serves plain HTTP on the LAN.
            response.set_cookie(
                auth.COOKIE_NAME,
                signer.issue(),
                max_age=signer.max_age_seconds,
                path="/",
                httponly=True,
                samesite="strict",
            )
            return response

        @app.post("/api/logout", status_code=204)
        async def logout_endpoint() -> Response:
            response = Response(status_code=204)
            response.delete_cookie(
                auth.COOKIE_NAME, path="/", httponly=True, samesite="strict"
            )
            return response

    apply_lock = asyncio.Lock()

    def current_settings_payload() -> dict[str, Any]:
        return overrides.settings_payload(
            base_settings, app.state.overrides, app.state.settings
        )

    def require_editable() -> None:
        reason = base_settings.settings_read_only()
        if reason == "disabled":
            raise HTTPException(
                status_code=403,
                detail="settings are read-only (PITBOSS_SETTINGS_EDITABLE=false)",
            )
        if reason == "no_login":
            raise HTTPException(
                status_code=403,
                detail="set PITBOSS_UI_PASSWORD to edit settings from the dashboard",
            )

    def build_runtime(new_overrides: dict[str, Any], changes: dict[str, Any]) -> Runtime:
        """Build (but don't start) the runtime for `new_overrides`.

        Raises 422 if the settings can't be built. Callers persist only after
        this returns, so a rejected update changes nothing.
        """
        effective = base_settings.with_overrides(new_overrides)
        try:
            overrides.check_source(effective, changes)
            new = Runtime(effective, store, hub)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return new

    async def swap(new: Runtime, new_overrides: dict[str, Any]) -> None:
        old: Runtime = app.state.runtime
        await old.stop()
        if new.same_grill(old):
            new.take_over_from(old)
        else:
            # Don't show the old grill's last reading as the new one's state.
            hub.current = None
        configure_logging(new.settings.log_level)
        use_runtime(new, new_overrides)
        await new.start()

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return current_settings_payload()

    @app.put("/api/settings")
    async def put_settings(body: overrides.SettingsUpdate) -> dict[str, Any]:
        require_editable()
        changes = body.changes()
        if not changes:
            return current_settings_payload()
        async with apply_lock:
            new_overrides = {**app.state.overrides, **changes}
            new = build_runtime(new_overrides, changes)
            store.save_settings(changes)
            await swap(new, new_overrides)
        return current_settings_payload()

    @app.delete("/api/settings/{name}")
    async def reset_setting(name: str) -> dict[str, Any]:
        require_editable()
        if name not in EDITABLE_FIELDS:
            raise HTTPException(status_code=404, detail="not an editable setting")
        async with apply_lock:
            if name in app.state.overrides:
                new_overrides = {
                    key: value
                    for key, value in app.state.overrides.items()
                    if key != name
                }
                new = build_runtime(new_overrides, {name: None})
                store.delete_setting(name)
                await swap(new, new_overrides)
        return current_settings_payload()

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
                    snapshot.to_api_dict(stale_after_seconds=hub.stale_after_seconds)
                )
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(queue)

    mount_dashboard(app, login_page=signer is not None)

    return app
