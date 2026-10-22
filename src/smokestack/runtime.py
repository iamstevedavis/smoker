"""The grill source and the background services built from one Settings.

Applying new settings swaps the whole runtime: stop the old one, start a new
one. The store and the StateHub outlive runtimes, so the HTTP server, history
and live WebSocket subscribers carry on across a swap.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import stat
import sys
from collections.abc import Mapping
from functools import cache
from typing import Any

from smokestack.config import Settings
from smokestack.services.alerts import (
    AlertConfig,
    AlertEngine,
    AlertService,
    NtfySender,
)
from smokestack.services.collector import Collector, CollectorHealth, StateHub
from smokestack.services.mqtt import MqttConfig, MqttPublisher
from smokestack.services.passwords import set_pending_passwords
from smokestack.sources.fake import FakeGrillSource
from smokestack.store import Database, ProbeTarget

logger = logging.getLogger(__name__)

# Settings that point at a different grill. Changing one means earlier
# readings and alert state belong to another grill.
GRILL_FIELDS = ("source", "model", "host", "device_name")

# The modules each source needs, so the UI only offers what this install can run.
_SOURCE_MODULES = {
    "fake": (),
    "http": ("pytboss.http",),
    "ble": ("pytboss", "bleak"),
}


DEFAULT_SYSTEM_BUS = "/run/dbus/system_bus_socket"


def system_bus_reachable(
    platform: str | None = None, env: Mapping[str, str] | None = None
) -> bool:
    """Whether bleak can reach BlueZ: on Linux, the system D-Bus socket exists.

    bleak is always installed, so imports alone would offer Bluetooth in a
    container without the host's D-Bus socket, e.g. on Docker Desktop.
    Other platforms don't use D-Bus.
    """
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if not platform.startswith("linux"):
        return True
    address = env.get("DBUS_SYSTEM_BUS_ADDRESS") or f"unix:path={DEFAULT_SYSTEM_BUS}"
    for entry in address.split(";"):
        transport, _, params = entry.partition(":")
        if transport != "unix":
            return True  # not a socket file we can check; let bleak try
        options = dict(part.split("=", 1) for part in params.split(",") if "=" in part)
        if "path" not in options:
            return True  # abstract socket: nothing on disk to check
        try:
            if stat.S_ISSOCK(os.stat(options["path"]).st_mode):
                return True
        except OSError:
            continue
    return False


@cache
def available_sources() -> tuple[str, ...]:
    """Sources this install can run: their modules import and, for Bluetooth,
    the host's BlueZ is reachable. `fake` is always available."""
    available = []
    for name, modules in _SOURCE_MODULES.items():
        try:
            for module in modules:
                importlib.import_module(module)
        except Exception:
            continue
        if name == "ble" and not system_bus_reachable():
            continue
        available.append(name)
    return tuple(available)


def make_source(settings: Settings) -> Any:
    if settings.source == "fake":
        return FakeGrillSource(
            interval_seconds=settings.poll_seconds,
            password=settings.password,
        )
    if settings.source == "ble":
        from smokestack.sources.pytboss_ble import PytbossBleSource

        return PytbossBleSource(
            model=settings.model,
            device_name=settings.device_name,
            adapter=settings.ble_adapter,
            password=settings.password,
            poll_seconds=settings.poll_seconds,
        )
    if settings.source == "http":
        from smokestack.sources.pytboss_http import PytbossHttpSource

        return PytbossHttpSource(
            model=settings.model,
            host=settings.host,
            password=settings.password,
            poll_seconds=settings.poll_seconds,
        )
    raise ValueError(f"Unsupported PITBOSS_SOURCE={settings.source!r}")


def configure_logging(level: str) -> None:
    # uvicorn only configures its own loggers, so without this the bridge's
    # INFO logs (connects, disconnects, retries) would be silently dropped.
    logger = logging.getLogger("smokestack")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.propagate = False


class Runtime:
    """Owns the source, collector, MQTT publisher and alert service."""

    def __init__(self, settings: Settings, store: Database, hub: StateHub) -> None:
        self.settings = settings
        source = make_source(settings)
        # A password change that was in flight when the bridge stopped: the
        # grill may hold either password, so the source retries them.
        pending = store.load_pending_password()
        if pending is not None:
            set_pending_passwords(
                source, (pending.new_password, pending.old_password)
            )
        self.health = CollectorHealth(
            source=source.name,
            stale_after_seconds=settings.stale_after_seconds,
        )
        self.collector = Collector(
            source=source,
            store=store,
            hub=hub,
            health=self.health,
            retention_days=settings.retention_days,
        )

        self.mqtt: MqttPublisher | None = None
        if settings.mqtt_host:
            self.mqtt = MqttPublisher(
                MqttConfig(
                    host=settings.mqtt_host,
                    port=settings.mqtt_port,
                    user=settings.mqtt_user,
                    password=settings.mqtt_password,
                    prefix=settings.mqtt_prefix,
                    model=settings.model,
                ),
                hub=hub,
                health=self.health,
            )

        self.alerts: AlertService | None = None
        if settings.ntfy_url:
            # Probe targets are set from the dashboard, not from Settings.
            targets = store.load_probe_targets()
            self.alerts = AlertService(
                engine=AlertEngine(
                    AlertConfig(
                        probe1_target=targets.get(1),
                        probe2_target=targets.get(2),
                        drift_degrees=settings.alert_drift_degrees,
                        drift_seconds=settings.alert_drift_minutes * 60,
                        stale_seconds=settings.alert_stale_seconds,
                        cooldown_seconds=settings.alert_cooldown_seconds,
                    )
                ),
                hub=hub,
                health=self.health,
                sender=NtfySender(settings.ntfy_url, settings.ntfy_token),
                store=store,
            )
        self.tasks: list[asyncio.Task[None]] = []

    def same_grill(self, other: Runtime) -> bool:
        return all(
            getattr(self.settings, name) == getattr(other.settings, name)
            for name in GRILL_FIELDS
        )

    def take_over_from(self, previous: Runtime) -> None:
        """Carry alert state over from the stopped runtime this one replaces
        (same grill only; the caller checks)."""
        if self.alerts is not None and previous.alerts is not None:
            self.alerts.engine.inherit(previous.alerts.engine)

    def set_probe_targets(
        self, probe1: ProbeTarget | None, probe2: ProbeTarget | None
    ) -> None:
        if self.alerts is not None:
            self.alerts.engine.set_probe_targets(probe1, probe2)

    def hold_stale_alert(self, seconds: float) -> None:
        """Hold back the no-data alert for `seconds`; 0 releases it."""
        if self.alerts is not None:
            self.alerts.engine.hold_disconnect_alert(seconds)

    async def start(self) -> None:
        # Alerts first, so they subscribe before the collector's first reading.
        if self.alerts is not None:
            self.tasks.append(asyncio.create_task(self.alerts.run()))
        if self.mqtt is not None:
            self.tasks.append(asyncio.create_task(self.mqtt.run()))
        if self.settings.start_collector:
            self.tasks.append(asyncio.create_task(self.collector.run()))

    async def stop(self) -> None:
        self.collector.stop()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # A service that already died must not block the swap.
                logger.warning("Service ended with an error", exc_info=True)
        self.tasks = []
