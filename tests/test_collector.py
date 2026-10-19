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


class ScriptedSource:
    """Source whose sessions follow a script; records when sessions close.

    Each session is a list of steps: a dict is yielded as a reading, an
    Exception is raised. A session that runs out of steps waits forever.
    """

    name = "scripted"

    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.opened = 0
        self.closed = 0
        self.log = []

    async def readings(self):
        steps = self.sessions.pop(0) if self.sessions else []
        self.opened += 1
        self.log.append("open")
        try:
            for step in steps:
                if isinstance(step, Exception):
                    raise step
                yield step
            await asyncio.Event().wait()
        finally:
            self.closed += 1
            self.log.append("close")


READING = {"grillTemp": 225, "grillSetTemp": 225}


def make_collector(tmp_path, source, store=None):
    db = store or Database(tmp_path / "pitboss.sqlite")
    db.initialize()
    hub = StateHub(stale_after_seconds=30)
    health = CollectorHealth(source=source.name, stale_after_seconds=30)
    collector = Collector(
        source=source, store=db, hub=hub, health=health, retry_seconds=0
    )
    return collector, health, db


async def run_until(collector, condition, timeout=1.0):
    task = asyncio.create_task(collector.run())
    try:
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0)
    finally:
        collector.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_health_is_not_connected_before_first_reading():
    health = CollectorHealth(source="ble", stale_after_seconds=30)
    health.mark_started()

    assert health.to_api_dict()["connected"] is False


async def test_failed_attempts_do_not_count_as_reconnects(tmp_path):
    source = ScriptedSource(
        [
            [READING, RuntimeError("dropped")],
            [RuntimeError("not found")],
            [RuntimeError("not found")],
            [READING],
        ]
    )
    collector, health, db = make_collector(tmp_path, source)

    await run_until(collector, lambda: source.opened == 4 and health.connected)

    assert health.reconnect_count == 1
    assert health.consecutive_failures == 0
    assert health.last_error == "not found"
    events = [e["event_type"] for e in db.list_health_events()]
    assert events.count("connected") == 2
    assert events.count("source_error") == 3


async def test_consecutive_failures_reported_while_disconnected(tmp_path):
    source = ScriptedSource([[RuntimeError("not found")]] * 3 + [[]])
    collector, health, _ = make_collector(tmp_path, source)

    await run_until(collector, lambda: source.opened == 4)

    payload = health.to_api_dict()
    assert payload["connected"] is False
    assert payload["consecutive_failures"] == 3
    assert payload["reconnect_count"] == 0
    assert payload["last_error_at"] is not None


def test_retry_delay_backs_off_exponentially_and_caps(tmp_path):
    collector, health, _ = make_collector(tmp_path, ScriptedSource([]))
    collector.retry_seconds = 5
    collector.max_retry_seconds = 60

    delays = []
    for _ in range(6):
        health.mark_error("boom")
        delays.append(collector.retry_delay())

    assert delays == [5, 10, 20, 40, 60, 60]


async def test_open_session_is_closed_when_processing_fails(tmp_path):
    class FailingOnceStore(Database):
        failed = False

        def insert_reading(self, snapshot):
            if not self.failed:
                self.failed = True
                raise RuntimeError("disk full")
            super().insert_reading(snapshot)

    source = ScriptedSource([[READING, READING], [READING]])
    store = FailingOnceStore(tmp_path / "pitboss.sqlite")
    collector, health, _ = make_collector(tmp_path, source, store=store)

    await run_until(collector, lambda: source.opened == 2 and health.connected)

    # The first session was still suspended at a yield (holding the grill
    # connection) when the store failed. It must be closed before the next
    # session opens, not left for garbage collection to race the reconnect.
    assert source.log[:3] == ["open", "close", "open"]


async def test_stopping_collector_closes_source_session(tmp_path):
    source = ScriptedSource([[READING]])
    collector, health, _ = make_collector(tmp_path, source)

    await run_until(collector, lambda: health.connected)

    assert source.closed == 1


def test_prune_once_respects_retention_days(tmp_path):
    from datetime import timedelta

    from pitboss_bridge.services.collector import Collector, CollectorHealth, StateHub
    from pitboss_bridge.sources.fake import FakeGrillSource
    from pitboss_bridge.state import utc_now
    from pitboss_bridge.store import Database

    db = Database(tmp_path / "p.sqlite")
    db.initialize()
    db.insert_health_event("old", "x", at=utc_now() - timedelta(days=10))
    db.insert_health_event("new", "y")

    def make(days):
        return Collector(
            source=FakeGrillSource(interval_seconds=0),
            store=db,
            hub=StateHub(stale_after_seconds=30),
            health=CollectorHealth(source="fake", stale_after_seconds=30),
            retention_days=days,
        )

    assert make(0).prune_once() == {"readings": 0, "health_events": 0}
    assert make(7).prune_once()["health_events"] == 1
    assert [e["event_type"] for e in db.list_health_events()] == ["new"]
