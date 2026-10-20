import json
import time
from datetime import UTC, datetime, timedelta

from smokestack.state import normalize_state
from smokestack.store import Database


def test_database_persists_and_lists_history(tmp_path):
    db = Database(tmp_path / "smokestack.sqlite")
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
    db = Database(tmp_path / "smokestack.sqlite")
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
    db = Database(tmp_path / "smokestack.sqlite")
    db.initialize()

    db.insert_health_event("connect", "fake source connected", {"source": "fake"})
    events = db.list_health_events()

    assert events[0]["event_type"] == "connect"
    assert events[0]["message"] == "fake source connected"
    assert events[0]["data"] == {"source": "fake"}


def _seed(db, start, count, step_seconds=2, temp=lambda i: 200 + i % 50):
    from datetime import timedelta

    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO readings (observed_at, received_at, source, fields_json,"
            " unknown_json, raw_json) VALUES (?, ?, 'fake', ?, '{}', '{}')",
            [
                (
                    (start + timedelta(seconds=i * step_seconds)).isoformat(),
                    (start + timedelta(seconds=i * step_seconds)).isoformat(),
                    json.dumps({"grillTemp": temp(i), "isOn": i % 2 == 0, "mode": f"m{i}"}),
                )
                for i in range(count)
            ],
        )


T0 = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def test_history_until_and_newest_first(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    db.initialize()
    _seed(db, T0, 10, step_seconds=60)

    window = db.list_history(since=T0 + timedelta(minutes=2), until=T0 + timedelta(minutes=4))
    assert [r["fields"]["mode"] for r in window] == ["m2", "m3", "m4"]

    newest = db.list_history(limit=3, newest_first=True)
    assert [r["fields"]["mode"] for r in newest] == ["m9", "m8", "m7"]
    # default behaviour still returns the oldest rows first
    assert [r["fields"]["mode"] for r in db.list_history(limit=2)] == ["m0", "m1"]


def test_history_bucketing_averages_numbers_and_keeps_last_other(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    db.initialize()
    # 12 readings 10s apart -> two 60s buckets of 6
    _seed(db, T0, 12, step_seconds=10, temp=lambda i: i)

    rows = db.list_history(bucket_seconds=60)

    assert len(rows) == 2
    assert rows[0]["fields"]["grillTemp"] == 2.5  # mean of 0..5
    assert rows[1]["fields"]["grillTemp"] == 8.5  # mean of 6..11
    assert rows[0]["fields"]["isOn"] is False  # last value (i=5), not averaged
    assert rows[0]["fields"]["mode"] == "m5"
    assert rows[0]["samples"] == 6
    assert rows[0]["observed_at"] == T0.isoformat()

    newest = db.list_history(bucket_seconds=60, limit=1, newest_first=True)
    assert len(newest) == 1
    assert newest[0]["fields"]["grillTemp"] == 8.5


def test_14_hour_history_downsamples_quickly(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    db.initialize()
    _seed(db, T0, 14 * 3600 // 2)

    started = time.perf_counter()
    rows = db.list_history(bucket_seconds=60, limit=1000)
    elapsed = time.perf_counter() - started

    assert len(rows) == 14 * 60
    assert elapsed < 1.0


def test_prune_removes_old_rows_and_events(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    db.initialize()
    _seed(db, T0, 10, step_seconds=60)
    db.insert_health_event("old", "x", at=T0)
    db.insert_health_event("new", "y", at=T0 + timedelta(hours=1))

    deleted = db.prune(T0 + timedelta(minutes=5))

    assert deleted == {"readings": 5, "health_events": 1}
    assert len(db.list_history()) == 5
    assert [e["event_type"] for e in db.list_health_events()] == ["new"]


def test_probe_targets_are_set_and_cleared(tmp_path):
    db = Database(tmp_path / "smokestack.sqlite")
    db.initialize()

    db.save_probe_targets({1: 203, 2: 165})
    db.save_probe_targets({1: 205})
    assert db.load_probe_targets() == {1: 205, 2: 165}
    db.save_probe_targets({2: None})
    assert db.load_probe_targets() == {1: 205}


def test_probe_targets_saved_as_settings_are_carried_over(tmp_path):
    db = Database(tmp_path / "smokestack.sqlite")
    db.initialize()
    db.save_settings(
        {"alert_probe1_target": 203, "alert_probe2_target": None, "poll_seconds": 3}
    )

    db.initialize()

    assert db.load_probe_targets() == {1: 203}
    assert db.load_settings() == {"poll_seconds": 3}
