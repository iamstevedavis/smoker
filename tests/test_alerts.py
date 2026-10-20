import asyncio
from datetime import UTC, datetime

from pitboss_bridge.services.alerts import (
    AlertConfig,
    AlertEngine,
    AlertService,
    Notification,
    NtfySender,
)
from pitboss_bridge.services.collector import CollectorHealth, StateHub
from pitboss_bridge.state import normalize_state
from pitboss_bridge.store import Database


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def snap(**values):
    return normalize_state(values, source="fake", observed_at=datetime(2026, 6, 2, tzinfo=UTC))


def keys(notifications):
    return [n.key for n in notifications]


def test_probe_target_fires_once_and_rearms_with_hysteresis():
    engine = AlertEngine(AlertConfig(probe1_target=200))

    assert engine.on_snapshot(snap(p1Temp=190)) == []
    assert keys(engine.on_snapshot(snap(p1Temp=201))) == ["probe1_target"]
    assert engine.on_snapshot(snap(p1Temp=205)) == []
    assert engine.on_snapshot(snap(p1Temp=197)) == []  # inside hysteresis: still disarmed
    assert engine.on_snapshot(snap(p1Temp=199)) == []
    assert engine.on_snapshot(snap(p1Temp=190)) == []  # re-armed below target - 5
    assert keys(engine.on_snapshot(snap(p1Temp=200))) == ["probe1_target"]


def test_probe_rearms_exactly_at_the_hysteresis_boundary():
    engine = AlertEngine(AlertConfig(probe1_target=200))

    assert keys(engine.on_snapshot(snap(p1Temp=200))) == ["probe1_target"]
    assert engine.on_snapshot(snap(p1Temp=195)) == []
    assert keys(engine.on_snapshot(snap(p1Temp=200))) == ["probe1_target"]


def test_probe_rule_needs_a_target_and_a_reading():
    engine = AlertEngine(AlertConfig(probe1_target=200))

    assert engine.on_snapshot(snap(p1Temp=None, p2Temp=300)) == []  # no p2 target
    assert AlertEngine(AlertConfig()).on_snapshot(snap(p1Temp=999)) == []


def test_changing_a_probe_target_in_place_rearms_only_that_probe():
    engine = AlertEngine(AlertConfig(probe1_target=200, probe2_target=150))

    assert keys(engine.on_snapshot(snap(p1Temp=210, p2Temp=160))) == [
        "probe1_target",
        "probe2_target",
    ]
    engine.set_probe_targets(205, 150)
    assert engine.config.probe1_target == 205
    # The new probe 1 target is a new alert; probe 2 stays fired.
    assert keys(engine.on_snapshot(snap(p1Temp=210, p2Temp=160))) == ["probe1_target"]
    engine.set_probe_targets(None, 150)
    assert engine.on_snapshot(snap(p1Temp=300, p2Temp=160)) == []


def test_drift_requires_sustained_deviation_while_on():
    clock = Clock()
    engine = AlertEngine(AlertConfig(drift_degrees=25, drift_seconds=600), clock)
    drifting = snap(moduleIsOn=True, grillTemp=300, grillSetTemp=225)

    assert engine.on_snapshot(drifting) == []
    clock.now = 599
    assert engine.on_snapshot(drifting) == []
    clock.now = 600
    assert keys(engine.on_snapshot(drifting)) == ["grill_drift"]
    clock.now = 1200
    assert engine.on_snapshot(drifting) == []  # one per incident

    clock.now = 1300
    engine.on_snapshot(snap(moduleIsOn=True, grillTemp=225, grillSetTemp=225))
    clock.now = 1400
    engine.on_snapshot(drifting)
    clock.now = 2000
    assert keys(engine.on_snapshot(drifting)) == ["grill_drift"]


def test_drift_ignored_when_grill_is_off_or_returns_to_normal_in_time():
    clock = Clock()
    engine = AlertEngine(AlertConfig(drift_seconds=60), clock)

    engine.on_snapshot(snap(moduleIsOn=False, grillTemp=300, grillSetTemp=225))
    clock.now = 600
    assert engine.on_snapshot(snap(moduleIsOn=False, grillTemp=300, grillSetTemp=225)) == []

    engine.on_snapshot(snap(moduleIsOn=True, grillTemp=300, grillSetTemp=225))
    clock.now = 630
    engine.on_snapshot(snap(moduleIsOn=True, grillTemp=226, grillSetTemp=225))
    clock.now = 700
    assert engine.on_snapshot(snap(moduleIsOn=True, grillTemp=300, grillSetTemp=225)) == []


