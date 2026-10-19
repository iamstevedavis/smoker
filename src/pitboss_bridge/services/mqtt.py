"""MQTT publisher with Home Assistant discovery.

Read-only: state and availability go out, nothing is subscribed to. The broker
client is injected (`ClientFactory`) and the real one lazy-imports `aiomqtt`,
so the base install does not need it. A broker outage only affects this task;
the collector and `/api/*` never see it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from pitboss_bridge.services.collector import CollectorHealth, StateHub
from pitboss_bridge.state import ERROR_FLAGS_FOR_PROBLEM, StateSnapshot

logger = logging.getLogger(__name__)

# Published fields. The unreliable ones (smokerActTemp, p3/p4Temp, p1Target) are
# deliberately left out; see docs/fields.md once available.
STATE_FIELDS = (
    "grillTemp",
    "grillSetTemp",
    "p1Temp",
    "p2Temp",
    "isFahrenheit",
    "moduleIsOn",
    "fanState",
    "hotState",
    "motorState",
    "noPellets",
)

SENSORS = (
    ("grill_temp", "Grill temperature", "grillTemp"),
    ("set_temp", "Grill set temperature", "grillSetTemp"),
    ("probe_1", "Probe 1", "p1Temp"),
    ("probe_2", "Probe 2", "p2Temp"),
)
BINARY_SENSORS = (
    ("power", "Power", "moduleIsOn", "power"),
    ("fan", "Fan", "fanState", "running"),
    ("igniter", "Igniter", "hotState", "heat"),
    ("auger", "Auger", "motorState", "running"),
    ("problem", "Problem", "problem", "problem"),
)


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    user: str = ""
    password: str = ""
    prefix: str = "pitboss"
    model: str = "PB850PS2"


class MqttClient(Protocol):
    async def publish(self, topic: str, payload: str, retain: bool = False) -> None: ...


ClientFactory = Callable[[MqttConfig], AbstractAsyncContextManager[MqttClient]]


def state_payload(snapshot: StateSnapshot) -> dict[str, Any]:
    values = snapshot.values_dict()
    payload = {name: values.get(name) for name in STATE_FIELDS}
    payload["problem"] = any(values.get(flag) is True for flag in ERROR_FLAGS_FOR_PROBLEM)
    return payload


def discovery_messages(
    config: MqttConfig, fahrenheit: bool = True
) -> list[tuple[str, dict[str, Any]]]:
    """(topic, payload) pairs for Home Assistant MQTT discovery."""
    unit = "°F" if fahrenheit else "°C"
    device = {
        "identifiers": [f"{config.prefix}_grill"],
        "name": "Pit Boss Grill",
        "manufacturer": "Pit Boss",
        "model": config.model,
    }
    common = {
        "state_topic": f"{config.prefix}/state",
        "availability_topic": f"{config.prefix}/availability",
        "device": device,
    }
    messages: list[tuple[str, dict[str, Any]]] = []
    for key, name, field in SENSORS:
        unique_id = f"{config.prefix}_{key}"
        messages.append(
            (
                f"homeassistant/sensor/{unique_id}/config",
                {
                    **common,
                    "name": name,
                    "unique_id": unique_id,
                    "device_class": "temperature",
                    "unit_of_measurement": unit,
                    "state_class": "measurement",
                    "value_template": (
                        f"{{% if value_json.{field} is number %}}"
                        f"{{{{ value_json.{field} }}}}{{% else %}}unknown{{% endif %}}"
                    ),
                },
            )
        )
    for key, name, field, device_class in BINARY_SENSORS:
        unique_id = f"{config.prefix}_{key}"
        messages.append(
            (
                f"homeassistant/binary_sensor/{unique_id}/config",
                {
                    **common,
                    "name": name,
                    "unique_id": unique_id,
                    "device_class": device_class,
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "value_template": (
                        f"{{{{ 'ON' if value_json.{field} else 'OFF' }}}}"
                    ),
                },
            )
        )
    return messages


def aiomqtt_factory(config: MqttConfig) -> AbstractAsyncContextManager[MqttClient]:
    try:
        import aiomqtt
    except ImportError as exc:
        raise RuntimeError(
            "MQTT publishing requires installing the bridge with the 'mqtt' extra"
        ) from exc
    return aiomqtt.Client(
        hostname=config.host,
        port=config.port,
        username=config.user or None,
        password=config.password or None,
        will=aiomqtt.Will(
            topic=f"{config.prefix}/availability", payload="offline", retain=True
        ),
    )


class MqttPublisher:
    def __init__(
        self,
        config: MqttConfig,
        hub: StateHub,
        health: CollectorHealth,
        client_factory: ClientFactory = aiomqtt_factory,
        check_interval_seconds: float = 5.0,
        retry_seconds: float = 5.0,
        max_retry_seconds: float = 60.0,
    ) -> None:
        self.config = config
        self.hub = hub
        self.health = health
        self.client_factory = client_factory
        self.check_interval_seconds = check_interval_seconds
        self.retry_seconds = retry_seconds
        self.max_retry_seconds = max_retry_seconds

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                await self._session()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(self.retry_seconds * 2 ** (failures - 1), self.max_retry_seconds)
                logger.warning(
                    "MQTT unavailable (%s); retrying in %.0fs", exc or type(exc).__name__, delay
                )
                await asyncio.sleep(delay)

    async def _session(self) -> None:
        availability = f"{self.config.prefix}/availability"
        async with self.client_factory(self.config) as client:
            logger.info("Connected to MQTT broker %s", self.config.host)
            fahrenheit = True
            current = self.hub.current
            if current is not None:
                fahrenheit = current.values_dict().get("isFahrenheit") is not False
            await self._publish_discovery(client, fahrenheit)
            # Subscribe only while connected so a dead broker cannot back up the
            # hub's bounded queue.
            queue = self.hub.subscribe()
            try:
                online: str | None = None
                if current is not None:
                    await self._publish_state(client, current)
                while True:
                    try:
                        snapshot = await asyncio.wait_for(
                            queue.get(), timeout=self.check_interval_seconds
                        )
                    except asyncio.TimeoutError:
                        snapshot = None
                    if snapshot is not None:
                        unit = snapshot.values_dict().get("isFahrenheit") is not False
                        if unit != fahrenheit:
                            fahrenheit = unit
                            await self._publish_discovery(client, fahrenheit)
                        await self._publish_state(client, snapshot)
                    status = self._availability()
                    if status != online:
                        online = status
                        await client.publish(availability, status, retain=True)
            finally:
                self.hub.unsubscribe(queue)
                await self._publish_offline(client)

    async def _publish_offline(self, client: MqttClient) -> None:
        """Brokers skip the last will on a graceful disconnect, so say it ourselves."""
        try:
            await asyncio.wait_for(
                client.publish(
                    f"{self.config.prefix}/availability", "offline", retain=True
                ),
                timeout=self.check_interval_seconds,
            )
        except Exception:
            logger.debug("Could not publish MQTT offline status", exc_info=True)

    def _availability(self) -> str:
        info = self.health.to_api_dict()
        return "online" if info["connected"] and not info["stale"] else "offline"

    async def _publish_discovery(self, client: MqttClient, fahrenheit: bool) -> None:
        for topic, payload in discovery_messages(self.config, fahrenheit):
            await client.publish(topic, json.dumps(payload), retain=True)

    async def _publish_state(self, client: MqttClient, snapshot: StateSnapshot) -> None:
        await client.publish(
            f"{self.config.prefix}/state",
            json.dumps(state_payload(snapshot)),
            retain=True,
        )
