import inspect
import logging
import os
import time

import pytest
from fastapi.testclient import TestClient

from smokestack import overrides, runtime
from smokestack import api
from smokestack.config import EDITABLE_FIELDS, Settings, env_name
from smokestack.sources.fake import FakeGrillSource
from smokestack.state import normalize_state
from smokestack.store import Database, ProbeTarget
from conftest import finish_setup


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Origins depend on the environment, so start every test without our variables.
    for name in list(os.environ):
        if name.startswith(("PITBOSS_", "SMOKESTACK_")):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _restore_log_level():
    logger = logging.getLogger("smokestack")
    level = logger.level
    yield
    logger.setLevel(level)


def _settings(tmp_path, **kwargs) -> Settings:
    values = {
        "source": "fake",
        "db_path": tmp_path / "smokestack.sqlite",
        "poll_seconds": 0,
        "start_collector": False,
    }
    values.update(kwargs)
    return Settings(**values)


def create_app(settings: Settings | None = None):
    """An app past first-run setup with the password off; these tests are
    about settings, not logging in."""
    settings = settings or Settings.from_env()
    finish_setup(settings, required=False)
    return api.create_app(settings)


def _fields(payload) -> dict:
    return {
        field["name"]: field for group in payload["groups"] for field in group["fields"]
    }


def _env_app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("PITBOSS_SOURCE", "fake")
    monkeypatch.setenv("SMOKESTACK_DB", str(tmp_path / "smokestack.sqlite"))
    monkeypatch.setenv("SMOKESTACK_START_COLLECTOR", "false")
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
        "control",
        "logging",
    ]
    assert "fake" in payload["sources"]
    assert "SMOKESTACK_DB" in payload["env_only"]
    assert "editable" not in payload
    assert _fields(payload)["poll_seconds"]["env"] == "SMOKESTACK_POLL_SECONDS"


def test_env_names_match_the_variables_settings_reads():
    # The settings panel shows env_name(); it must be the variable from_env() reads.
    source = inspect.getsource(Settings.from_env)
    for name in EDITABLE_FIELDS:
        assert f'"{env_name(name)}"' in source, name
    assert env_name("host") == "PITBOSS_HOST"
    assert env_name("mqtt_host") == "SMOKESTACK_MQTT_HOST"


def test_layering_default_then_env_then_override(tmp_path, monkeypatch):
    app = _env_app(tmp_path, monkeypatch, SMOKESTACK_POLL_SECONDS="5")
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


ADAPTERS = [
    {"name": "hci0", "address": "AA:AA:AA:AA:AA:AA", "alias": "t440p", "powered": True},
    {"name": "hci1", "address": "BB:BB:BB:BB:BB:BB", "alias": "t440p #2", "powered": True},
]


def test_ble_adapter_round_trips_and_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("SMOKESTACK_BLE_ADAPTER", "hci0")
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert _fields(client.get("/api/settings").json())["ble_adapter"]["origin"] == "env"
        response = client.put("/api/settings", json={"ble_adapter": "hci1"})

    assert response.status_code == 200
    field = _fields(response.json())["ble_adapter"]
    assert (field["value"], field["origin"], field["env"]) == (
        "hci1",
        "ui",
        "SMOKESTACK_BLE_ADAPTER",
    )
    app = create_app(_settings(tmp_path))
    assert app.state.settings.ble_adapter == "hci1"

    with TestClient(app) as client:
        response = client.put("/api/settings", json={"ble_adapter": ""})
    assert response.status_code == 200
    assert app.state.settings.ble_adapter == ""


def test_settings_list_the_hosts_bluetooth_adapters(tmp_path, monkeypatch):
    async def list_adapters():
        return ADAPTERS

    monkeypatch.setattr(api.bluetooth, "list_adapters", list_adapters)
    monkeypatch.setattr(overrides, "available_sources", lambda: ("fake", "ble"))
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/api/settings").json()["ble_adapters"] == ADAPTERS

    # Without Bluetooth there's nothing to ask.
    monkeypatch.setattr(overrides, "available_sources", lambda: ("fake", "http"))
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/api/settings").json()["ble_adapters"] == []


def test_enable_control_round_trips_and_survives_a_restart(tmp_path, monkeypatch):
    app = _env_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        field = _fields(client.get("/api/settings").json())["enable_control"]
        assert (field["value"], field["origin"], field["env"]) == (
            False,
            "default",
            "SMOKESTACK_ENABLE_CONTROL",
        )
        field = _fields(
            client.put("/api/settings", json={"enable_control": True}).json()
        )["enable_control"]
        assert (field["value"], field["origin"]) == (True, "ui")
        assert app.state.settings.enable_control is True
        assert client.put(
            "/api/settings", json={"enable_control": "maybe"}
        ).status_code == 422

    restarted = _env_app(tmp_path, monkeypatch)
    assert restarted.state.settings.enable_control is True
    with TestClient(restarted) as client:
        field = _fields(client.delete("/api/settings/enable_control").json())[
            "enable_control"
        ]
    assert (field["value"], field["origin"]) == (False, "default")


