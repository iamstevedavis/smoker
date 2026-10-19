from datetime import UTC, datetime, timedelta

from pitboss_bridge.healthcheck import is_healthy

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def test_connected_is_healthy():
    assert is_healthy({"connected": True, "started_at": "2020-01-01T00:00:00+00:00"}, NOW)


def test_not_connected_is_healthy_only_within_startup_grace():
    recent = (NOW - timedelta(seconds=30)).isoformat()
    old = (NOW - timedelta(seconds=300)).isoformat()

    assert is_healthy({"connected": False, "started_at": recent}, NOW, grace_seconds=120)
    assert not is_healthy({"connected": False, "started_at": old}, NOW, grace_seconds=120)


def test_collector_not_started_yet_is_healthy():
    assert is_healthy({"connected": False, "started_at": None}, NOW)
