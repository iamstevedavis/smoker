from fastapi.testclient import TestClient

from pitboss_bridge.config import Settings


def test_health_state_and_history_endpoints(tmp_path, make_app):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = make_app(settings)

    with TestClient(app) as client:
        app.state.collector_sync_collect_next()

        health = client.get("/api/health").json()
        state = client.get("/api/state").json()
        history = client.get("/api/history").json()

    assert health["source"] == "fake"
    assert health["connected"] is True
    assert state["available"] is True
    assert state["fields"]["grillSetTemp"]["value"] == 225
    assert history["count"] == 1
    assert history["items"][0]["fields"]["grillSetTemp"] == 225


def test_history_since_filter(tmp_path, make_app):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = make_app(settings)

    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        app.state.collector_sync_collect_next()
        since = client.get("/api/history").json()["items"][1]["observed_at"]
        filtered = client.get(f"/api/history?since={since}").json()

    assert filtered["count"] == 1


def test_live_websocket_streams_current_and_next_state(tmp_path, make_app):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = make_app(settings)

    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        with client.websocket_connect("/api/live") as websocket:
            first = websocket.receive_json()
            app.state.collector_sync_collect_next()
            second = websocket.receive_json()

    assert first["fields"]["grillSetTemp"]["value"] == 225
    assert second["fields"]["grillSetTemp"]["value"] == 225
    assert second["fields"]["grillTemp"]["value"] != first["fields"]["grillTemp"]["value"]


def test_history_order_until_and_bucket_params(tmp_path, make_app):
    from datetime import UTC, datetime, timedelta

    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = make_app(settings)
    start = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    with app.state.store.connect() as conn:
        conn.executemany(
            "INSERT INTO readings (observed_at, received_at, source, fields_json,"
            " unknown_json, raw_json) VALUES (?, ?, 'fake', ?, '{}', '{}')",
            [
                (
                    (start + timedelta(seconds=10 * i)).isoformat(),
                    (start + timedelta(seconds=10 * i)).isoformat(),
                    f'{{"grillTemp": {i}}}',
                )
                for i in range(12)
            ],
        )

    with TestClient(app) as client:
        newest = client.get("/api/history?order=desc&limit=2").json()
        until = client.get(
            "/api/history?until=2026-06-02T12:00:20%2B00:00"
        ).json()
        bucketed = client.get("/api/history?bucket_seconds=60").json()
        bad = client.get("/api/history?order=sideways")

    assert [i["fields"]["grillTemp"] for i in newest["items"]] == [11, 10]
    assert until["count"] == 3
    assert bucketed["count"] == 2
    assert bucketed["items"][0]["fields"]["grillTemp"] == 2.5
    assert bad.status_code == 422
