import logging
import os

import pytest
from fastapi.testclient import TestClient

from pitboss_bridge import overrides
from pitboss_bridge.api import create_app
from pitboss_bridge.config import EDITABLE_FIELDS, Settings
from pitboss_bridge.store import Database


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Origins depend on the environment, so start every test without PITBOSS_*.
    for name in list(os.environ):
        if name.startswith("PITBOSS_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _restore_log_level():
    logger = logging.getLogger("pitboss_bridge")
    level = logger.level
    yield
    logger.setLevel(level)


def _settings(tmp_path, **kwargs) -> Settings:
    values = {
        "source": "fake",
        "db_path": tmp_path / "pitboss.sqlite",
        "poll_seconds": 0,
        "start_collector": False,
        # Editing needs a login by default; most tests are about editing.
        "settings_editable": True,
    }
    values.update(kwargs)
    return Settings(**values)


def _fields(payload) -> dict:
    return {
        field["name"]: field for group in payload["groups"] for field in group["fields"]
    }


def _env_app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("PITBOSS_SOURCE", "fake")
    monkeypatch.setenv("PITBOSS_DB", str(tmp_path / "pitboss.sqlite"))
    monkeypatch.setenv("PITBOSS_START_COLLECTOR", "false")
    monkeypatch.setenv("PITBOSS_SETTINGS_EDITABLE", "true")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return create_app()


def test_lists_every_editable_field_once(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        payload = client.get("/api/settings").json()

    names = [f["name"] for g in payload["groups"] for f in g["fields"]]
    assert sorted(names) == sorted(EDITABLE_FIELDS)
    assert [g["name"] for g in payload["groups"]] == [
        "grill",
        "mqtt",
        "alerts",
        "data",
        "logging",
    ]
    assert payload["editable"] is True
    assert "fake" in payload["sources"]
    assert "PITBOSS_UI_PASSWORD" in payload["env_only"]
    assert _fields(payload)["poll_seconds"]["env"] == "PITBOSS_POLL_SECONDS"


def test_layering_default_then_env_then_override(tmp_path, monkeypatch):
    app = _env_app(tmp_path, monkeypatch, PITBOSS_POLL_SECONDS="5")
    with TestClient(app) as client:
        fields = _fields(client.get("/api/settings").json())
        assert (fields["poll_seconds"]["value"], fields["poll_seconds"]["origin"]) == (
            5,
            "env",
        )
        assert fields["mqtt_port"]["value"] == 1883
        assert fields["mqtt_port"]["origin"] == "default"
        assert fields["source"]["origin"] == "env"

        fields = _fields(
            client.put("/api/settings", json={"poll_seconds": 3, "mqtt_port": 1884}).json()
        )
        assert (fields["poll_seconds"]["value"], fields["poll_seconds"]["origin"]) == (
            3,
            "ui",
        )
        assert (fields["mqtt_port"]["value"], fields["mqtt_port"]["origin"]) == (1884, "ui")
        assert app.state.settings.poll_seconds == 3

        fields = _fields(client.delete("/api/settings/poll_seconds").json())
        assert (fields["poll_seconds"]["value"], fields["poll_seconds"]["origin"]) == (
            5,
            "env",
        )
        fields = _fields(client.delete("/api/settings/mqtt_port").json())
        assert (fields["mqtt_port"]["value"], fields["mqtt_port"]["origin"]) == (
            1883,
            "default",
        )
        assert app.state.settings.poll_seconds == 5

    assert app.state.store.load_settings() == {}


def test_overrides_survive_a_restart(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.put("/api/settings", json={"retention_days": 7, "log_level": "DEBUG"})

    app = create_app(_settings(tmp_path))
    assert app.state.settings.retention_days == 7
    assert app.state.settings.log_level == "DEBUG"
    with TestClient(app) as client:
        fields = _fields(client.get("/api/settings").json())
    assert fields["retention_days"]["origin"] == "ui"


def test_values_passed_to_create_app_count_as_env(tmp_path):
    with TestClient(create_app(_settings(tmp_path, mqtt_prefix="grill"))) as client:
        fields = _fields(client.get("/api/settings").json())
    assert fields["mqtt_prefix"]["origin"] == "env"
    assert fields["mqtt_host"]["origin"] == "default"


def test_secrets_are_never_returned(tmp_path, monkeypatch):
    app = _env_app(tmp_path, monkeypatch, PITBOSS_PASSWORD="grill-secret")
    with TestClient(app) as client:
        put = client.put(
            "/api/settings",
            json={"mqtt_password": "mqtt-secret", "ntfy_token": "tk_secret"},
        )
        get = client.get("/api/settings")

    for response in (put, get):
        assert response.status_code == 200
        for secret in ("grill-secret", "mqtt-secret", "tk_secret"):
            assert secret not in response.text
    fields = _fields(get.json())
    assert fields["password"] == {
        "name": "password",
        "env": "PITBOSS_PASSWORD",
        "value": {"set": True},
        "origin": "env",
        "secret": True,
    }
    assert fields["mqtt_password"]["value"] == {"set": True}
    assert fields["mqtt_password"]["origin"] == "ui"
    assert fields["ntfy_token"]["value"] == {"set": True}
    # Saved and applied, just not shown.
    assert app.state.settings.mqtt_password == "mqtt-secret"


@pytest.mark.parametrize(
    "body",
    [
        {"poll_seconds": 3, "mqtt_port": 70000},
        {"poll_seconds": 3, "poll_seconds_typo": 1},
        {"poll_seconds": 3, "db_path": "/tmp/elsewhere.sqlite"},
        {"poll_seconds": 3, "ui_password": "x"},
        {"poll_seconds": 3, "settings_editable": False},
        {"poll_seconds": 3, "source": "carrier-pigeon"},
        {"poll_seconds": 3, "log_level": "LOUD"},
        {"poll_seconds": 3, "ntfy_url": "ntfy.sh/topic"},
        {"poll_seconds": 3, "mqtt_prefix": "pit/#"},
        {"poll_seconds": 3, "stale_after_seconds": 0},
        {"poll_seconds": 0},
        {"poll_seconds": None},
        {"model": ""},
    ],
)
def test_validation_rejects_the_whole_update(tmp_path, body):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        runtime = app.state.runtime
        response = client.put("/api/settings", json=body)

    assert response.status_code == 422
    assert app.state.runtime is runtime
    assert app.state.settings.poll_seconds == 0
    assert app.state.store.load_settings() == {}


def test_source_without_its_host_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(overrides, "available_sources", lambda: ("fake", "http"))
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"source": "http"})

    assert response.status_code == 422
    assert "host" in response.json()["detail"]
    assert app.state.settings.source == "fake"
    assert app.state.store.load_settings() == {}


def test_probe_targets_can_be_cleared_with_null(tmp_path):
    app = create_app(_settings(tmp_path, alert_probe1_target=203))
    with TestClient(app) as client:
        fields = _fields(
            client.put("/api/settings", json={"alert_probe1_target": None}).json()
        )

    assert fields["alert_probe1_target"]["value"] is None
    assert fields["alert_probe1_target"]["origin"] == "ui"
    assert app.state.store.load_settings() == {"alert_probe1_target": None}


def test_reset_unknown_or_env_only_field_is_404(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.delete("/api/settings/nope").status_code == 404
        assert client.delete("/api/settings/db_path").status_code == 404
        # Resetting a field with no override is a no-op.
        assert client.delete("/api/settings/poll_seconds").status_code == 200


def test_apply_swaps_runtime_and_stops_old_tasks(tmp_path):
    app = create_app(
        _settings(
            tmp_path,
            poll_seconds=0.01,
            start_collector=True,
            ntfy_url="http://127.0.0.1:9/unreachable",
        )
    )
    with TestClient(app) as client:
        old = app.state.runtime
        old_tasks = list(old.tasks)
        assert len(old_tasks) == 2  # alerts + collector

        client.put("/api/settings", json={"poll_seconds": 0.02})

        new = app.state.runtime
        assert new is not old
        assert all(task.done() for task in old_tasks)
        assert old.tasks == []
        assert len(new.tasks) == 2
        assert not any(task.done() for task in new.tasks)
        assert app.state.health is new.health
        assert app.state.collector is new.collector
        assert new.collector.source.interval_seconds == 0.02
        health = client.get("/api/health").json()
        assert health["source"] == "fake"
    assert all(task.done() for task in new.tasks)


def test_state_and_health_follow_the_new_runtime(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        assert client.get("/api/state").json()["available"] is True
        assert client.get("/api/health").json()["connected"] is True

        # A different grill: the old reading is no longer current.
        client.put("/api/settings", json={"model": "PB1150PS2"})
        assert client.get("/api/state").json()["available"] is False
        assert client.get("/api/health").json()["connected"] is False

        app.state.collector_sync_collect_next()
        assert client.get("/api/state").json()["available"] is True
        assert client.get("/api/health").json()["connected"] is True

        # A non-grill change keeps the last reading.
        client.put("/api/settings", json={"stale_after_seconds": 90})
        assert client.get("/api/state").json()["available"] is True
        assert client.get("/api/health").json()["stale_after_seconds"] == 90


def test_enabling_and_disabling_mqtt_and_alerts(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert app.state.runtime.mqtt is None
        assert app.state.runtime.alerts is None
        assert app.state.runtime.tasks == []

        client.put(
            "/api/settings",
            json={
                "mqtt_host": "127.0.0.1",
                "mqtt_port": 9,
                "ntfy_url": "http://127.0.0.1:9/unreachable",
            },
        )
        runtime = app.state.runtime
        assert runtime.mqtt is not None
        assert runtime.mqtt.config.port == 9
        assert runtime.alerts is not None
        running = list(runtime.tasks)
        assert len(running) == 2
        assert not any(task.done() for task in running)

        client.delete("/api/settings/mqtt_host")
        assert app.state.runtime.mqtt is None
        assert app.state.runtime.alerts is not None
        assert all(task.done() for task in running)

        client.put("/api/settings", json={"ntfy_url": ""})
        assert app.state.runtime.alerts is None
        assert app.state.runtime.tasks == []


def test_log_level_is_applied(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.put("/api/settings", json={"log_level": "DEBUG"})
        assert logging.getLogger("pitboss_bridge").level == logging.DEBUG
        client.put("/api/settings", json={"log_level": "WARNING"})
        assert logging.getLogger("pitboss_bridge").level == logging.WARNING


def test_bad_saved_values_fall_back_to_env(tmp_path):
    store = Database(tmp_path / "pitboss.sqlite")
    store.initialize()
    store.save_settings(
        {"source": "carrier-pigeon", "poll_seconds": 4, "db_path": "/elsewhere"}
    )

    app = create_app(_settings(tmp_path))
    assert app.state.settings.source == "fake"
    assert app.state.settings.poll_seconds == 4
    assert app.state.settings.db_path == tmp_path / "pitboss.sqlite"


@pytest.mark.parametrize(
    ("editable", "ui_password", "reason"),
    [
        (False, "", "disabled"),
        (False, "pw", "disabled"),
        # Without a login anyone on the LAN could re-point MQTT/ntfy at their
        # own server and collect the saved secrets.
        (None, "", "no_login"),
    ],
)
def test_settings_can_be_read_only(tmp_path, editable, ui_password, reason):
    app = create_app(
        _settings(tmp_path, settings_editable=editable, ui_password=ui_password)
    )
    with TestClient(app) as client:
        if ui_password:
            client.post("/api/login", json={"password": ui_password})
        payload = client.get("/api/settings").json()
        put = client.put("/api/settings", json={"poll_seconds": 3})
        delete = client.delete("/api/settings/poll_seconds")

    assert (payload["editable"], payload["read_only_reason"]) == (False, reason)
    assert put.status_code == 403
    assert delete.status_code == 403
    assert app.state.store.load_settings() == {}


@pytest.mark.parametrize(
    ("editable", "ui_password"),
    [(None, "pw"), (True, ""), (True, "pw")],
)
def test_settings_are_editable_with_a_login_or_when_enabled(
    tmp_path, editable, ui_password
):
    app = create_app(
        _settings(tmp_path, settings_editable=editable, ui_password=ui_password)
    )
    with TestClient(app) as client:
        if ui_password:
            client.post("/api/login", json={"password": ui_password})
        payload = client.get("/api/settings").json()
        put = client.put("/api/settings", json={"poll_seconds": 3})

    assert (payload["editable"], payload["read_only_reason"]) == (True, None)
    assert put.status_code == 200


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, None), ("", None), ("false", False), ("true", True)]
)
def test_editable_switch_reads_env(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv("PITBOSS_SETTINGS_EDITABLE", raw)
    assert Settings.from_env().settings_editable is expected


def test_settings_endpoints_require_login(tmp_path):
    app = create_app(_settings(tmp_path, ui_password="pw", settings_editable=None))
    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 401
        assert client.put("/api/settings", json={"poll_seconds": 3}).status_code == 401
        assert client.delete("/api/settings/poll_seconds").status_code == 401

        client.post("/api/login", json={"password": "pw"})
        assert client.get("/api/settings").status_code == 200
        assert client.put("/api/settings", json={"poll_seconds": 3}).status_code == 200
