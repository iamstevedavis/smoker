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
from dataclasses import dataclass, field, replace
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
        self._first_health_at: float | None = None

    def _rule(self, key: str) -> _RuleState:
        return self._rules.setdefault(key, _RuleState())

    def inherit(self, previous: AlertEngine) -> None:
        """Take over the fired/armed state of the engine this one replaces,
        so applying settings doesn't re-send alerts for ongoing conditions.

        A probe whose target changed starts fresh: the new target is a new
        alert. The no-reading clock is not carried, since it belongs to the
        old collector's health.
        """
        self._rules = {key: replace(state) for key, state in previous._rules.items()}
        self._forget_changed_targets(previous.config)

    def set_probe_targets(self, probe1: float | None, probe2: float | None) -> None:
        """Change the probe targets in place, keeping every other rule's state."""
        previous = self.config
        self.config = replace(previous, probe1_target=probe1, probe2_target=probe2)
        self._forget_changed_targets(previous)

    def _forget_changed_targets(self, previous: AlertConfig) -> None:
        for number in (1, 2):
            attr = f"probe{number}_target"
            if getattr(self.config, attr) != getattr(previous, attr):
                self._rules.pop(attr, None)

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
        now = self._clock()
        if self._first_health_at is None:
            self._first_health_at = now
        age = health.get("last_update_age_seconds")
        if age is None:
            # No reading yet: count from when we started watching, so a grill
            # that is unreachable at startup still raises a disconnect alert.
            age = now - self._first_health_at
        key = "disconnected"
        rule = self._rule(key)
        if age > self.config.stale_seconds:
            # The temperature was not observed during the outage, so it must
            # not count towards a sustained drift once readings resume, and
            # drift after recovery is a new incident.
            drift = self._rule("grill_drift")
            drift.since = None
            drift.armed = True
            fired = self._fire(
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
            if fired:
                # Each disconnect alert earns one recovery alert, which goes
                # through _fire so the cooldown applies to it too.
                self._rule("recovered").armed = True
            return fired
        if not rule.armed:
            rule.armed = True
            return self._fire(
                "recovered",
                Notification(
                    "Grill data recovered",
                    "Readings are flowing again.",
                    priority=3,
                    tags=("white_check_mark",),
                    key="recovered",
                ),
            )
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
            elif temp <= target - PROBE_HYSTERESIS:
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
            # Back in range, or the grill was turned off: the incident is over.
            if usable or values.get("moduleIsOn") is False:
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
        # A snapshot published before we subscribed is not in the queue.
        if self.hub.current is not None:
            self.dispatch(self.engine.on_snapshot(self.hub.current))
        # Check health now so the no-reading clock starts at startup, not one
        # interval later.
        self.dispatch(self.engine.on_health(self.health.to_api_dict()))
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
