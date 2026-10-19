import socket

import pytest

from pitboss_bridge import runtime
from pitboss_bridge.runtime import available_sources, system_bus_reachable


@pytest.fixture
def bus_socket(tmp_path):
    path = tmp_path / "bus"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    yield path
    server.close()


@pytest.fixture
def fresh_sources():
    available_sources.cache_clear()
    yield
    available_sources.cache_clear()


def test_bus_reachable_when_the_socket_exists(bus_socket):
    env = {"DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={bus_socket}"}
    assert system_bus_reachable("linux", env) is True


def test_bus_unreachable_without_the_socket(tmp_path):
    missing = {"DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={tmp_path / 'nope'}"}
    assert system_bus_reachable("linux", missing) is False
    plain_file = tmp_path / "file"
    plain_file.write_text("")
    not_a_socket = {"DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={plain_file}"}
    assert system_bus_reachable("linux", not_a_socket) is False


def test_bus_tries_each_address(tmp_path, bus_socket):
    env = {
        "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={tmp_path / 'nope'};unix:path={bus_socket}"
    }
    assert system_bus_reachable("linux", env) is True


def test_bus_defaults_to_the_standard_socket(monkeypatch, bus_socket):
    monkeypatch.setattr(runtime, "DEFAULT_SYSTEM_BUS", str(bus_socket))
    assert system_bus_reachable("linux", {}) is True
    monkeypatch.setattr(runtime, "DEFAULT_SYSTEM_BUS", str(bus_socket) + "-gone")
    assert system_bus_reachable("linux", {}) is False


@pytest.mark.parametrize(
    "address", ["unix:abstract=/tmp/dbus-x,guid=1", "tcp:host=localhost,port=1"]
)
def test_bus_addresses_without_a_file_are_assumed_reachable(address):
    assert system_bus_reachable("linux", {"DBUS_SYSTEM_BUS_ADDRESS": address}) is True


def test_bus_check_only_applies_to_linux():
    # macOS and Windows bleak backends don't use D-Bus.
    missing = {"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/bus"}
    assert system_bus_reachable("darwin", missing) is True
    assert system_bus_reachable("win32", missing) is True


def test_ble_is_offered_only_with_the_bus(monkeypatch, fresh_sources):
    pytest.importorskip("bleak")
    pytest.importorskip("pytboss")
    monkeypatch.setattr(runtime, "system_bus_reachable", lambda: False)
    assert "ble" not in available_sources()
    assert "fake" in available_sources()

    available_sources.cache_clear()
    monkeypatch.setattr(runtime, "system_bus_reachable", lambda: True)
    assert "ble" in available_sources()