def test_disconnect_and_recovery_notifications():
    engine = AlertEngine(AlertConfig(stale_seconds=60), Clock())

    assert engine.on_health({"last_update_age_seconds": None}) == []
    assert engine.on_health({"last_update_age_seconds": 30}) == []
    assert keys(engine.on_health({"last_update_age_seconds": 61})) == ["disconnected"]
    assert engine.on_health({"last_update_age_seconds": 120}) == []
    assert keys(engine.on_health({"last_update_age_seconds": 2})) == ["recovered"]
    assert engine.on_health({"last_update_age_seconds": 2}) == []
    assert keys(engine.on_health({"last_update_age_seconds": 90})) == ["disconnected"]


def test_cooldown_applies_to_recovery_alerts():
    clock = Clock()
    engine = AlertEngine(AlertConfig(stale_seconds=60, cooldown_seconds=300), clock)

    assert keys(engine.on_health({"last_update_age_seconds": 61})) == ["disconnected"]
    clock.now = 200
    assert keys(engine.on_health({"last_update_age_seconds": 1})) == ["recovered"]
    clock.now = 301
    assert keys(engine.on_health({"last_update_age_seconds": 61})) == ["disconnected"]
    clock.now = 302
    assert engine.on_health({"last_update_age_seconds": 1}) == []  # within cooldown
    clock.now = 700
    assert keys(engine.on_health({"last_update_age_seconds": 61})) == ["disconnected"]
    assert keys(engine.on_health({"last_update_age_seconds": 1})) == ["recovered"]


def test_disconnect_fires_when_no_reading_ever_arrives():
    clock = Clock()
    engine = AlertEngine(AlertConfig(stale_seconds=60), clock)

    assert engine.on_health({"last_update_age_seconds": None}) == []
    clock.now = 60
    assert engine.on_health({"last_update_age_seconds": None}) == []
    clock.now = 61
    assert keys(engine.on_health({"last_update_age_seconds": None})) == ["disconnected"]
    assert keys(engine.on_health({"last_update_age_seconds": 1})) == ["recovered"]


def test_drift_timer_resets_during_a_disconnect():
    clock = Clock()
    engine = AlertEngine(AlertConfig(drift_seconds=600, stale_seconds=60), clock)
    drifting = snap(moduleIsOn=True, grillTemp=300, grillSetTemp=225)

    engine.on_snapshot(drifting)
    clock.now = 100
    engine.on_health({"last_update_age_seconds": 100})
    clock.now = 1000
    engine.on_health({"last_update_age_seconds": 1})
    assert engine.on_snapshot(drifting) == []  # timer restarted on recovery
    clock.now = 1600
    assert keys(engine.on_snapshot(drifting)) == ["grill_drift"]


def test_outage_rearms_drift_that_already_fired():
    clock = Clock()
    engine = AlertEngine(AlertConfig(drift_seconds=600, stale_seconds=60), clock)
    drifting = snap(moduleIsOn=True, grillTemp=300, grillSetTemp=225)

    engine.on_snapshot(drifting)
    clock.now = 600
    assert keys(engine.on_snapshot(drifting)) == ["grill_drift"]
    engine.on_health({"last_update_age_seconds": 100})
    clock.now = 1000
    engine.on_health({"last_update_age_seconds": 1})
    engine.on_snapshot(drifting)
    clock.now = 1600
    assert keys(engine.on_snapshot(drifting)) == ["grill_drift"]


def test_turning_the_grill_off_rearms_drift():
    clock = Clock()
    engine = AlertEngine(AlertConfig(drift_seconds=60), clock)
    drifting = snap(moduleIsOn=True, grillTemp=300, grillSetTemp=225)

    engine.on_snapshot(drifting)
    clock.now = 60
    assert keys(engine.on_snapshot(drifting)) == ["grill_drift"]
    engine.on_snapshot(snap(moduleIsOn=False, grillTemp=300, grillSetTemp=225))

    clock.now = 100
    engine.on_snapshot(drifting)
    clock.now = 160
    assert keys(engine.on_snapshot(drifting)) == ["grill_drift"]


def test_error_flags_fire_on_rising_edge_and_no_pellets_is_urgent():
    engine = AlertEngine(AlertConfig())

    assert engine.on_snapshot(snap(noPellets=False, fanErr=False)) == []
    fired = engine.on_snapshot(snap(noPellets=True, fanErr=True))
    by_key = {n.key: n for n in fired}
    assert set(by_key) == {"error_noPellets", "error_fanErr"}
    assert by_key["error_noPellets"].priority == 5
    assert by_key["error_fanErr"].priority == 4
    assert engine.on_snapshot(snap(noPellets=True, fanErr=True)) == []
    engine.on_snapshot(snap(noPellets=False))
    assert keys(engine.on_snapshot(snap(noPellets=True))) == ["error_noPellets"]


