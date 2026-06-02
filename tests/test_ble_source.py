import pytest

from pitboss_bridge.sources.pytboss_ble import PytbossBleSource


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
