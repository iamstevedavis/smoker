from types import SimpleNamespace

from smokestack import bluetooth


def _variant(value):
    return SimpleNamespace(value=value)


def test_adapters_come_from_bluez_managed_objects():
    objects = {
        "/org/bluez": {"org.bluez.AgentManager1": {}},
        "/org/bluez/hci1": {
            "org.bluez.Adapter1": {
                "Address": _variant("BB:BB:BB:BB:BB:BB"),
                "Alias": _variant("t440p #2"),
                "Powered": _variant(False),
            }
        },
        "/org/bluez/hci0": {
            "org.bluez.Adapter1": {
                "Address": _variant("AA:AA:AA:AA:AA:AA"),
                "Alias": _variant("t440p"),
                "Powered": _variant(True),
            }
        },
        "/org/bluez/hci0/dev_11_22_33_44_55_66": {"org.bluez.Device1": {}},
    }

    assert bluetooth.adapters_from(objects) == [
        {"name": "hci0", "address": "AA:AA:AA:AA:AA:AA", "alias": "t440p", "powered": True},
        {"name": "hci1", "address": "BB:BB:BB:BB:BB:BB", "alias": "t440p #2", "powered": False},
    ]


async def test_no_adapters_when_bluez_cannot_be_reached(monkeypatch):
    async def unreachable():
        raise FileNotFoundError("/run/dbus/system_bus_socket")

    monkeypatch.setattr(bluetooth.sys, "platform", "linux")
    monkeypatch.setattr(bluetooth, "_managed_objects", unreachable)

    assert await bluetooth.list_adapters() == []


async def test_no_adapters_off_linux(monkeypatch):
    monkeypatch.setattr(bluetooth.sys, "platform", "darwin")

    assert await bluetooth.list_adapters() == []
