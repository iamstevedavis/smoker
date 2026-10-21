"""Changing the grill's own password (issue #54).

The bridge keeps using the grill with the password, so changing it is the
one command that can lock the bridge out. What settles it is which password
the grill accepts afterwards, never what the command returned.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from conftest import finish_setup
from smokestack import auth
from smokestack.api import create_app
from smokestack.config import Settings
from smokestack.services.passwords import PASSWORD_EVENT
from smokestack.sources.commands import Command
from smokestack.store import CONTROL_EVENT, Database

PASSWORD = "brisket!"
HEADERS = {"X-Smokestack-Control": "1"}
OLD = "old-grill-password"
NEW = "new-grill-password"


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_FAILURE_DELAY_SECONDS", 0)


def _settings(tmp_path, **kwargs) -> Settings:
    values = {
        "source": "fake",
        "db_path": tmp_path / "smokestack.sqlite",
        "poll_seconds": 0.01,
        "enable_control": True,
        "password": OLD,
    }
    values.update(kwargs)
    return Settings(**values)


def _app(tmp_path, **kwargs):
    settings = _settings(tmp_path, **kwargs)
    finish_setup(settings, password=PASSWORD, required=True)
    app = create_app(settings)
    app.state.control.min_gap_seconds = 0
    app.state.runtime.collector.retry_seconds = 0
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


def _body(password=NEW, dashboard_password=PASSWORD, confirm=None):
    return {
        "password": password,
        "confirm": password if confirm is None else confirm,
        "dashboard_password": dashboard_password,
    }


def _post(client, body=None):
    return client.post(
        "/api/control/set-password",
        json=_body() if body is None else body,
        headers=HEADERS,
    )


def _audit(app):
    return [
        event
        for event in app.state.store.list_health_events(limit=1000)
        if event["event_type"] == CONTROL_EVENT
    ]


# The happy path ------------------------------------------------------------


def test_the_password_change_is_confirmed_and_the_bridge_follows(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        response = _post(client)
        status = client.get("/api/control").json()

    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "confirmed"
    assert body["command"] == "set-password"
    # The grill has the new password, and so has the bridge.
    assert _source(app).password == NEW
    assert app.state.settings.password == NEW
    assert app.state.store.load_settings() == {"password": NEW}
    # Nothing is pending any more.
    assert app.state.store.load_pending_password() is None
    assert status["pending_password_change"] is None


def test_the_audit_row_records_the_change_without_the_password(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client).json()

    [event] = _audit(app)
    assert event["message"] == "set-password: confirmed"
    assert event["data"]["args"] == {}
    assert event["data"]["outcome"] == "confirmed"
    assert OLD not in event["data"].__repr__()
    assert NEW not in event["data"].__repr__()
    # Nor in the answer the dashboard gets.
    assert NEW not in body["detail"]


def test_the_change_is_a_command_the_source_really_sent(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        _post(client)

    assert _source(app).executed == [Command("set-password", password=NEW)]


# Refusals ------------------------------------------------------------------


def test_a_wrong_dashboard_password_is_refused(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        response = _post(client, _body(dashboard_password="not it"))

    assert response.status_code == 403
    assert response.json()["detail"] == "wrong dashboard password"
    assert _source(app).executed == []
    assert app.state.store.load_pending_password() is None
    assert _audit(app)[0]["data"]["outcome"] == "rejected"


def test_the_two_passwords_have_to_match(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        response = _post(client, _body(confirm="something else"))

    assert response.status_code == 422
    assert _source(app).executed == []


def test_an_empty_password_is_refused(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _signed_in(client, app)
        response = client.post(
            "/api/control/set-password",
            json={"password": "", "confirm": "", "dashboard_password": PASSWORD},
            headers=HEADERS,
        )

    assert response.status_code == 422
    assert _source(app).executed == []


def test_the_command_needs_the_control_header_and_a_session(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/control/set-password", json=_body()).status_code == 401
        _signed_in(client, app)
        missing = client.post("/api/control/set-password", json=_body())
        foreign = client.post(
            "/api/control/set-password",
            json=_body(),
            headers={**HEADERS, "Origin": "http://evil.example"},
        )

    assert missing.status_code == 403
    assert foreign.status_code == 403
    assert _source(app).executed == []


# When the send lies ---------------------------------------------------------


def test_a_change_whose_reply_was_lost_is_still_confirmed(tmp_path):
    app = _app(tmp_path)
    _source(app).faults.add("password_reply_lost")
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client).json()

    # The send failed, but the grill took the change: the bridge asks.
    assert body["outcome"] == "confirmed"
    assert _source(app).password == NEW
    assert app.state.settings.password == NEW
    assert app.state.store.load_pending_password() is None


def test_a_change_that_did_not_take_is_failed(tmp_path):
    app = _app(tmp_path)
    _source(app).faults.add("ignore")
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client).json()

    assert body["outcome"] == "failed"
    assert "old password" in body["detail"]
    # The bridge keeps the password the grill still has.
    assert _source(app).password == OLD
    assert app.state.settings.password == OLD
    assert app.state.store.load_settings() == {}
    assert app.state.store.load_pending_password() is None


def test_a_change_that_cannot_be_reconciled_stays_pending(tmp_path):
    app = _app(tmp_path)
    source = _source(app)

    async def unreachable(password):
        raise ConnectionError("the grill is gone")

    source.check_password = unreachable
    with TestClient(app) as client:
        _signed_in(client, app)
        body = _post(client).json()
        status = client.get("/api/control").json()

    assert body["outcome"] == "unconfirmed"
    # Both passwords are kept, so the bridge can come back on its own.
    pending = app.state.store.load_pending_password()
    assert (pending.old_password, pending.new_password) == (OLD, NEW)
    assert status["pending_password_change"] is not None
    assert "Settings" in body["detail"]


# Recovery -------------------------------------------------------------------


def test_saving_a_password_in_settings_ends_the_pending_change(tmp_path):
    app = _app(tmp_path)
    source = _source(app)

    async def unreachable(password):
        raise ConnectionError("the grill is gone")

    source.check_password = unreachable
    with TestClient(app) as client:
        _signed_in(client, app)
        assert _post(client).json()["outcome"] == "unconfirmed"
        assert app.state.store.load_pending_password() is not None
        # The user says which password the grill has: that ends it.
        response = client.put("/api/settings", json={"password": "typed-in"})

    assert response.status_code == 200
    assert app.state.store.load_pending_password() is None
    assert app.state.settings.password == "typed-in"


def test_saving_a_password_in_settings_beats_a_reconcile_in_flight(tmp_path):
    """A reconcile that is already running must not overwrite the user's
    value: the user knows which password the grill has."""
    settings = _settings(tmp_path)
    finish_setup(settings, password=PASSWORD, required=True)
    # A change was in flight when the bridge stopped.
    store = Database(settings.db_path)
    store.initialize()
    store.save_pending_password(OLD, NEW)
    app = create_app(settings)
    source = app.state.runtime.collector.source
    started = []

    async def slow_check(password):
        started.append(password)
        # Long enough for the user to save a password while this waits.
        await asyncio.sleep(0.5)
        return True

    source.check_password = slow_check
    with TestClient(app) as client:
        _signed_in(client, app)
        # The bridge settles the change on startup, and asks the grill.
        _wait(lambda: started)
        # The user saves the password they know the grill has.
        saved = client.put("/api/settings", json={"password": "typed-in"})

    assert saved.status_code == 200
    # The user's value is the one that is kept.
    assert app.state.settings.password == "typed-in"
    assert app.state.store.load_settings() == {"password": "typed-in"}


def test_a_second_password_change_is_refused_while_one_is_pending(tmp_path):
    app = _app(tmp_path)
    source = _source(app)

    async def unreachable(password):
        raise ConnectionError("the grill is gone")

    source.check_password = unreachable
    with TestClient(app) as client:
        _signed_in(client, app)
        assert _post(client).json()["outcome"] == "unconfirmed"
        pending = app.state.store.load_pending_password()
        # The first change is still holding on to the password the grill
        # may have, so a second one is refused.
        second = _post(client, _body(password="yet-another"))

    assert second.status_code == 409
    assert "still in flight" in second.json()["detail"]
    # The first change's recovery state is untouched.
    assert app.state.store.load_pending_password() == pending


def test_a_password_change_starts_the_gap_between_commands(tmp_path):
    app = _app(tmp_path)
    app.state.control.min_gap_seconds = 5
    with TestClient(app) as client:
        _signed_in(client, app)
        changed = _post(client)
        # Another command has to wait, as after any other one.
        other = client.post(
            "/api/control/set-temperature", json={"temp": 250}, headers=HEADERS
        )

    assert changed.json()["outcome"] == "confirmed"
    assert other.status_code == 429


def test_a_pending_change_is_settled_after_a_restart(tmp_path):
    app = _app(tmp_path)
    source = _source(app)

    async def unreachable(password):
        raise ConnectionError("the grill is gone")

    source.check_password = unreachable
    with TestClient(app) as client:
        _signed_in(client, app)
        assert _post(client).json()["outcome"] == "unconfirmed"

    # The bridge stops with the change in flight, and the grill has the new
    # password: the next start settles it.
    store = Database(app.state.settings.db_path)
    store.save_pending_password(OLD, NEW)
    restarted = create_app(_settings(tmp_path))
    # The grill took the change; the bridge does not know that yet.
    restarted.state.runtime.collector.source.password = NEW
    with TestClient(restarted) as client:
        _wait(
            lambda: restarted.state.store.load_pending_password() is None,
            timeout=5.0,
        )
        status = restarted.state.control.status()

    assert restarted.state.settings.password == NEW
    assert status["pending_password_change"] is None
    events = [
        event
        for event in restarted.state.store.list_health_events(limit=100)
        if event["event_type"] == PASSWORD_EVENT
    ]
    assert events and events[0]["data"]["outcome"] == "confirmed"
