import asyncio

from pitboss_bridge.services.collector import Collector, CollectorHealth, StateHub
from pitboss_bridge.sources.fake import FakeGrillSource
from pitboss_bridge.store import Database


async def test_fake_source_emits_realistic_state():
    source = FakeGrillSource(interval_seconds=0)
    stream = source.readings()

    first = await anext(stream)
    second = await anext(stream)

    assert first["grillSetTemp"] == 225
    assert first["isFahrenheit"] is True
    assert "p1Temp" in first
    assert second["grillTemp"] != first["grillTemp"]


async def test_collector_stores_snapshot_and_updates_health(tmp_path):
    source = FakeGrillSource(interval_seconds=0)
    db = Database(tmp_path / "pitboss.sqlite")
    db.initialize()
    hub = StateHub(stale_after_seconds=30)
    health = CollectorHealth(source="fake", stale_after_seconds=30)
    collector = Collector(source=source, store=db, hub=hub, health=health)

    await collector.collect_next()

    assert hub.current is not None
    assert hub.current.fields["grillTemp"].value is not None
    assert health.connected is True
    assert health.last_update_at is not None
    assert db.list_history()[0]["fields"]["grillSetTemp"] == 225


async def test_state_hub_publishes_updates_to_subscribers():
    hub = StateHub(stale_after_seconds=30)
    queue = hub.subscribe()
    source = FakeGrillSource(interval_seconds=0)
    raw = await anext(source.readings())

    await hub.publish_raw(raw, source="fake")
    snapshot = await asyncio.wait_for(queue.get(), timeout=1)

    assert snapshot.fields["grillSetTemp"].value == 225
    hub.unsubscribe(queue)
