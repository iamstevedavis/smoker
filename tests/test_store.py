from datetime import UTC, datetime

from pitboss_bridge.state import normalize_state
from pitboss_bridge.store import Database


def test_database_persists_and_lists_history(tmp_path):
    db = Database(tmp_path / "pitboss.sqlite")
    db.initialize()
    snapshot = normalize_state(
        {"grillTemp": 220, "p1Temp": 150, "unexpected": "raw"},
        source="fake",
        observed_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 6, 2, 12, 0, 1, tzinfo=UTC),
    )

    db.insert_reading(snapshot)
    rows = db.list_history()

    assert len(rows) == 1
    assert rows[0]["source"] == "fake"
    assert rows[0]["fields"]["grillTemp"] == 220
    assert rows[0]["fields"]["p1Temp"] == 150
    assert rows[0]["unknown"] == {"unexpected": "raw"}
    assert rows[0]["raw"]["unexpected"] == "raw"


def test_database_filters_history_by_since(tmp_path):
    db = Database(tmp_path / "pitboss.sqlite")
    db.initialize()
    old = normalize_state(
        {"grillTemp": 200},
        source="fake",
        observed_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
        received_at=datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
    )
    new = normalize_state(
        {"grillTemp": 225},
        source="fake",
        observed_at=datetime(2026, 6, 2, 12, 5, tzinfo=UTC),
        received_at=datetime(2026, 6, 2, 12, 5, tzinfo=UTC),
    )

    db.insert_reading(old)
    db.insert_reading(new)
    rows = db.list_history(since=datetime(2026, 6, 2, 12, 1, tzinfo=UTC))

    assert len(rows) == 1
    assert rows[0]["fields"]["grillTemp"] == 225


def test_database_records_health_events(tmp_path):
    db = Database(tmp_path / "pitboss.sqlite")
    db.initialize()

    db.insert_health_event("connect", "fake source connected", {"source": "fake"})
    events = db.list_health_events()

    assert events[0]["event_type"] == "connect"
    assert events[0]["message"] == "fake source connected"
    assert events[0]["data"] == {"source": "fake"}
