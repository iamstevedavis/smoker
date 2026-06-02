from fastapi.testclient import TestClient

from pitboss_bridge.api import create_app
from pitboss_bridge.config import Settings


def test_health_state_and_history_endpoints(tmp_path):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = create_app(settings)

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


def test_history_since_filter(tmp_path):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        app.state.collector_sync_collect_next()
        since = client.get("/api/history").json()["items"][1]["observed_at"]
        filtered = client.get(f"/api/history?since={since}").json()

    assert filtered["count"] == 1
