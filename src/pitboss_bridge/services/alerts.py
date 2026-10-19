"""Push notifications (ntfy) for probe targets, drift, disconnects and errors.

The engine is pure: it turns snapshots and health into `Notification`s and keeps
the per-rule de-duplication state. `AlertService` wires it to the `StateHub`
and sends in background tasks so the collector is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pitboss_bridge.services.collector import CollectorHealth, StateHub
from pitboss_bridge.state import StateSnapshot
from pitboss_bridge.store import Database

logger = logging.getLogger(__name__)

ERROR_FLAGS = (
    "err1",
    "err2",
    "err3",
    "highTempErr",
    "fanErr",
    "hotErr",
    "motorErr",
    "noPellets",
    "erL",
)
PROBE_HYSTERESIS = 5.0


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    priority: int = 3  # ntfy: 1 min .. 5 max/urgent
    tags: tuple[str, ...] = ()
    key: str = ""


class Sender(Protocol):
    async def send(self, notification: Notification) -> None: ...


@dataclass(frozen=True)
class AlertConfig:
    probe1_target: float | None = None
    probe2_target: float | None = None
    drift_degrees: float = 25.0
    drift_seconds: float = 600.0
    stale_seconds: float = 60.0
    cooldown_seconds: float = 0.0


class NtfySender:
    """POST to an ntfy topic URL with only the standard library."""

    def __init__(self, url: str, token: str = "", timeout: float = 10.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout

    async def send(self, notification: Notification) -> None:
        await asyncio.to_thread(self._post, notification)

    def _post(self, notification: Notification) -> None:
        headers = {
            "Title": notification.title,
            "Priority": str(notification.priority),
        }
        if notification.tags:
            headers["Tags"] = ",".join(notification.tags)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.url,
            data=notification.message.encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout):
            pass


@dataclass
class _RuleState:
    armed: bool = True
    last_fired: float | None = None
    since: float | None = None


class AlertEngine:
    """Evaluates the alert rules. `clock` returns seconds (monotonic)."""

    def __init__(
        self,
        config: AlertConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._clock = clock
        self._rules: dict[str, _RuleState] = {}

    def _rule(self, key: str) -> _RuleState:
        return self._rules.setdefault(key, _RuleState())

    def _fire(self, key: str, notification: Notification) -> list[Notification]:
        """Fire once per incident, honouring the optional cooldown."""
        rule = self._rule(key)
        if not rule.armed:
            return []
        now = self._clock()
        cooldown = self.config.cooldown_seconds
        if (
            cooldown
            and rule.last_fired is not None
            and now - rule.last_fired < cooldown
        ):
            return []
        rule.armed = False
        rule.last_fired = now
        return [notification]

    def on_snapshot(self, snapshot: StateSnapshot) -> list[Notification]:
        values = snapshot.values_dict()
        out: list[Notification] = []
        out += self._probe_rules(values)
        out += self._drift_rule(values)
        out += self._error_rules(values)
        return out

    def on_health(self, health: dict[str, Any]) -> list[Notification]:
        age = health.get("last_update_age_seconds")
        if age is None:
            return []
        key = "disconnected"
        rule = self._rule(key)
        if age > self.config.stale_seconds:
            return self._fire(
                key,
                Notification(
                    "Grill data lost",
                    f"No reading from the {health.get('source', 'grill')} source "
                    f"for {int(age)} s.",
                    priority=4,
                    tags=("warning",),
                    key=key,
                ),
            )
        if not rule.armed:
            rule.armed = True
            return [
                Notification(
                    "Grill data recovered",
                    "Readings are flowing again.",
                    priority=3,
                    tags=("white_check_mark",),
                    key="recovered",
                )
            ]
        return []

    def _probe_rules(self, values: dict[str, Any]) -> list[Notification]:
        out: list[Notification] = []
        targets = {1: self.config.probe1_target, 2: self.config.probe2_target}
        for number, target in targets.items():
            temp = values.get(f"p{number}Temp")
            if target is None or not isinstance(temp, (int, float)):
                continue
            key = f"probe{number}_target"
            rule = self._rule(key)
            if temp >= target:
                out += self._fire(
                    key,
                    Notification(
                        f"Probe {number} reached {target:g}",
                        f"Probe {number} is at {temp:g} (target {target:g}).",
                        priority=4,
                        tags=("meat_on_bone",),
                        key=key,
                    ),
                )
            elif temp < target - PROBE_HYSTERESIS:
                rule.armed = True
        return out

    def _drift_rule(self, values: dict[str, Any]) -> list[Notification]:
        key = "grill_drift"
        rule = self._rule(key)
        temp, set_temp = values.get("grillTemp"), values.get("grillSetTemp")
        usable = (
            values.get("moduleIsOn")
            and isinstance(temp, (int, float))
            and isinstance(set_temp, (int, float))
        )
        if not usable or abs(temp - set_temp) <= self.config.drift_degrees:
            rule.since = None
            if usable:
                rule.armed = True
            return []
        now = self._clock()
        if rule.since is None:
            rule.since = now
        if now - rule.since < self.config.drift_seconds:
            return []
        return self._fire(
            key,
            Notification(
                "Grill temperature drifting",
                f"Grill is at {temp:g} but set to {set_temp:g} "
                f"(off by {abs(temp - set_temp):g} for {int(now - rule.since)} s).",
                priority=4,
                tags=("thermometer",),
                key=key,
            ),
        )

    def _error_rules(self, values: dict[str, Any]) -> list[Notification]:
        out: list[Notification] = []
        for flag in ERROR_FLAGS:
            key = f"error_{flag}"
            if values.get(flag) is True:
                out += self._fire(
                    key,
                    Notification(
                        "Out of pellets" if flag == "noPellets" else f"Grill error: {flag}",
                        "The hopper is empty."
                        if flag == "noPellets"
                        else f"The grill reported {flag}.",
                        priority=5 if flag == "noPellets" else 4,
                        tags=("rotating_light",),
                        key=key,
                    ),
                )
            elif values.get(flag) is False:
                self._rule(key).armed = True
        return out


@dataclass
class AlertService:
    """Feeds hub snapshots and health to the engine and sends in the background."""

    engine: AlertEngine
    hub: StateHub
    health: CollectorHealth
    sender: Sender
    store: Database | None = None
    check_interval_seconds: float = 5.0
    send_timeout_seconds: float = 10.0
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    async def run(self) -> None:
        queue = self.hub.subscribe()
        try:
            while True:
                try:
                    snapshot = await asyncio.wait_for(
                        queue.get(), timeout=self.check_interval_seconds
                    )
                    self.dispatch(self.engine.on_snapshot(snapshot))
                except asyncio.TimeoutError:
                    pass
                self.dispatch(self.engine.on_health(self.health.to_api_dict()))
        finally:
            self.hub.unsubscribe(queue)
            for task in list(self._tasks):
                task.cancel()

    def dispatch(self, notifications: list[Notification]) -> None:
        for notification in notifications:
            task = asyncio.create_task(self._send(notification))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _send(self, notification: Notification) -> None:
        try:
            await asyncio.wait_for(
                self.sender.send(notification), timeout=self.send_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            logger.warning("Alert %r failed to send: %s", notification.key, message)
            self._record("alert_error", message, notification)
        else:
            logger.info("Sent alert %r", notification.key)
            self._record("alert_sent", notification.title, notification)

    def _record(self, event_type: str, message: str, notification: Notification) -> None:
        if self.store is None:
            return
        try:
            self.store.insert_health_event(
                event_type, message, {"rule": notification.key}
            )
        except Exception:
            logger.debug("Could not record alert event", exc_info=True)
