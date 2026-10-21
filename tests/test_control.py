import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import finish_setup
from smokestack.api import create_app
from smokestack.config import Settings
from smokestack.services.alerts import AlertConfig, AlertEngine
from smokestack.services.control import ControlRejected, ControlRequest
from smokestack.sources.commands import (
    PB850PS2_SETPOINTS,
    Command,
    CommandNotSent,
    celsius_setpoints,
)
from smokestack.state import utc_now
from smokestack.store import CONTROL_EVENT, Database

PASSWORD = "brisket!"
HEADERS = {"X-Smokestack-Control": "1"}


def _settings(tmp_path, **kwargs) -> Settings:
    values = {
        "source": "fake",
        "db_path": tmp_path / "smokestack.sqlite",
        "poll_seconds": 0.01,
        "enable_control": True,
    }
    values.update(kwargs)
    return Settings(**values)


def _app(tmp_path, required=True, **kwargs):
    settings = _settings(tmp_path, **kwargs)
    finish_setup(settings, password=PASSWORD, required=required)
    app = create_app(settings)
    # Fast, but otherwise the real thing: the defaults are 30 s, 90 s and 5 s.
    app.state.control.confirm_seconds = 2
    app.state.control.reboot_confirm_seconds = 3
    app.state.control.min_gap_seconds = 0
    app.state.runtime.collector.retry_seconds = 0
    app.state.runtime.collector.source.reboot_seconds = 0
    return app


def _source(app):
    return app.state.runtime.collector.source