def test_enable_control_from_env(tmp_path, monkeypatch):
    app = _env_app(tmp_path, monkeypatch, SMOKESTACK_ENABLE_CONTROL="true")
    with TestClient(app) as client:
        field = _fields(client.get("/api/settings").json())["enable_control"]
    assert (field["value"], field["origin"]) == (True, "env")


def test_mqtt_and_ntfy_switches_round_trip_and_survive_a_restart(
    tmp_path, monkeypatch
):
    app = _env_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        fields = _fields(client.get("/api/settings").json())
        for name in ("mqtt_enabled", "ntfy_enabled"):
            # On by default, so an existing install with a host or URL keeps
            # publishing or alerting after the upgrade.
            assert (fields[name]["value"], fields[name]["origin"]) == (True, "default")

        client.put(
            "/api/settings",
            json={
                "mqtt_host": "127.0.0.1",
                "mqtt_port": 9,
                "ntfy_url": "http://127.0.0.1:9/unreachable",
            },
        )
        assert app.state.runtime.mqtt is not None
        assert app.state.runtime.alerts is not None

        fields = _fields(
            client.put(
                "/api/settings",
                json={"mqtt_enabled": False, "ntfy_enabled": False},
            ).json()
        )
        for name in ("mqtt_enabled", "ntfy_enabled"):
            assert (fields[name]["value"], fields[name]["origin"]) == (False, "ui")
        # Switching off stops the service but keeps where it would send.
        assert app.state.runtime.mqtt is None
        assert app.state.runtime.alerts is None
        assert app.state.settings.mqtt_host == "127.0.0.1"
        assert app.state.settings.ntfy_url == "http://127.0.0.1:9/unreachable"

    restarted = _env_app(tmp_path, monkeypatch)
    assert restarted.state.settings.mqtt_enabled is False
    assert restarted.state.settings.ntfy_enabled is False
    with TestClient(restarted) as client:
        fields = _fields(
            client.put(
                "/api/settings",
                json={"mqtt_enabled": True, "ntfy_enabled": True},
            ).json()
        )
        for name in ("mqtt_enabled", "ntfy_enabled"):
            assert (fields[name]["value"], fields[name]["origin"]) == (True, "ui")
        # Turning them back on reuses the host and URL saved earlier.
        assert restarted.state.runtime.mqtt is not None
        assert restarted.state.runtime.alerts is not None


def test_mqtt_and_ntfy_switches_from_env(tmp_path, monkeypatch):
    app = _env_app(
        tmp_path,
        monkeypatch,
        SMOKESTACK_MQTT_ENABLED="false",
        SMOKESTACK_MQTT_HOST="127.0.0.1",
        SMOKESTACK_MQTT_PORT="9",
        SMOKESTACK_NTFY_ENABLED="false",
        SMOKESTACK_NTFY_URL="http://127.0.0.1:9/unreachable",
    )
    with TestClient(app) as client:
        fields = _fields(client.get("/api/settings").json())
    for name in ("mqtt_enabled", "ntfy_enabled"):
        assert (fields[name]["value"], fields[name]["origin"]) == (False, "env")
    assert app.state.runtime.mqtt is None
    assert app.state.runtime.alerts is None


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
        {"poll_seconds": 3, "mqtt_enabled": "maybe"},
        {"poll_seconds": 3, "ntfy_enabled": "sometimes"},
        {"poll_seconds": 3, "stale_after_seconds": 0},
        {"poll_seconds": 3, "ble_adapter": "../hci0"},
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


