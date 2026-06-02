from datetime import UTC, datetime, timedelta

from pitboss_bridge.state import KNOWN_STATE_FIELDS, normalize_state


def test_normalize_state_wraps_each_known_value_with_timestamp():
    observed_at = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    received_at = observed_at + timedelta(seconds=1)

    snapshot = normalize_state(
        {
            "grillTemp": 220,
            "grillSetTemp": 225,
            "p1Temp": 150,
            "fanState": True,
            "surprise": "kept for diagnostics",
        },
        source="fake",
        observed_at=observed_at,
        received_at=received_at,
    )

    assert set(snapshot.fields) == {"grillTemp", "grillSetTemp", "p1Temp", "fanState"}
    assert snapshot.fields["grillTemp"].value == 220
    assert snapshot.fields["grillTemp"].observed_at == observed_at
    assert snapshot.unknown == {"surprise": "kept for diagnostics"}
    assert snapshot.raw["surprise"] == "kept for diagnostics"
    assert "grillTemp" in KNOWN_STATE_FIELDS


def test_snapshot_api_dict_marks_stale_by_age():
    observed_at = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    now = observed_at + timedelta(seconds=45)
    snapshot = normalize_state(
        {"grillTemp": 220},
        source="fake",
        observed_at=observed_at,
        received_at=observed_at,
    )

    payload = snapshot.to_api_dict(now=now, stale_after_seconds=30)

    assert payload["source"] == "fake"
    assert payload["stale"] is True
    assert payload["age_seconds"] == 45.0
    assert payload["fields"]["grillTemp"]["value"] == 220
    assert payload["fields"]["grillTemp"]["age_seconds"] == 45.0