def _wait(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def _signed_in(client, app):
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 204
    _wait(lambda: app.state.health.connected and app.state.hub.current is not None)
    return client


def _audit(app):
    return [
        event
        for event in app.state.store.list_health_events(limit=1000)
        if event["event_type"] == CONTROL_EVENT
    ]


def _post(client, command, json=None, headers=HEADERS):
    return client.post(f"/api/control/{command}", json=json, headers=headers)


# Availability ----------------------------------------------------------------


def test_status_when_available(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        status = _signed_in(client, app).get("/api/control").json()

    assert status["enabled"] is True
    assert status["available"] is True
    assert status["reason"] is None
    assert status["commands"] == ["turn-off", "set-temperature", "reboot"]
    assert status["unit"] == "F"
    assert status["setpoints"] == list(PB850PS2_SETPOINTS)
    assert status["last_result"] is None


def test_disabled_control_answers_403_and_never_calls_the_source(tmp_path, monkeypatch):
    app = _app(tmp_path, enable_control=False)
    with TestClient(app) as client:
        _signed_in(client, app)

        async def execute(command):  # pragma: no cover - must not run
            raise AssertionError("execute called while control is off")

        monkeypatch.setattr(_source(app), "execute", execute)
        status = client.get("/api/control").json()
        responses = [
            _post(client, "turn-off"),
            _post(client, "set-temperature", {"temp": 250}),
            _post(client, "reboot"),
        ]

    assert (status["enabled"], status["available"]) == (False, False)
    assert "SMOKESTACK_ENABLE_CONTROL" in status["reason"]
    assert [r.status_code for r in responses] == [403, 403, 403]
    assert [e["data"]["outcome"] for e in _audit(app)] == ["rejected"] * 3


def test_control_is_unavailable_while_login_is_off(tmp_path):
    app = _app(tmp_path, required=False)
    with TestClient(app) as client:
        _wait(lambda: app.state.hub.current is not None)
        status = client.get("/api/control").json()
        response = _post(client, "turn-off")

    assert status["available"] is False
    assert "login" in status["reason"]
    assert response.status_code == 403
    assert _source(app).executed == []
    assert _audit(app)[0]["data"]["detail"] == status["reason"]


def test_control_needs_a_session_and_unauthenticated_requests_are_not_audited(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/control").status_code == 401
        assert _post(client, "turn-off").status_code == 401

    assert _audit(app) == []
    assert _source(app).executed == []


def test_a_session_ended_after_the_gate_is_still_refused(tmp_path, monkeypatch):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        monkeypatch.setattr(app.state.auth, "session_valid", lambda token: False)
        monkeypatch.setattr(app.state.auth, "allows", lambda token: True)
        assert _post(client, "turn-off").status_code == 401
    assert _audit(app)[0]["data"]["status_code"] == 401


def test_a_source_without_commands_is_unavailable(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)

        class ReadOnly:
            name = "readonly"

        app.state.runtime.collector.source = ReadOnly()
        status = client.get("/api/control").json()
        response = _post(client, "turn-off")

    assert status["available"] is False
    assert status["reason"] == "The readonly source can't send commands."
    assert response.status_code == 503


def test_disconnected_or_stale_data_is_rejected(tmp_path):
    app = _app(tmp_path, start_collector=False)
    with TestClient(app) as client:
        client.post("/api/login", json={"password": PASSWORD})
        assert _post(client, "turn-off").status_code == 503
        app.state.collector_sync_collect_next()
        app.state.hub.stale_after_seconds = 0
        time.sleep(0.01)
        assert _post(client, "turn-off").status_code == 503
    assert _source(app).executed == []


# Cross-site guard --------------------------------------------------------------


def test_control_posts_need_the_header_and_a_matching_origin(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        missing = _post(client, "turn-off", headers={})
        wrong_value = _post(client, "turn-off", headers={"X-Smokestack-Control": "yes"})
        foreign = _post(
            client, "turn-off", headers={**HEADERS, "Origin": "http://evil.example"}
        )
        other_port = _post(
            client, "turn-off", headers={**HEADERS, "Origin": "http://testserver:8080"}
        )
        same = _post(client, "turn-off", headers={**HEADERS, "Origin": "http://testserver"})

    assert [r.status_code for r in (missing, wrong_value, foreign, other_port)] == [403] * 4
    assert foreign.json()["detail"] == "cross-site request"
    assert same.status_code == 200
    assert _source(app).executed == [Command("turn-off")]


# Commands and their results ------------------------------------------------------


def test_turn_off_is_confirmed_and_audited(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        response = _post(client, "turn-off")
        status = client.get("/api/control").json()

    body = response.json()
    assert response.status_code == 200
    assert (body["command"], body["args"]) == ("turn-off", {})
    assert body["outcome"] == "confirmed"
    assert body["sent"] is True
    assert body["attempts"] == 1
    assert app.state.hub.current.values_dict()["moduleIsOn"] is False
    assert status["last_result"] == body
    [event] = _audit(app)
    assert event["id"] == body["audit_id"]
    assert event["message"] == "turn-off: confirmed"
    assert event["data"]["client"] == "testclient"
    assert event["data"]["command"] == "turn-off"


def test_set_temperature_is_snapped_and_confirmed(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client, "set-temperature", {"temp": 333}).json()

    assert body["outcome"] == "confirmed"
    assert (body["requested"], body["target"], body["unit"]) == (333, 325, "F")
    assert _source(app).executed == [Command("set-temperature", 325)]
    assert _audit(app)[0]["data"]["args"] == {"temp": 333}


def test_set_temperature_snaps_in_celsius(tmp_path):
    app = _app(tmp_path)
    source = _source(app)
    source.fahrenheit = False
    source.set_temp = 107
    with TestClient(app) as client:
        _signed_in(client, app)
        _wait(lambda: app.state.hub.current.values_dict()["isFahrenheit"] is False)
        status = client.get("/api/control").json()
        body = _post(client, "set-temperature", {"temp": 120}).json()

    assert status["setpoints"] == celsius_setpoints(PB850PS2_SETPOINTS)
    assert (body["target"], body["unit"], body["outcome"]) == (121, "C", "confirmed")


def test_set_temperature_needs_a_known_unit(tmp_path):
    app = _app(tmp_path)
    source = _source(app)
    reading = source._reading
    source._reading = lambda: {k: v for k, v in reading().items() if k != "isFahrenheit"}
    with TestClient(app) as client:
        _signed_in(client, app)
        _wait(lambda: "isFahrenheit" not in app.state.hub.current.values_dict())
        response = _post(client, "set-temperature", {"temp": 250})

    assert response.status_code == 409
    assert source.executed == []


@pytest.mark.parametrize("body", [{}, {"temp": 0}, {"temp": 2000}, {"temp": 250, "x": 1}])
def test_bad_set_temperature_bodies_are_rejected(tmp_path, body):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        assert _post(client, "set-temperature", body).status_code == 422
    assert _source(app).executed == []


def test_already_applied_sends_nothing(tmp_path):
    app = _app(tmp_path)
    _source(app).set_temp = 230
    with TestClient(app) as client:
        _signed_in(client, app)
        same_temp = _post(client, "set-temperature", {"temp": 228}).json()
        _post(client, "turn-off")
        off_again = _post(client, "turn-off").json()

    assert same_temp["outcome"] == "already_applied"
    assert same_temp["target"] == 230
    assert same_temp["detail"] == "The grill was already set to 230°F."
    assert same_temp["sent"] is False
    assert off_again["outcome"] == "already_applied"
    assert off_again["detail"] == "The grill was already off."
    assert _source(app).executed == [Command("turn-off")]


def test_a_missing_field_never_counts_as_already_applied(tmp_path):
    app = _app(tmp_path)
    source = _source(app)
    source.module_on = False
    reading = source._reading
    source._reading = lambda: {
        k: v for k, v in reading().items() if k not in ("moduleIsOn", "grillSetTemp")
    }
    with TestClient(app) as client:
        _signed_in(client, app)
        _wait(lambda: "moduleIsOn" not in app.state.hub.current.values_dict())
        app.state.control.confirm_seconds = 0.2
        off = _post(client, "turn-off").json()
        temp = _post(client, "set-temperature", {"temp": 225}).json()

    # Sent, and with no field to watch, never confirmed either.
    assert (off["outcome"], off["sent"]) == ("unconfirmed", True)
    assert (temp["outcome"], temp["sent"]) == ("unconfirmed", True)
    assert source.executed == [Command("turn-off"), Command("set-temperature", 220)]


def test_an_ignored_command_is_unconfirmed(tmp_path):
    app = _app(tmp_path)
    _source(app).faults.add("ignore")
    with TestClient(app) as client:
        _signed_in(client, app)
        app.state.control.confirm_seconds = 0.3
        body = _post(client, "set-temperature", {"temp": 250}).json()

    assert body["outcome"] == "unconfirmed"
    assert "Check the grill" in body["detail"]


def test_an_rpc_error_fails(tmp_path):
    app = _app(tmp_path)
    _source(app).faults.add("rpc_error")
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client, "turn-off").json()

    assert body["outcome"] == "failed"
    assert "RPC error" in body["detail"]
    assert body["attempts"] == 1


def test_unauthorized_is_retried_once_for_idempotent_commands(tmp_path):
    app = _app(tmp_path)
    source = _source(app)
    source.faults.add("unauthorized_once")
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client, "turn-off").json()

    assert (body["outcome"], body["attempts"]) == ("confirmed", 2)
    assert source.executed == [Command("turn-off")]


def test_unauthorized_reboot_is_not_retried(tmp_path):
    app = _app(tmp_path)
    source = _source(app)
    source.faults.add("unauthorized_once")
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client, "reboot").json()

    assert (body["outcome"], body["attempts"]) == ("failed", 1)
    assert source.executed == []


def test_the_blank_frame_after_a_command_is_ignored(tmp_path):
    app = _app(tmp_path)
    _source(app).faults.add("blank_frame")
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client, "turn-off").json()
        history = client.get("/api/history?limit=10000").json()["items"]

    assert body["outcome"] == "confirmed"
    assert all(item["fields"] for item in history)


def test_reboot_is_confirmed_when_the_source_comes_back(tmp_path, monkeypatch):
    app = _app(tmp_path)
    holds = []
    with TestClient(app) as client:
        _signed_in(client, app)
        monkeypatch.setattr(app.state.runtime, "hold_stale_alert", holds.append)
        before = app.state.health.reconnect_count
        body = _post(client, "reboot").json()
        after = app.state.health.reconnect_count

    assert body["outcome"] == "confirmed"
    assert after == before + 1
    # Held for the reboot window, released once readings are back.
    assert holds == [3, 0]


def test_a_reboot_that_never_comes_back_is_unconfirmed(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _source(app).faults.add("reboot_never_returns")
    holds = []
    with TestClient(app) as client:
        _signed_in(client, app)
        app.state.control.reboot_confirm_seconds = 0.5
        monkeypatch.setattr(app.state.runtime, "hold_stale_alert", holds.append)
        body = _post(client, "reboot").json()

    assert body["outcome"] == "unconfirmed"
    # The hold runs out on its own, so the stale alert follows.
    assert holds == [0.5]


# Rate limiting and the lock --------------------------------------------------------


def test_commands_need_a_gap_between_them(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        app.state.control.min_gap_seconds = 5
        first = _post(client, "set-temperature", {"temp": 250})
        second = _post(client, "turn-off")

    assert first.json()["outcome"] == "confirmed"
    assert second.status_code == 429
    assert 1 <= int(second.headers["retry-after"]) <= 5
    assert [e["data"]["outcome"] for e in _audit(app)] == ["rejected", "confirmed"]


def test_already_applied_does_not_start_the_gap(tmp_path):
    app = _app(tmp_path)
    _source(app).set_temp = 230
    with TestClient(app) as client:
        _signed_in(client, app)
        app.state.control.min_gap_seconds = 5
        assert _post(client, "set-temperature", {"temp": 230}).json()["outcome"] == (
            "already_applied"
        )
        assert _post(client, "turn-off").status_code == 200


async def test_one_command_at_a_time(tmp_path):
    settings = _settings(tmp_path, start_collector=False)
    finish_setup(settings, password=PASSWORD)
    app = create_app(settings)
    control = app.state.control
    request = ControlRequest("127.0.0.1", app.state.auth.issue(), True, None, None)

    async with control._lock:
        with pytest.raises(ControlRejected) as rejected:
            await control.run("turn-off", request)

    assert rejected.value.status_code == 409
    assert rejected.value.audit_id
    assert _audit(app)[0]["data"]["status_code"] == 409


def test_last_result_survives_a_restart(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        result = _post(client, "turn-off").json()
        _post(client, "turn-off", headers={})  # rejected: not a result

    restarted = _app(tmp_path)
    assert restarted.state.control.last_result["audit_id"] == result["audit_id"]
    assert restarted.state.control.last_result["outcome"] == "confirmed"


# Audit log retention and the held-back stale alert ------------------------------------


def test_retention_pruning_keeps_the_control_audit_log(tmp_path):
    store = Database(tmp_path / "smokestack.sqlite")
    store.initialize()
    old = utc_now() - timedelta(days=30)
    store.insert_health_event(CONTROL_EVENT, "turn-off: confirmed", {}, at=old)
    store.insert_health_event("source_error", "gone", {}, at=old)

    deleted = store.prune(utc_now() - timedelta(days=7))

    assert deleted["health_events"] == 1
    assert [e["event_type"] for e in store.list_health_events()] == [CONTROL_EVENT]


def test_held_back_stale_alert_fires_only_after_the_hold():
    now = [0.0]
    engine = AlertEngine(AlertConfig(stale_seconds=60), clock=lambda: now[0])
    stale = {"source": "http", "last_update_age_seconds": 120}

    engine.hold_disconnect_alert(90)
    assert engine.on_health(stale) == []
    now[0] = 89
    assert engine.on_health(stale) == []
    now[0] = 90
    assert [n.key for n in engine.on_health(stale)] == ["disconnected"]


def test_releasing_the_hold_restores_the_stale_alert():
    now = [0.0]
    engine = AlertEngine(AlertConfig(stale_seconds=60), clock=lambda: now[0])
    engine.hold_disconnect_alert(90)
    engine.hold_disconnect_alert(0)
    stale = {"source": "http", "last_update_age_seconds": 120}
    assert [n.key for n in engine.on_health(stale)] == ["disconnected"]


def test_the_hold_survives_a_settings_swap():
    now = [0.0]
    old = AlertEngine(AlertConfig(), clock=lambda: now[0])
    old.hold_disconnect_alert(90)
    new = AlertEngine(AlertConfig(), clock=lambda: now[0])
    new.inherit(old)
    assert new.on_health({"last_update_age_seconds": 120}) == []


def test_set_temperature_is_refused_when_the_unit_changed_since_confirming(tmp_path):
    app = _app(tmp_path)
    source = _source(app)
    with TestClient(app) as client:
        _signed_in(client, app)
        # Switched to Celsius on the grill's panel after the user confirmed 250 F.
        source.fahrenheit = False
        _wait(lambda: app.state.hub.current.values_dict()["isFahrenheit"] is False)
        stale = _post(client, "set-temperature", {"temp": 250, "unit": "F"})
        matching = _post(client, "set-temperature", {"temp": 120, "unit": "C"})

    assert stale.status_code == 409
    assert "°C" in stale.json()["detail"]
    assert matching.json()["outcome"] == "confirmed"
    assert source.executed == [Command("set-temperature", 121)]
    rejected = _audit(app)[-1]
    assert rejected["data"]["args"] == {"temp": 250, "unit": "F"}


def test_a_command_that_was_never_sent_does_not_start_the_gap(tmp_path, monkeypatch):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        app.state.control.min_gap_seconds = 5
        source = _source(app)
        execute = source.execute

        async def not_sent(command):
            raise CommandNotSent("the grill connection closed first")

        monkeypatch.setattr(source, "execute", not_sent)
        first = _post(client, "turn-off").json()
        monkeypatch.setattr(source, "execute", execute)
        retry = _post(client, "turn-off")

    assert (first["outcome"], first["sent"]) == ("failed", False)
    assert retry.status_code == 200
    assert retry.json()["outcome"] == "confirmed"
