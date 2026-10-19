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


def test_unusable_fields_stay_in_raw_but_not_in_fields():
    snapshot = normalize_state(
        {
            "grillTemp": 220,
            "smokerActTemp": 220,
            "p3Temp": None,
            "p4Temp": None,
            "p1Target": 50,
            "p2Target": 50,
        },
        source="fake",
    )

    assert set(snapshot.fields) == {"grillTemp", "p2Target"}
    assert snapshot.unknown == {}
    assert snapshot.raw["smokerActTemp"] == 220
    assert snapshot.raw["p1Target"] == 50
    assert "p3Temp" in snapshot.raw


def test_p1_target_other_than_firmware_default_is_kept():
    snapshot = normalize_state({"p1Target": 165}, source="fake")

    assert snapshot.fields["p1Target"].value == 165


def test_fake_source_state_omits_unusable_fields():
    from pitboss_bridge.sources.fake import FakeGrillSource

    raw = FakeGrillSource(interval_seconds=0)._reading()
    snapshot = normalize_state(raw, source="fake")

    assert "smokerActTemp" in snapshot.raw
    assert "smokerActTemp" not in snapshot.fields


def test_unknown_model_keeps_every_known_field():
    snapshot = normalize_state(
        {"smokerActTemp": 200, "p1Target": 50}, source="x", model="OTHER"
    )

    assert set(snapshot.fields) == {"smokerActTemp", "p1Target"}
