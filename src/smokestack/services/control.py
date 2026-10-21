"""Grill control: send an approved command, confirm it, and audit it.

Built as docs/specs/2026-10-19-grill-control.md says. The commands are
turn-off, set-temperature, a WiFi-module reboot and (#58) switching the
grill's unit while it is off; there is no power-on.
The grill's own password is changed too
(docs/specs/2026-10-20-grill-password-wifi.md), which is settled by
authenticating rather than by what the command returned. Every attempt that
reaches the control routes is recorded as a `control_command` health event,
accepted or rejected.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from smokestack import auth
from smokestack.services.collector import StateHub
from smokestack.services.passwords import PasswordChange, set_pending_passwords
from smokestack.sources.commands import (
    COMMANDS,
    Command,
    CommandNotSent,
    CommandUnauthorized,
    ConnectionLost,
)
from smokestack.state import temperature_unit, utc_now
from smokestack.store import CONTROL_EVENT, Database

if TYPE_CHECKING:
    from smokestack.auth import AuthState
    from smokestack.runtime import Runtime

logger = logging.getLogger(__name__)

CONTROL_HEADER = "x-smokestack-control"
CONFIRM_SECONDS = 30.0
REBOOT_CONFIRM_SECONDS = 90.0
MIN_GAP_SECONDS = 5.0
# Absolute commands: sending one twice is safe, so an Unauthorized may be
# retried once. A reboot is not, and neither is a password change: the
# reply says nothing about whether the grill took it.
RETRYABLE = frozenset({"turn-off", "set-temperature", "set-unit"})

OUTCOMES = ("confirmed", "unconfirmed", "failed", "already_applied")

# The command goes out between polls and the grill's answer comes back in the
# frames after it (the frame right after an MCU command is the blank one), so
# the confirmation window has to cover a few polls. A bridge that polls slowly
# would otherwise report a change the grill did make as unconfirmed.
POLL_INTERVALS = 3

_REASONS = {
    "disabled": "Grill control is off. Turn it on in Settings "
    "(SMOKESTACK_ENABLE_CONTROL).",
    "login_off": "Grill control needs the dashboard login switched on.",
    "unsupported": "The {source} source can't send commands.",
}


class ControlRejected(Exception):
    """A command refused before anything was sent."""

    def __init__(
        self, status_code: int, detail: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
        self.audit_id: int | None = None


@dataclass(frozen=True)
class ControlRequest:
    """What the control routes need from the HTTP request."""

    client: str | None
    session: str | None
    has_header: bool
    origin: str | None
    host: str | None


def _degrees(value: float, unit: str | None) -> str:
    return f"{value:g}°{unit or ''}"


class GrillControl:
    """Checks, sends and confirms grill commands one at a time.

    Lives as long as the app, across runtime swaps; `runtime` returns the
    current one, which supplies the source, its health and the settings.
    """

    def __init__(
        self,
        store: Database,
        hub: StateHub,
        runtime: Callable[[], Runtime],
        auth: AuthState,
        passwords: PasswordChange | None = None,
        clock: Callable[[], float] = time.monotonic,
        confirm_seconds: float = CONFIRM_SECONDS,
        reboot_confirm_seconds: float = REBOOT_CONFIRM_SECONDS,
        min_gap_seconds: float = MIN_GAP_SECONDS,
    ) -> None:
        self.store = store
        self.hub = hub
        self.runtime = runtime
        self.auth = auth
        self.passwords = passwords or PasswordChange(store)
        self.clock = clock
        self.confirm_seconds = confirm_seconds
        self.reboot_confirm_seconds = reboot_confirm_seconds
        self.min_gap_seconds = min_gap_seconds
        self._lock = asyncio.Lock()
        self._last_sent_at: float | None = None
        self.in_flight: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = store.last_control_result()

    # Status ---------------------------------------------------------------

    def unavailable(self) -> tuple[int, str] | None:
        """Why control can't be used at all, as (HTTP status, reason)."""
        runtime = self.runtime()
        if not runtime.settings.enable_control:
            return 403, _REASONS["disabled"]
        if not self.auth.required:
            return 403, _REASONS["login_off"]
        source = runtime.collector.source
        if not callable(getattr(source, "execute", None)):
            return 503, _REASONS["unsupported"].format(source=source.name)
        return None

    def status(self) -> dict[str, Any]:
        unavailable = self.unavailable()
        reason = unavailable[1] if unavailable else None
        unit = self._unit()
        pending = self.passwords.pending
        return {
            "enabled": self.runtime().settings.enable_control,
            "available": reason is None,
            "reason": reason,
            "commands": list(COMMANDS),
            "unit": unit,
            # What a set-temperature is snapped to, in the grill's unit.
            "setpoints": self._setpoints(unit) if reason is None else None,
            "in_flight": self.in_flight,
            "last_result": self.last_result,
            # Entries in the command log (#55), kept until pruned by hand.
            "log_count": self.store.count_control_events(),
            # A password change the bridge hasn't settled yet: it needs the
            # user to say which password the grill has.
            "pending_password_change": (
                {"started_at": pending.updated_at.isoformat()} if pending else None
            ),
        }

    def _unit(self) -> str | None:
        current = self.hub.current
        return temperature_unit(current.values_dict()) if current else None

    def _setpoints(self, unit: str | None) -> list[int] | None:
        accepted = getattr(self.runtime().collector.source, "accepted_setpoints", None)
        if unit is None or accepted is None:
            return None
        return list(accepted(unit == "F"))

    # Commands -------------------------------------------------------------

    async def run(
        self,
        name: str,
        request: ControlRequest,
        temp: float | None = None,
        unit: str | None = None,
        password: str | None = None,
        dashboard_password: str | None = None,
    ) -> dict[str, Any]:
        """Check, send and confirm one command, and audit the attempt.

        For set-temperature, `unit` is the unit ("F" or "C") the caller meant
        `temp` in; when given it must still be the grill's unit, so a unit
        change on the grill's panel can't reinterpret a confirmed
        temperature. For set-unit it is the unit to switch to.

        `password` is the new grill password, for `set-password`; it never
        reaches the log or the audit row.

        Returns the result (with `audit_id`); raises `ControlRejected`, also
        audited, when nothing was sent.
        """
        args: dict[str, Any] = {} if temp is None else {"temp": temp}
        if unit is not None:
            args["unit"] = unit
        try:
            result = await self._run(
                name, request, temp, unit, password, dashboard_password
            )
        except ControlRejected as exc:
            exc.audit_id = self._audit(
                name,
                args,
                request,
                {
                    "outcome": "rejected",
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                },
            )
            raise
        except BaseException as exc:
            # Cancelled (the bridge is stopping) or a bug: still record that a
            # command may have been sent, then let it propagate.
            self._audit(
                name,
                args,
                request,
                {"outcome": "failed", "detail": f"interrupted: {type(exc).__name__}"},
            )
            raise
        result = {"command": name, "args": args, **result}
        result["audit_id"] = self._audit(name, args, request, result)
        self.last_result = result
        return result

    def _audit(
        self,
        name: str,
        args: dict[str, Any],
        request: ControlRequest,
        result: dict[str, Any],
    ) -> int:
        data = {"command": name, "args": args, "client": request.client, **result}
        try:
            return self.store.insert_health_event(
                CONTROL_EVENT, f"{name}: {result['outcome']}", data
            )
        except Exception:
            logger.exception("Could not record the %s command in the audit log", name)
            return 0

    def check_request(self, request: ControlRequest) -> None:
        # The session cookie is SameSite=Strict, but another service on the
        # same site could still send it. The custom header forces a CORS
        # preflight, which the bridge never grants.
        if not request.has_header:
            raise ControlRejected(403, "missing the X-Smokestack-Control header")
        if request.origin is not None and urlsplit(request.origin).netloc != request.host:
            raise ControlRejected(403, "cross-site request")

    async def _run(
        self,
        name: str,
        request: ControlRequest,
        temp: float | None,
        unit: str | None = None,
        password: str | None = None,
        dashboard_password: str | None = None,
    ) -> dict[str, Any]:
        if name not in COMMANDS:
            raise ControlRejected(404, "not a grill command")
        self.check_request(request)
        if self._lock.locked():
            raise ControlRejected(409, "another command is still being confirmed")
        async with self._lock:
            # Checked under the lock, against the state the command is sent into.
            if not self.auth.required:
                raise ControlRejected(403, _REASONS["login_off"])
            if not self.auth.session_valid(request.session):
                raise ControlRejected(401, "not authenticated")
            unavailable = self.unavailable()
            if unavailable is not None:
                raise ControlRejected(*unavailable)
            if name == "set-password":
                # This is the only command whose failure can lock the bridge
                # out, so it takes the dashboard password again, not just a
                # session.
                await self._check_dashboard_password(dashboard_password)
            if self._last_sent_at is not None:
                wait = self.min_gap_seconds - (self.clock() - self._last_sent_at)
                if wait > 0:
                    seconds = max(1, math.ceil(wait))
                    raise ControlRejected(
                        429,
                        f"wait {seconds} s between commands",
                        {"Retry-After": str(seconds)},
                    )
            runtime = self.runtime()
            health = runtime.health
            current = self.hub.current
            if (
                not health.connected
                or current is None
                or current.is_stale(stale_after_seconds=self.hub.stale_after_seconds)
            ):
                raise ControlRejected(
                    503, "the grill isn't connected or its data is stale"
                )
            values = current.values_dict()
            command = self._command(name, temp, values, unit, password)
            started = utc_now()
            self.in_flight = {
                "command": name,
                "args": command.args,
                "started_at": started.isoformat(),
            }
            try:
                if name == "set-password":
                    result = await self._change_password(runtime, command)
                else:
                    result = await self._send_and_confirm(runtime, command, values)
            finally:
                self.in_flight = None
            unit = temperature_unit(values)
            return {
                "outcome": result["outcome"],
                "detail": result["detail"],
                "sent": result["sent"],
                "attempts": result.get("attempts", 0),
                "target": command.temp,
                "requested": temp,
                "unit": unit if name == "set-temperature" else None,
                "started_at": started.isoformat(),
                "finished_at": utc_now().isoformat(),
            }

    async def _check_dashboard_password(self, password: str | None) -> None:
        """Check the dashboard password again, for the commands that can
        lock the bridge out."""
        if not password or not await asyncio.to_thread(
            self.auth.check_password, password
        ):
            await asyncio.sleep(auth.LOGIN_FAILURE_DELAY_SECONDS)
            raise ControlRejected(403, "wrong dashboard password")

    def _command(
        self,
        name: str,
        temp: float | None,
        values: dict[str, Any],
        expected_unit: str | None = None,
        password: str | None = None,
    ) -> Command:
        if name == "set-unit":
            if expected_unit not in ("F", "C"):
                raise ControlRejected(422, 'unit must be "F" or "C"')
            # Only while the grill is off, as the latest fresh reading shows;
            # a missing moduleIsOn doesn't count as off.
            if values.get("moduleIsOn") is not False:
                raise ControlRejected(
                    409, "turn the grill off before switching its unit"
                )
            return Command("set-unit", fahrenheit=expected_unit == "F")
        if name == "set-password":
            if not password:
                raise ControlRejected(422, "password is required")
            return Command("set-password", password=password)
        if name != "set-temperature":
            return Command(name)  # type: ignore[arg-type]
        if temp is None:
            raise ControlRejected(422, "temp is required")
        unit = temperature_unit(values)
        setpoints = self._setpoints(unit)
        if unit is None:
            raise ControlRejected(409, "the grill's temperature unit isn't known yet")
        if expected_unit is not None and expected_unit != unit:
            raise ControlRejected(
                409,
                f"the grill now reports °{unit}, not °{expected_unit}; "
                "check the temperature and send it again",
            )
        if not setpoints:
            raise ControlRejected(503, "the grill's accepted setpoints aren't known")
        # Ties go to the lower setpoint, the same as pytboss.
        snapped = min(setpoints, key=lambda value: abs(value - temp))
        return Command("set-temperature", snapped)

    async def _change_password(
        self, runtime: Runtime, command: Command
    ) -> dict[str, Any]:
        """Change the grill's password, and settle it by authenticating.

        What the send returns is only a hint: a reply lost after the grill
        applied the change looks like a failure but isn't, so what decides is
        which password the grill accepts afterwards.
        """
        source = runtime.collector.source
        new = command.password or ""
        old = runtime.settings.password
        async with self.passwords.changing():
            # The recovery state first: both passwords are recorded before
            # anything is sent, so a bridge that stops mid-change comes back
            # knowing both and can reconnect without anyone at the grill.
            self.passwords.begin(old, new)
            set_pending_passwords(source, self.passwords.candidates)
            sent = await self._send(runtime, command)
            outcome, detail = await self.passwords.settle_by_asking(source)
        set_pending_passwords(source, self.passwords.candidates)
        return {
            "outcome": outcome,
            "detail": detail,
            "sent": sent["sent"],
            "attempts": sent.get("attempts", 1),
        }

    async def _send_and_confirm(
        self, runtime: Runtime, command: Command, values: dict[str, Any]
    ) -> dict[str, Any]:
        unit = temperature_unit(values)
        if command.name == "turn-off" and values.get("moduleIsOn") is False:
            return {
                "outcome": "already_applied",
                "detail": "The grill was already off.",
                "sent": False,
            }
        if (
            command.name == "set-unit"
            and values.get("isFahrenheit") is command.fahrenheit
        ):
            return {
                "outcome": "already_applied",
                "detail": f"The grill already uses °{command.args['unit']}.",
                "sent": False,
            }
        if command.name == "set-temperature" and values.get("grillSetTemp") == command.temp:
            return {
                "outcome": "already_applied",
                "detail": f"The grill was already set to {_degrees(command.temp, unit)}.",
                "sent": False,
            }

        health = runtime.health
        reconnects = health.reconnect_count
        reboot = command.name == "reboot"
        window = self._window(
            runtime, self.reboot_confirm_seconds if reboot else self.confirm_seconds
        )
        # Subscribe first so no frame after the send can be missed.
        frames = self.hub.subscribe()
        try:
            if reboot:
                runtime.hold_stale_alert(window)
            sent = await self._send(runtime, command)
            if sent["sent"]:
                # Only a command that may have reached the grill starts the gap.
                self._last_sent_at = self.clock()
            if reboot and sent["outcome"] != "failed":
                # Waiting in the queue and sending can take a while; restart
                # the hold so it covers the same window as the deadline below.
                runtime.hold_stale_alert(window)
            if sent["outcome"] == "failed":
                if reboot:
                    runtime.hold_stale_alert(0)
                return sent
            deadline = self.clock() + window
            while (remaining := deadline - self.clock()) > 0:
                try:
                    frame = await asyncio.wait_for(frames.get(), remaining)
                except TimeoutError:
                    break
                frame_values = frame.values_dict()
                if reboot:
                    # A new session counts once the frame that started it
                    # was published; the collector updates health after that.
                    done = health.reconnect_count > reconnects
                elif command.name == "turn-off":
                    done = frame_values.get("moduleIsOn") is False
                elif command.name == "set-unit":
                    done = frame_values.get("isFahrenheit") is command.fahrenheit
                else:
                    done = frame_values.get("grillSetTemp") == command.temp
                if done:
                    if reboot:
                        runtime.hold_stale_alert(0)
                    return {
                        **sent,
                        "outcome": "confirmed",
                        "detail": self._confirmed(command, unit),
                    }
            return {
                **sent,
                "outcome": "unconfirmed",
                "detail": f"Sent, but the grill didn't report the change within "
                f"{window:g} s. Check the grill.",
            }
        finally:
            self.hub.unsubscribe(frames)

    def _window(self, runtime: Runtime, seconds: float) -> float:
        """The confirmation window: `seconds`, or longer when the source polls
        slowly.

        The grill's answer arrives in the frames after the command, one poll
        apart, so a source that polls every 30 seconds needs more than the
        default window to see the change it made.
        """
        return max(seconds, POLL_INTERVALS * runtime.settings.poll_seconds)

    @staticmethod
    def _confirmed(command: Command, unit: str | None) -> str:
        if command.name == "turn-off":
            return "The grill reports that it is off."
        if command.name == "set-temperature":
            return f"The grill is set to {_degrees(command.temp, unit)}."
        if command.name == "set-unit":
            return f"The grill now uses °{command.args['unit']}."
        return "The WiFi module restarted and the grill is reporting again."

    async def _send(self, runtime: Runtime, command: Command) -> dict[str, Any]:
        source = runtime.collector.source
        attempts = 2 if command.name in RETRYABLE else 1
        for attempt in range(1, attempts + 1):
            try:
                await source.execute(command)
            except CommandUnauthorized as exc:
                if attempt < attempts:
                    logger.warning(
                        "Grill rejected %s as Unauthorized; retrying once", command.name
                    )
                    continue
                return self._failed(exc, attempt, sent=True)
            except ConnectionLost as exc:
                if command.name == "reboot":
                    # The module restarts before it can answer.
                    return {"outcome": "sent", "sent": True, "attempts": attempt}
                return self._failed(exc, attempt, sent=True)
            except CommandNotSent as exc:
                return self._failed(exc, attempt, sent=False)
            except Exception as exc:
                return self._failed(exc, attempt, sent=True)
            return {"outcome": "sent", "sent": True, "attempts": attempt}
        raise AssertionError("unreachable")

    @staticmethod
    def _failed(exc: Exception, attempts: int, sent: bool) -> dict[str, Any]:
        message = str(exc) or type(exc).__name__
        logger.warning("Grill command failed: %s", message)
        return {
            "outcome": "failed",
            "detail": f"The grill didn't take the command: {message}",
            "sent": sent,
            "attempts": attempts,
        }
