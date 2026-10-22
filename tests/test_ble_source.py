import pytest

from smokestack.config import Settings
from smokestack.sources.pytboss_ble import PytbossBleSource


def test_ble_source_requires_device_name():
    source = PytbossBleSource(model="PB850PS2", device_name="", password="", poll_seconds=2)

    with pytest.raises(ValueError, match="device_name"):
        source.validate()


def test_ble_source_exposes_name_and_settings():
    source = PytbossBleSource(
        model="PB850PS2",
        device_name="PBL-EC6260C77A8C",
        password="secret",
        poll_seconds=2,
    )

    source.validate()

    assert source.name == "ble"
    assert source.model == "PB850PS2"
    assert source.device_name == "PBL-EC6260C77A8C"


def _scan(monkeypatch, error=None):
    """Patch the BLE scan; return the keyword arguments each scan got."""
    bleak = pytest.importorskip("bleak")
    calls = []

    async def find_device(_filter, **kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return None

    monkeypatch.setattr(bleak.BleakScanner, "find_device_by_filter", find_device)
    return calls


async def test_ble_source_scans_on_the_chosen_adapter(monkeypatch):
    calls = _scan(monkeypatch)
    source = PytbossBleSource(
        model="PB850PS2", device_name="PBL-1", password="", adapter="hci1"
    )

    await source._find_device()

    assert calls == [{"adapter": "hci1"}]


async def test_ble_source_leaves_the_adapter_to_bleak_by_default(monkeypatch):
    calls = _scan(monkeypatch)
    source = PytbossBleSource(model="PB850PS2", device_name="PBL-1", password="")

    await source._find_device()

    assert calls == [{}]


async def test_ble_source_names_the_setting_when_the_adapter_is_missing(monkeypatch):
    bleak_exc = pytest.importorskip("bleak.exc")
    _scan(monkeypatch, error=bleak_exc.BleakError("adapter 'hci7' not found"))
    source = PytbossBleSource(
        model="PB850PS2", device_name="PBL-1", password="", adapter="hci7"
    )

    with pytest.raises(RuntimeError, match="SMOKESTACK_BLE_ADAPTER.*'hci7' not found"):
        await source._find_device()


def test_ble_adapter_comes_from_the_env_and_reaches_the_source(monkeypatch):
    from smokestack.runtime import make_source

    monkeypatch.setenv("PITBOSS_SOURCE", "ble")
    monkeypatch.setenv("SMOKESTACK_BLE_ADAPTER", "hci1")
    assert make_source(Settings.from_env()).adapter == "hci1"

    monkeypatch.delenv("SMOKESTACK_BLE_ADAPTER")
    assert make_source(Settings.from_env()).adapter == ""
