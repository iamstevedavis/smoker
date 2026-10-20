"""FastAPI app for Smokestack."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ConfigDict, Field

from smokestack import auth, overrides
from smokestack.config import EDITABLE_FIELDS, Settings
from smokestack.runtime import Runtime, configure_logging, make_source
from smokestack.services.collector import StateHub
from smokestack.store import Database
from smokestack.web import mount_dashboard


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


class NewPassword(BaseModel):
    password: str = Field(min_length=auth.PASSWORD_MIN_LENGTH)


class PasswordChange(BaseModel):
    current: str
    new: str = Field(min_length=auth.PASSWORD_MIN_LENGTH)


class AuthUpdate(BaseModel):
    required: bool
    # Needed to turn the password back on, so the caller can be signed in.
    password: str | None = None


class ProbeTargetsUpdate(BaseModel):
    """A partial update; null clears a probe's alert."""

    model_config = ConfigDict(extra="forbid")

    probe1: float | None = Field(default=None, gt=0, le=1000)
    probe2: float | None = Field(default=None, gt=0, le=1000)


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

    auth_state = auth.AuthState(
        store,
        auth.load_or_create_key(base_settings.db_path.parent / auth.KEY_FILENAME),
    )

    app = FastAPI(title="Smokestack", lifespan=lifespan)
    app.add_middleware(auth.AuthGate, state=auth_state)
    app.state.store = store
    app.state.auth = auth_state
    app.state.hub = hub
    use_runtime(runtime, saved)

    def collector_sync_collect_next() -> None:
        asyncio.run(app.state.collector.collect_next())

    app.state.collector_sync_collect_next = collector_sync_collect_next

    @app.get("/api/health")
    async def health_endpoint() -> dict[str, Any]:
        return app.state.health.to_api_dict()

    def session_payload(request: Request) -> dict[str, Any]:
        return {
            "setup_required": auth_state.setup_required,
            "auth_enabled": auth_state.required,
            "authenticated": auth_state.session_valid(
                request.cookies.get(auth.COOKIE_NAME)
            ),
        }

    def signed_in(response: Response) -> Response:
        # No `Secure`: the bridge serves plain HTTP on the LAN.
        response.set_cookie(
            auth.COOKIE_NAME,
            auth_state.issue(),
            max_age=auth_state.max_age_seconds,
            path="/",
            httponly=True,
            samesite="strict",
        )
        return response

    # Attempts are checked one at a time and a failure holds the lock through
    # its delay, so concurrent guesses can't share one wait.
    login_lock = asyncio.Lock()

    async def require_password(password: str, status_code: int) -> None:
        """Raise unless `password` is right; call with `login_lock` held."""
        if not await asyncio.to_thread(auth_state.check_password, password):
            await asyncio.sleep(auth.LOGIN_FAILURE_DELAY_SECONDS)
            raise HTTPException(status_code=status_code, detail="wrong password")

    @app.get("/api/session")
    async def session_endpoint(request: Request) -> dict[str, Any]:
        return session_payload(request)

    @app.post("/api/setup", status_code=204)
    async def setup_endpoint(body: NewPassword) -> Response:
        async with login_lock:
            if not auth_state.setup_required:
                raise HTTPException(status_code=409, detail="a password is already set")
            await asyncio.to_thread(auth_state.set_password, body.password)
        return signed_in(Response(status_code=204))

    @app.post("/api/login", status_code=204)
    async def login_endpoint(body: LoginBody) -> Response:
        if auth_state.setup_required:
            raise HTTPException(status_code=409, detail="setup required")
        async with login_lock:
            await require_password(body.password, 401)
        return signed_in(Response(status_code=204))

    @app.post("/api/logout", status_code=204)
    async def logout_endpoint() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(
            auth.COOKIE_NAME, path="/", httponly=True, samesite="strict"
        )
        return response

    # Behind the gate. Wrong passwords answer 403, not 401, so the dashboard
    # doesn't take them as a signed-out session.
    @app.put("/api/auth")
    async def update_auth(body: AuthUpdate) -> Response:
        response = Response(status_code=204)
        async with login_lock:
            if body.required and not auth_state.required:
                await require_password(body.password or "", 403)
                auth_state.set_required(True)
                signed_in(response)
            elif not body.required:
                auth_state.set_required(False)
        return response

    @app.post("/api/auth/password", status_code=204)
    async def change_password(body: PasswordChange) -> Response:
        async with login_lock:
            await require_password(body.current, 403)
            await asyncio.to_thread(auth_state.set_password, body.new)
        # Every other session ends; keep this one signed in.
        return signed_in(Response(status_code=204))

    apply_lock = asyncio.Lock()

    def current_settings_payload() -> dict[str, Any]:
        return overrides.settings_payload(
            base_settings, app.state.overrides, app.state.settings
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

    def probe_targets_payload() -> dict[str, Any]:
        targets = store.load_probe_targets()
        return {
            "probe1": targets.get(1),
            "probe2": targets.get(2),
            # Targets are kept either way, but only sent while ntfy is set up.
            "alerts_enabled": app.state.runtime.alerts is not None,
        }

    @app.get("/api/probe-targets")
    async def get_probe_targets() -> dict[str, Any]:
        return probe_targets_payload()

    @app.put("/api/probe-targets")
    async def put_probe_targets(body: ProbeTargetsUpdate) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        # Under the apply lock so a settings swap can't build its runtime
        # from targets that are about to change.
        async with apply_lock:
            store.save_probe_targets(
                {int(name.removeprefix("probe")): value for name, value in changes.items()}
            )
            targets = store.load_probe_targets()
            app.state.runtime.set_probe_targets(targets.get(1), targets.get(2))
        return probe_targets_payload()

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
        token = websocket.cookies.get(auth.COOKIE_NAME)
        queue = hub.subscribe()
        try:
            if hub.current is not None:
                await websocket.send_json(hub.current_api_dict())
            while True:
                snapshot = await queue.get()
                # The gate only checked the handshake. Turning the password
                # on, changing it or the session expiring must also end
                # sockets that are already open.
                if not auth_state.allows(token):
                    await websocket.close(code=1008)
                    return
                await websocket.send_json(
                    snapshot.to_api_dict(stale_after_seconds=hub.stale_after_seconds)
                )
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(queue)

    mount_dashboard(app)

    return app
