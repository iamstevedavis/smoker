import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from smokestack.api import create_app
from smokestack.config import Settings
from smokestack.store import Database

T0 = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def _seed(db, start, temps):
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO readings (observed_at, received_at, source, fields_json,"
            " unknown_json, raw_json) VALUES (?, ?, 'fake', ?, '{}', '{}')",
            [
                (
                    (start + timedelta(minutes=i)).isoformat(),
                    (start + timedelta(minutes=i)).isoformat(),
                    json.dumps({"grillTemp": t, "p1Temp": t - 100}),
                )
                for i, t in enumerate(temps)
            ],
        )


def test_cook_lifecycle_and_single_active_rule(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    db.initialize()

    cook = db.start_cook("Brisket", meat="beef", at=T0)
    assert cook["ended_at"] is None
    assert db.start_cook("Another") is None

    stopped = db.stop_cook(cook["id"], at=T0 + timedelta(hours=1))
    assert stopped["ended_at"] is not None
    assert db.stop_cook(cook["id"]) is None
    assert db.start_cook("Ribs", at=T0 + timedelta(hours=2)) is not None
    assert [c["name"] for c in db.list_cooks()] == ["Ribs", "Brisket"]


def test_cooks_survive_reinitialize(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    db.initialize()
    db.start_cook("Brisket", at=T0)

    Database(tmp_path / "p.sqlite").initialize()

    assert [c["name"] for c in db.list_cooks()] == ["Brisket"]


def test_cook_summary_stats_cover_only_the_cook_window(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    db.initialize()
    _seed(db, T0 - timedelta(minutes=2), [500, 500])  # before the cook
    _seed(db, T0, [200, 220, 240])
    cook = db.start_cook("Brisket", at=T0)
    cook = db.stop_cook(cook["id"], at=T0 + timedelta(minutes=2))

    summary = db.cook_summary(cook)

    assert summary["duration_seconds"] == 120
    assert summary["readings"] == 3
    assert summary["grill_temp"] == {"min": 200, "max": 240, "avg": 220.0}
    assert summary["probe1_max"] == 140


@pytest.fixture
def client(tmp_path, make_app):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "smokestack.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = make_app(settings)
    with TestClient(app) as test_client:
        test_client.app_state = app.state
        yield test_client


def test_cook_endpoints(client):
    created = client.post("/api/cooks", json={"name": "Brisket", "meat": "beef"})
    assert created.status_code == 201
    cook_id = created.json()["id"]
    assert client.post("/api/cooks", json={"name": "Dup"}).status_code == 409
    assert client.post("/api/cooks", json={"name": ""}).status_code == 422

    patched = client.patch(f"/api/cooks/{cook_id}", json={"notes": "wrapped at 165"})
    assert patched.json()["notes"] == "wrapped at 165"
    assert patched.json()["name"] == "Brisket"

    client.app_state.collector_sync_collect_next()
    client.app_state.collector_sync_collect_next()
    detail = client.get(f"/api/cooks/{cook_id}").json()
    assert detail["summary"]["readings"] == 2

    stopped = client.post(f"/api/cooks/{cook_id}/stop")
    assert stopped.status_code == 200
    assert client.post(f"/api/cooks/{cook_id}/stop").status_code == 409
    assert client.get("/api/cooks/999").status_code == 404
    assert client.post("/api/cooks/999/stop").status_code == 404

    listing = client.get("/api/cooks").json()
    assert listing["count"] == 1


def test_cook_history_is_window_bounded_and_bucketable(client):
    store = client.app_state.store
    _seed(store, T0 - timedelta(minutes=5), [999])
    cook = store.start_cook("Brisket", at=T0)
    _seed(store, T0, [200, 220, 240, 260])
    store.stop_cook(cook["id"], at=T0 + timedelta(minutes=2, seconds=30))
    _seed(store, T0 + timedelta(minutes=10), [999])

    plain = client.get(f"/api/cooks/{cook['id']}/history").json()
    bucketed = client.get(
        f"/api/cooks/{cook['id']}/history?bucket_seconds=120"
    ).json()

    assert [i["fields"]["grillTemp"] for i in plain["items"]] == [200, 220, 240]
    assert [i["fields"]["grillTemp"] for i in bucketed["items"]] == [210, 240]
