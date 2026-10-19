import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from pitboss_bridge.api import create_app
from pitboss_bridge.config import Settings
from pitboss_bridge.services.collector import CollectorHealth, StateHub
from pitboss_bridge.services.mqtt import (
    MqttConfig,
    MqttPublisher,
    discovery_messages,
    state_payload,
)
from pitboss_bridge.state import normalize_state

CONFIG = MqttConfig(host="broker", prefix="pitboss")


def snap(**values):
    return normalize_state(values, source="fake", observed_at=datetime(2026, 6, 2, tzinfo=UTC))


class FakeClient:
    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []

    async def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))

    def on(self, topic):
        return [json.loads(p) if p.startswith("{") else p for t, p, _ in self.published if t == topic]


def test_discovery_payloads_cover_sensors_and_unit():
    messages = dict(discovery_messages(CONFIG, fahrenheit=False))

    probe = messages["homeassistant/sensor/pitboss_probe_1/config"]
    assert probe["unit_of_measurement"] == "°C"
    assert probe["device_class"] == "temperature"
    assert probe["state_topic"] == "pitboss/state"
    assert probe["availability_topic"] == "pitboss/availability"
    assert "p1Temp" in probe["value_template"]
    assert {"pitboss_grill_temp", "pitboss_set_temp", "pitboss_probe_2"} <= {
        m["unique_id"] for t, m in messages.items() if "/sensor/" in t
    }
    binary = {m["unique_id"] for t, m in messages.items() if "/binary_sensor/" in t}
    assert binary == {
        "pitboss_power",
        "pitboss_fan",
        "pitboss_igniter",
        "pitboss_auger",
        "pitboss_problem",
    }
    assert dict(discovery_messages(CONFIG))[
        "homeassistant/sensor/pitboss_grill_temp/config"
    ]["unit_of_measurement"] == "°F"


def test_state_payload_skips_unreliable_fields_and_aggregates_problems():
    payload = state_payload(
        snap(
            grillTemp=225,
            p1Temp=150,
            p3Temp=None,
            p1Target=50,
            smokerActTemp=225,
            fanErr=True,
        )
    )

    assert payload["grillTemp"] == 225
    assert payload["p2Temp"] is None
    assert payload["problem"] is True
    assert not {"p3Temp", "p1Target", "smokerActTemp"} & set(payload)
    assert state_payload(snap(noPellets=False, fanErr=False))["problem"] is False


def make_publisher(factory, **kwargs):
    hub = StateHub(stale_after_seconds=30)
    health = CollectorHealth(source="fake", stale_after_seconds=30)
    publisher = MqttPublisher(
        CONFIG,
        hub,
        health,
        client_factory=factory,
        check_interval_seconds=0.01,
        retry_seconds=0.01,
        max_retry_seconds=0.02,
        **kwargs,
    )
    return publisher, hub, health


async def test_publishes_discovery_state_and_availability():
    client = FakeClient()

    @asynccontextmanager
    async def factory(config):
        yield client

    publisher, hub, health = make_publisher(factory)
    health.mark_update()
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.05)

    await hub.publish(snap(grillTemp=225, moduleIsOn=True))
    await asyncio.sleep(0.05)
    task.cancel()

    assert any(t.startswith("homeassistant/sensor/") for t, _, _ in client.published)
    assert all(retain for t, _, retain in client.published if t.startswith("homeassistant/"))
    assert client.on("pitboss/state")[-1]["grillTemp"] == 225
    assert client.on("pitboss/availability") == ["online"]


async def test_availability_goes_offline_when_source_is_stale():
    client = FakeClient()

    @asynccontextmanager
    async def factory(config):
        yield client

    publisher, hub, health = make_publisher(factory)
    health.mark_update()
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.05)
    health.mark_error("lost")
    await asyncio.sleep(0.05)
    task.cancel()

    assert client.on("pitboss/availability") == ["online", "offline"]


async def test_publishes_offline_on_graceful_shutdown():
    client = FakeClient()

    @asynccontextmanager
    async def factory(config):
        yield client

    publisher, hub, health = make_publisher(factory)
    health.mark_update()
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.on("pitboss/availability") == ["online", "offline"]
    assert client.published[-1][2] is True


async def test_discovery_is_republished_when_unit_changes():
    client = FakeClient()

    @asynccontextmanager
    async def factory(config):
        yield client

    publisher, hub, health = make_publisher(factory)
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.05)
    await hub.publish(snap(isFahrenheit=False, grillTemp=100))
    await asyncio.sleep(0.05)
    task.cancel()

    units = [u["unit_of_measurement"] for u in client.on("homeassistant/sensor/pitboss_grill_temp/config")]
    assert units == ["°F", "°C"]


async def test_reconnects_with_backoff_after_broker_failure():
    attempts = []
    client = FakeClient()

    @asynccontextmanager
    async def factory(config):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("broker down")
        yield client

    publisher, hub, health = make_publisher(factory)
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.2)
    task.cancel()

    assert len(attempts) == 3
    assert client.published


async def test_dead_broker_never_backs_up_the_hub():
    @asynccontextmanager
    async def factory(config):
        raise ConnectionError("broker down")
        yield

    publisher, hub, health = make_publisher(factory)
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.03)

    for _ in range(50):  # far more than the hub's per-subscriber queue size
        await hub.publish(snap(grillTemp=225))
    task.cancel()


def test_broker_outage_does_not_affect_api(tmp_path):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "p.sqlite",
        poll_seconds=0,
        start_collector=False,
        mqtt_host="127.0.0.1",
        mqtt_port=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/state").json()["available"] is True
        assert client.get("/api/history").json()["count"] == 1