def test_probe_targets_are_not_settings(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert "alert_probe1_target" not in _fields(client.get("/api/settings").json())
        response = client.put("/api/settings", json={"alert_probe1_target": 203})

    assert response.status_code == 422


def _report_unit(app, fahrenheit: bool | None) -> None:
    """Put a reading on the hub in the given unit (None: a reading that
    doesn't say)."""
    values = {"grillTemp": 225}
    if fahrenheit is not None:
        values["isFahrenheit"] = fahrenheit
    app.state.hub.current = normalize_state(values, source="fake")


def test_probe_targets_are_set_and_cleared(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/probe-targets").json() == {
            "probe1": None,
            "probe1_unit": None,
            "probe2": None,
            "probe2_unit": None,
            "alerts_enabled": False,
        }
        _report_unit(app, True)
        runtime = app.state.runtime
        body = client.put("/api/probe-targets", json={"probe1": 203}).json()
        assert body == {
            "probe1": 203,
            "probe1_unit": "F",
            "probe2": None,
            "probe2_unit": None,
            "alerts_enabled": False,
        }
        # Partial: probe 1 keeps its target. New targets take the grill's
        # current unit.
        _report_unit(app, False)
        body = client.put("/api/probe-targets", json={"probe2": 74}).json()
        assert body["probe1"] == 203 and body["probe1_unit"] == "F"
        assert body["probe2"] == 74 and body["probe2_unit"] == "C"
        body = client.put("/api/probe-targets", json={"probe1": None}).json()
        assert body["probe1"] is None and body["probe2"] == 74

    # Applied in place, without restarting the grill connection.
    assert app.state.runtime is runtime
    assert app.state.store.load_probe_targets() == {2: ProbeTarget(74, "C")}
    assert app.state.store.load_settings() == {}


def test_probe_targets_keep_the_unit_they_were_entered_in(tmp_path):
    # The dashboard sends the unit its form showed, so a unit change while
    # the form was open doesn't reinterpret the value.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        _report_unit(app, False)
        body = client.put("/api/probe-targets", json={"probe1": 200, "unit": "F"}).json()
        assert body["probe1"] == 200 and body["probe1_unit"] == "F"

        # No reading is needed when the unit is given.
        app.state.hub.current = None
        response = client.put("/api/probe-targets", json={"probe2": 74, "unit": "C"})
        assert response.status_code == 200
        assert client.put("/api/probe-targets", json={"probe1": 9, "unit": "K"}).status_code == 422

    assert app.state.store.load_probe_targets() == {
        1: ProbeTarget(200, "F"),
        2: ProbeTarget(74, "C"),
    }


@pytest.mark.parametrize("fahrenheit", [None, "no reading"])
def test_probe_targets_need_a_known_unit_to_be_set(tmp_path, fahrenheit):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        if fahrenheit is None:
            _report_unit(app, None)
        assert client.put("/api/probe-targets", json={"probe1": 203}).status_code == 409
        assert app.state.store.load_probe_targets() == {}

        # Clearing doesn't need one.
        _report_unit(app, True)
        client.put("/api/probe-targets", json={"probe1": 203})
        app.state.hub.current = None
        response = client.put("/api/probe-targets", json={"probe1": None})
        assert response.status_code == 200
        assert app.state.store.load_probe_targets() == {}


def test_probe_targets_saved_before_units_are_reported_with_no_unit(tmp_path):
    settings = _settings(tmp_path)
    Database(settings.db_path).initialize()
    with Database(settings.db_path).connect() as conn:
        conn.execute(
            "INSERT INTO probe_targets (probe, target, updated_at) VALUES (1, 203, 'x')"
        )
    with TestClient(create_app(settings)) as client:
        body = client.get("/api/probe-targets").json()

    assert body["probe1"] == 203 and body["probe1_unit"] is None


@pytest.mark.parametrize(
    "body", [{"probe1": 0}, {"probe2": 1001}, {"probe3": 100}, {"probe1": "hot"}]
)
def test_bad_probe_targets_are_rejected(tmp_path, body):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.put("/api/probe-targets", json=body).status_code == 422

    assert app.state.store.load_probe_targets() == {}


def test_probe_targets_reach_the_alert_engine_and_survive_a_swap(tmp_path):
    app = create_app(_settings(tmp_path, ntfy_url="http://ntfy.test/t"))
    with TestClient(app) as client:
        assert client.get("/api/probe-targets").json()["alerts_enabled"] is True
        _report_unit(app, True)
        client.put("/api/probe-targets", json={"probe1": 203})
        target = ProbeTarget(203, "F")
        assert app.state.runtime.alerts.engine.config.probe1_target == target

        client.put("/api/settings", json={"alert_drift_degrees": 30})
        assert app.state.runtime.alerts.engine.config.probe1_target == target

        # Turning ntfy off keeps the targets for when it comes back.
        client.put("/api/settings", json={"ntfy_url": ""})
        assert client.get("/api/probe-targets").json() == {
            "probe1": 203,
            "probe1_unit": "F",
            "probe2": None,
            "probe2_unit": None,
            "alerts_enabled": False,
        }


def test_probe_targets_require_login(tmp_path):
    settings = _settings(tmp_path)
    finish_setup(settings, password="pw", required=True)
    with TestClient(api.create_app(settings)) as client:
        assert client.get("/api/probe-targets").status_code == 401
        assert client.put("/api/probe-targets", json={"probe1": 200}).status_code == 401


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
        assert logging.getLogger("smokestack").level == logging.DEBUG
        client.put("/api/settings", json={"log_level": "WARNING"})
        assert logging.getLogger("smokestack").level == logging.WARNING


def test_bad_saved_values_fall_back_to_env(tmp_path):
    store = Database(tmp_path / "smokestack.sqlite")
    store.initialize()
    store.save_settings(
        {"source": "carrier-pigeon", "poll_seconds": 4, "db_path": "/elsewhere"}
    )

    app = create_app(_settings(tmp_path))
    assert app.state.settings.source == "fake"
    assert app.state.settings.poll_seconds == 4
    assert app.state.settings.db_path == tmp_path / "smokestack.sqlite"


def test_settings_endpoints_require_login(tmp_path):
    settings = _settings(tmp_path)
    finish_setup(settings, password="pw")
    app = api.create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 401
        assert client.put("/api/settings", json={"poll_seconds": 3}).status_code == 401
        assert client.delete("/api/settings/poll_seconds").status_code == 401
        assert client.post("/api/alerts/test").status_code == 401

        client.post("/api/login", json={"password": "pw"})
        assert client.get("/api/settings").status_code == 200
        assert client.put("/api/settings", json={"poll_seconds": 3}).status_code == 200


def test_test_alert_uses_the_saved_url_and_token(tmp_path, monkeypatch):
    calls = []

    class FakeSender:
        def __init__(self, url, token=""):
            calls.append(("sender", url, token))

        async def send(self, notification):
            calls.append(("send", notification))

    monkeypatch.setattr(api, "NtfySender", FakeSender)
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        client.put(
            "/api/settings",
            json={
                "ntfy_url": "http://ntfy.test/topic",
                "ntfy_token": "tk_secret",
            },
        )
        calls.clear()
        response = client.post("/api/alerts/test")

    assert response.status_code == 200
    assert response.json() == {"sent": True}
    assert calls[0] == ("sender", "http://ntfy.test/topic", "tk_secret")
    _, notification = calls[1]
    assert notification.title == "Smokestack test alert"
    assert notification.message == "Alerts are working. This is a test from Smokestack."
    assert notification.tags == ("white_check_mark",)


def test_test_alert_needs_alerts_on_and_a_topic(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post("/api/alerts/test")
    assert response.status_code == 409
    assert response.json()["detail"] == "Set an ntfy topic URL first."

    app = create_app(
        _settings(tmp_path, ntfy_url="http://ntfy.test/topic", ntfy_enabled=False)
    )
    with TestClient(app) as client:
        response = client.post("/api/alerts/test")
    assert response.status_code == 409
    assert response.json()["detail"] == "Turn alerts on first."


def test_test_alert_returns_the_ntfy_error(tmp_path, monkeypatch):
    class FailingSender:
        def __init__(self, url, token=""):
            pass

        async def send(self, notification):
            raise RuntimeError("HTTP Error 404: Not Found")

    monkeypatch.setattr(api, "NtfySender", FailingSender)
    app = create_app(_settings(tmp_path, ntfy_url="http://ntfy.test/topic"))
    with TestClient(app) as client:
        response = client.post("/api/alerts/test")

    assert response.status_code == 502
    assert response.json()["detail"] == "HTTP Error 404: Not Found"


def test_saving_settings_does_not_resend_active_alerts(tmp_path, monkeypatch):
    sent: list[str] = []

    class RecordingSender:
        def __init__(self, url, token=""):
            pass

        async def send(self, notification):
            sent.append(notification.key)

    def wait_for_sends(count):
        deadline = time.monotonic() + 2
        while len(sent) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)  # room for a duplicate to show up
        return sent

    monkeypatch.setattr(runtime, "NtfySender", RecordingSender)
    settings = _settings(tmp_path, ntfy_url="http://ntfy.test/t")
    Database(settings.db_path).initialize()
    Database(settings.db_path).save_probe_targets({1: 130}, "F")
    app = create_app(settings)
    with TestClient(app) as client:
        # Probe 1 reads 140, past its 130 target. A new runtime's alert
        # service checks the current reading as soon as it starts.
        reading = FakeGrillSource(0)._reading()
        app.state.hub.current = normalize_state(reading, source="fake")
        client.put("/api/settings", json={"log_level": "DEBUG"})
        assert wait_for_sends(1) == ["probe1_target"]

        # Unrelated saves keep the fired state: no repeat.
        client.put("/api/settings", json={"log_level": "INFO"})
        client.put("/api/settings", json={"alert_drift_degrees": 30})
        assert wait_for_sends(2) == ["probe1_target"]

        # A new target is a new alert, sent without restarting anything.
        client.put("/api/probe-targets", json={"probe1": 135})
        client.portal.call(app.state.hub.publish, app.state.hub.current)
        assert wait_for_sends(2) == ["probe1_target", "probe1_target"]
