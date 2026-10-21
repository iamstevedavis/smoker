"""FastAPI app for Smokestack."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from smokestack import __version__, auth, overrides
from smokestack.config import EDITABLE_FIELDS, Settings
from smokestack.runtime import Runtime, configure_logging, make_source
from smokestack.services.collector import StateHub
from smokestack.services.control import (
    CONTROL_HEADER,
    ControlRejected,
    ControlRequest,
    GrillControl,
)
from smokestack.services.passwords import PasswordChange
from smokestack.state import temperature_unit, utc_now
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


class DashboardPasswordChange(BaseModel):
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
    # The unit the targets were entered in; defaults to the grill's current one.
    unit: Literal["F", "C"] | None = None


class SetTemperatureBody(BaseModel):
    """A grill setpoint in the grill's current unit; snapped to what the
    board accepts."""

    model_config = ConfigDict(extra="forbid")

    temp: float = Field(gt=0, le=1000)
    # The unit `temp` was meant in; refused (409) if the grill's unit differs.
    unit: Literal["F", "C"] | None = None


class SetPasswordBody(BaseModel):
    """A new password for the grill itself.

    The dashboard password comes along: this is the one command whose
    failure can lock the bridge out, so it takes more than a session.
    """

    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1)
    # The same password again, so a typo isn't sent to the grill.
    confirm: str = Field(min_length=1)
    dashboard_password: str = Field(min_length=1)


class ControlLogPrune(BaseModel):
    """Delete grill command log entries: all of them, or those older than
    `older_than_days` (or a fixed `cutoff`). `dry_run` only counts what
    would go, and answers the `cutoff` and `max_id` to send back so the
    delete covers exactly the entries that were counted."""

    model_config = ConfigDict(extra="forbid")

    older_than_days: float | None = Field(default=None, gt=0, le=36500)
    cutoff: datetime | None = None
    max_id: int | None = Field(default=None, ge=0)
    dry_run: bool = False

    @model_validator(mode="after")
    def _one_age(self) -> "ControlLogPrune":
        if self.older_than_days is not None and self.cutoff is not None:
            raise ValueError("give older_than_days or cutoff, not both")
        if self.cutoff is not None and self.cutoff.tzinfo is None:
            raise ValueError("cutoff needs a timezone")
        return self


class SetUnitBody(BaseModel):
    """The unit the grill itself should work in (#58)."""

    model_config = ConfigDict(extra="forbid")

    unit: Literal["F", "C"]


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

    def apply_grill_password(password: str) -> None:
        """Take a new grill password as the bridge's own setting.

        The source keeps using the password the grill accepted, so nothing
        is restarted: the bridge is already talking to the grill with it.
        """
        app.state.overrides = {**app.state.overrides, "password": password}
        app.state.settings = replace(app.state.settings, password=password)
        app.state.runtime.settings = replace(
            app.state.runtime.settings, password=password
        )

    passwords = PasswordChange(store, apply_grill_password)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.runtime.start()
        # A password change that was in flight when the bridge stopped is
        # settled here: the grill is asked which password it accepts.
        settle = asyncio.create_task(
            passwords.watch(lambda: app.state.runtime.collector.source)
        )
        try:
            yield
        finally:
            settle.cancel()
            try:
                await settle
            except asyncio.CancelledError:
                pass
            except Exception:
                # The change stays pending; the next start tries again.
                pass
            await app.state.runtime.stop()

    auth_state = auth.AuthState(
        store,
        auth.load_or_create_key(base_settings.db_path.parent / auth.KEY_FILENAME),
    )

    app = FastAPI(title="Smokestack", version=__version__, lifespan=lifespan)
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
        return {**app.state.health.to_api_dict(), "version": __version__}

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
    async def change_password(body: DashboardPasswordChange) -> Response:
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
            if "password" in changes:
                # Saving a grill password ends a pending change: the value
                # the user typed is the one the grill has. A reconcile that
                # is running waits, so it cannot overwrite it afterwards.
                await passwords.clear_now()
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
        payload: dict[str, Any] = {}
        for probe in (1, 2):
            saved = targets.get(probe)
            payload[f"probe{probe}"] = saved.target if saved else None
            # None for a target saved before units were recorded: it isn't
            # compared until it is entered again.
            payload[f"probe{probe}_unit"] = saved.unit if saved else None
        return {
            **payload,
            # Targets are kept either way, but only sent while ntfy is set up.
            "alerts_enabled": app.state.runtime.alerts is not None,
        }

    @app.get("/api/probe-targets")
    async def get_probe_targets() -> dict[str, Any]:
        return probe_targets_payload()

    @app.put("/api/probe-targets")
    async def put_probe_targets(body: ProbeTargetsUpdate) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        unit = changes.pop("unit", None)
        # Under the apply lock so a settings swap can't build its runtime
        # from targets that are about to change.
        async with apply_lock:
            # Unless the caller says otherwise, new targets are in the unit
            # the grill reports now. Clearing a target needs no unit.
            if unit is None:
                current = hub.current
                unit = temperature_unit(current.values_dict()) if current else None
            if unit is None and any(value is not None for value in changes.values()):
                raise HTTPException(
                    status_code=409,
                    detail="the grill's temperature unit isn't known yet",
                )
            store.save_probe_targets(
                {
                    int(name.removeprefix("probe")): value
                    for name, value in changes.items()
                },
                unit,
            )
            targets = store.load_probe_targets()
            app.state.runtime.set_probe_targets(targets.get(1), targets.get(2))
        return probe_targets_payload()

    control = GrillControl(store, hub, lambda: app.state.runtime, auth_state, passwords)
    app.state.control = control
    app.state.passwords = passwords

    async def run_control(
        name: str,
        request: Request,
        temp: float | None = None,
        unit: str | None = None,
        password: str | None = None,
        dashboard_password: str | None = None,
    ) -> Any:
        info = ControlRequest(
            client=request.client.host if request.client else None,
            session=request.cookies.get(auth.COOKIE_NAME),
            has_header=request.headers.get(CONTROL_HEADER) == "1",
            origin=request.headers.get("origin"),
            host=request.headers.get("host"),
        )
        try:
            return await control.run(
                name, info, temp, unit, password, dashboard_password
            )
        except ControlRejected as exc:
            return JSONResponse(
                {"detail": exc.detail, "audit_id": exc.audit_id},
                status_code=exc.status_code,
                headers=exc.headers,
            )

    # Grill control (docs/specs/2026-10-19-grill-control.md and
    # docs/specs/2026-10-20-grill-password-wifi.md). These are the only
    # routes that write to the grill, and there is no power-on.
    @app.get("/api/control")
    async def get_control() -> dict[str, Any]:
        return control.status()

    @app.post("/api/control/turn-off")
    async def control_turn_off(request: Request) -> Any:
        return await run_control("turn-off", request)

    @app.post("/api/control/set-temperature")
    async def control_set_temperature(request: Request, body: SetTemperatureBody) -> Any:
        return await run_control("set-temperature", request, body.temp, body.unit)

    @app.post("/api/control/set-unit")
    async def control_set_unit(request: Request, body: SetUnitBody) -> Any:
        return await run_control("set-unit", request, unit=body.unit)

    @app.post("/api/control/reboot")
    async def control_reboot(request: Request) -> Any:
        return await run_control("reboot", request)

    @app.post("/api/control/set-password")
    async def control_set_password(request: Request, body: SetPasswordBody) -> Any:
        if body.password != body.confirm:
            return JSONResponse(
                {"detail": "the two passwords are not the same"},
                status_code=422,
            )
        return await run_control(
            "set-password",
            request,
            password=body.password,
            dashboard_password=body.dashboard_password,
        )

    # The grill command log (#55): the control_command health events. Like
    # the controls, it needs the login on; the gate has already checked the
    # session. Pruning is the only way these rows are ever deleted.
    def require_login_on() -> None:
        if not auth_state.required:
            raise HTTPException(
                status_code=403,
                detail="the command log needs the dashboard login switched on",
            )

    @app.get("/api/control-log")
    async def get_control_log(
        limit: int = Query(default=100, ge=1, le=1000),
        before: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        require_login_on()
        items = store.list_control_events(limit=limit, before_id=before)
        return {
            "count": len(items),
            "total": store.count_control_events(),
            "items": items,
            # Pass as `before` for the next (older) page.
            "next_before": items[-1]["id"] if len(items) == limit else None,
        }

    @app.post("/api/control-log/prune")
    async def prune_control_log(request: Request, body: ControlLogPrune) -> Any:
        require_login_on()
        try:
            # The same cross-site guard as the controls: this deletes records.
            control.check_request(
                ControlRequest(
                    client=None,
                    session=None,
                    has_header=request.headers.get(CONTROL_HEADER) == "1",
                    origin=request.headers.get("origin"),
                    host=request.headers.get("host"),
                )
            )
        except ControlRejected as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        days = body.older_than_days
        cutoff = body.cutoff
        if days is not None:
            cutoff = utc_now() - timedelta(days=days)
        if body.dry_run:
            # Fix the scope now: the delete that follows sends these back, so
            # entries logged (or aged past the cutoff) while the user reads
            # the confirmation aren't deleted with the ones counted.
            max_id = store.max_control_event_id()
            return {
                "matching": store.count_control_events(cutoff, max_id),
                "deleted": 0,
                "cutoff": cutoff.isoformat() if cutoff else None,
                "max_id": max_id,
            }
        deleted = store.prune_control_events(cutoff, body.max_id)
        # The last result may have been one of them.
        control.last_result = store.last_control_result()
        # Not a control_command row, so it isn't in the log it just pruned;
        # retention pruning removes it like any other health event.
        store.insert_health_event(
            "control_log_pruned",
            f"deleted {deleted} grill command log entries",
            {
                "older_than_days": days,
                "cutoff": cutoff.isoformat() if cutoff else None,
                "max_id": body.max_id,
                "deleted": deleted,
                "client": request.client.host if request.client else None,
            },
        )
        return {
            "matching": deleted,
            "deleted": deleted,
            "total": store.count_control_events(),
        }

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