def test_cooldown_suppresses_refiring_after_rearm():
    clock = Clock()
    engine = AlertEngine(AlertConfig(cooldown_seconds=300), clock)

    assert keys(engine.on_snapshot(snap(noPellets=True))) == ["error_noPellets"]
    engine.on_snapshot(snap(noPellets=False))
    clock.now = 100
    assert engine.on_snapshot(snap(noPellets=True)) == []
    engine.on_snapshot(snap(noPellets=False))
    clock.now = 400
    assert keys(engine.on_snapshot(snap(noPellets=True))) == ["error_noPellets"]


class FakeSender:
    def __init__(self, fail=False, hang=False):
        self.sent: list[Notification] = []
        self.fail = fail
        self.hang = hang

    async def send(self, notification):
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise RuntimeError("ntfy down")
        self.sent.append(notification)


def make_service(tmp_path, sender, **kwargs):
    store = Database(tmp_path / "p.sqlite")
    store.initialize()
    hub = StateHub(stale_after_seconds=30)
    health = CollectorHealth(source="fake", stale_after_seconds=30)
    service = AlertService(
        engine=AlertEngine(AlertConfig(probe1_target=150)),
        hub=hub,
        health=health,
        sender=sender,
        store=store,
        **{"check_interval_seconds": 0.01, **kwargs},
    )
    return service, hub, store


async def test_service_sends_alerts_from_hub_snapshots(tmp_path):
    sender = FakeSender()
    service, hub, store = make_service(tmp_path, sender)
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)

    await hub.publish(snap(p1Temp=160))
    await asyncio.sleep(0.05)
    task.cancel()

    assert [n.key for n in sender.sent] == ["probe1_target"]
    assert store.list_health_events()[0]["event_type"] == "alert_sent"


async def test_service_evaluates_a_snapshot_published_before_it_started(tmp_path):
    sender = FakeSender()
    service, hub, _ = make_service(tmp_path, sender)
    await hub.publish(snap(p1Temp=160))

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    task.cancel()

    assert keys(sender.sent) == ["probe1_target"]


async def test_service_starts_the_no_reading_clock_immediately(tmp_path):
    clock = Clock()
    service, _, _ = make_service(tmp_path, FakeSender(), check_interval_seconds=3600)
    service.engine = AlertEngine(AlertConfig(stale_seconds=60), clock)

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.01)
    task.cancel()
    clock.now = 61

    assert keys(service.engine.on_health({"last_update_age_seconds": None})) == [
        "disconnected"
    ]


async def test_sender_failure_is_logged_as_health_event_and_does_not_raise(tmp_path):
    service, hub, store = make_service(tmp_path, FakeSender(fail=True))
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)

    await hub.publish(snap(p1Temp=160))
    await asyncio.sleep(0.05)

    assert not task.done()
    task.cancel()
    event = store.list_health_events()[0]
    assert event["event_type"] == "alert_error"
    assert event["message"] == "ntfy down"


async def test_slow_sender_times_out_without_blocking(tmp_path):
    service, hub, store = make_service(
        tmp_path, FakeSender(hang=True), send_timeout_seconds=0.02
    )
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)

    await hub.publish(snap(p1Temp=160))
    await asyncio.sleep(0.15)
    task.cancel()

    assert store.list_health_events()[0]["event_type"] == "alert_error"


def test_ntfy_sender_builds_request(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    sender = NtfySender("https://ntfy.sh/topic", token="tk_x", timeout=3)

    sender._post(Notification("Hi", "body", priority=5, tags=("a", "b")))

    request = seen["request"]
    assert request.full_url == "https://ntfy.sh/topic"
    assert request.data == b"body"
    assert request.get_header("Title") == "Hi"
    assert request.get_header("Priority") == "5"
    assert request.get_header("Tags") == "a,b"
    assert request.get_header("Authorization") == "Bearer tk_x"
    assert seen["timeout"] == 3


def test_app_starts_with_alerts_configured(tmp_path, make_app):
    from fastapi.testclient import TestClient

    from pitboss_bridge.config import Settings

    settings = Settings(
        source="fake",
        db_path=tmp_path / "p.sqlite",
        poll_seconds=0,
        start_collector=False,
        ntfy_url="http://127.0.0.1:9/unreachable",
    )
    with TestClient(make_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
