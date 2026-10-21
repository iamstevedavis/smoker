from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import finish_setup
from smokestack.api import create_app
from smokestack.config import Settings
from smokestack.state import utc_now
from smokestack.store import CONTROL_EVENT

PASSWORD = "brisket!"
HEADERS = {"X-Smokestack-Control": "1"}


def _app(tmp_path, required=True):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "smokestack.sqlite",
        poll_seconds=0,
        start_collector=False,
        enable_control=True,
    )
    finish_setup(settings, password=PASSWORD, required=required)
    return create_app(settings)


def _signed_in(client):
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return client


def _record(app, command, outcome, days_ago=0, **extra):
    return app.state.store.insert_health_event(
        CONTROL_EVENT,
        f"{command}: {outcome}",
        {"command": command, "args": {}, "client": "10.0.0.5", "outcome": outcome, **extra},
        at=utc_now() - timedelta(days=days_ago),
    )


def _commands(app):
    return [item["command"] for item in app.state.store.list_control_events()]


def test_the_log_lists_command_attempts_newest_first(tmp_path):
    app = _app(tmp_path)
    _record(app, "turn-off", "confirmed", days_ago=2)
    app.state.store.insert_health_event("source_error", "gone", {})
    newest = _record(
        app,
        "set-temperature",
        "rejected",
        status_code=429,
        detail="wait 3 s between commands",
    )
    with TestClient(app) as client:
        body = _signed_in(client).get("/api/control-log").json()

    assert (body["count"], body["total"], body["next_before"]) == (2, 2, None)
    first = body["items"][0]
    assert first["id"] == newest
    assert (first["command"], first["client"], first["outcome"]) == (
        "set-temperature",
        "10.0.0.5",
        "rejected",
    )
    assert first["detail"] == "wait 3 s between commands"
    assert first["at"]
    assert body["items"][1]["command"] == "turn-off"


def test_the_log_pages_back_in_time(tmp_path):
    app = _app(tmp_path)
    ids = [_record(app, "turn-off", "confirmed") for _ in range(5)]
    with TestClient(app) as client:
        _signed_in(client)
        page = client.get("/api/control-log?limit=2").json()
        older = client.get(f"/api/control-log?limit=2&before={page['next_before']}").json()
        last = client.get(f"/api/control-log?limit=2&before={older['next_before']}").json()

    assert [i["id"] for i in page["items"]] == ids[:2:-1][:2]
    assert [i["id"] for i in older["items"]] == [ids[2], ids[1]]
    assert [i["id"] for i in last["items"]] == [ids[0]]
    assert last["next_before"] is None


def test_the_log_needs_a_session_and_the_login_on(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/control-log").status_code == 401
        assert client.post(
            "/api/control-log/prune", json={}, headers=HEADERS
        ).status_code == 401

    open_app = _app(tmp_path / "open", required=False)
    _record(open_app, "turn-off", "confirmed")
    with TestClient(open_app) as client:
        assert client.get("/api/control-log").status_code == 403
        assert client.post(
            "/api/control-log/prune", json={}, headers=HEADERS
        ).status_code == 403
    assert _commands(open_app) == ["turn-off"]


def test_pruning_needs_the_header_and_a_matching_origin(tmp_path):
    app = _app(tmp_path)
    _record(app, "turn-off", "confirmed")
    with TestClient(app) as client:
        _signed_in(client)
        missing = client.post("/api/control-log/prune", json={})
        foreign = client.post(
            "/api/control-log/prune",
            json={},
            headers={**HEADERS, "Origin": "http://evil.example"},
        )

    assert (missing.status_code, foreign.status_code) == (403, 403)
    assert _commands(app) == ["turn-off"]


def test_a_dry_run_counts_without_deleting(tmp_path):
    app = _app(tmp_path)
    _record(app, "turn-off", "confirmed", days_ago=40)
    _record(app, "reboot", "confirmed", days_ago=1)
    with TestClient(app) as client:
        _signed_in(client)
        older = client.post(
            "/api/control-log/prune",
            json={"older_than_days": 30, "dry_run": True},
            headers=HEADERS,
        ).json()
        everything = client.post(
            "/api/control-log/prune", json={"dry_run": True}, headers=HEADERS
        ).json()

    assert (older["matching"], older["deleted"]) == (1, 0)
    assert everything["matching"] == 2
    assert _commands(app) == ["reboot", "turn-off"]


def test_pruning_older_than_n_days_keeps_recent_entries(tmp_path):
    app = _app(tmp_path)
    _record(app, "turn-off", "confirmed", days_ago=40)
    _record(app, "reboot", "confirmed", days_ago=1)
    app.state.store.insert_health_event(
        "source_error", "old", {}, at=utc_now() - timedelta(days=40)
    )
    with TestClient(app) as client:
        _signed_in(client)
        body = client.post(
            "/api/control-log/prune", json={"older_than_days": 30}, headers=HEADERS
        ).json()

    assert (body["deleted"], body["total"]) == (1, 1)
    assert _commands(app) == ["reboot"]
    types = [e["event_type"] for e in app.state.store.list_health_events(limit=100)]
    assert "source_error" in types


def test_pruning_everything_is_recorded_outside_the_log(tmp_path):
    app = _app(tmp_path)
    _record(app, "turn-off", "confirmed", days_ago=40)
    _record(app, "reboot", "confirmed")
    with TestClient(app) as client:
        _signed_in(client)
        body = client.post("/api/control-log/prune", json={}, headers=HEADERS).json()
        status = client.get("/api/control").json()

    assert (body["deleted"], body["total"]) == (2, 0)
    assert _commands(app) == []
    assert status["log_count"] == 0
    [pruned] = [
        e
        for e in app.state.store.list_health_events(limit=100)
        if e["event_type"] == "control_log_pruned"
    ]
    assert pruned["data"] == {"older_than_days": None, "deleted": 2, "client": "testclient"}


@pytest.mark.parametrize(
    "body",
    [{"older_than_days": 0}, {"older_than_days": -1}, {"days": 3}, {"dry_run": "maybe"}],
)
def test_bad_prune_requests_are_rejected(tmp_path, body):
    app = _app(tmp_path)
    _record(app, "turn-off", "confirmed", days_ago=40)
    with TestClient(app) as client:
        _signed_in(client)
        assert client.post(
            "/api/control-log/prune", json=body, headers=HEADERS
        ).status_code == 422
    assert _commands(app) == ["turn-off"]


def test_control_status_reports_the_log_size(tmp_path):
    app = _app(tmp_path)
    _record(app, "turn-off", "confirmed")
    with TestClient(app) as client:
        assert _signed_in(client).get("/api/control").json()["log_count"] == 1
