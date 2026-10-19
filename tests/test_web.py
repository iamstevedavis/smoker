import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from pitboss_bridge.config import Settings


@pytest.fixture
def app(tmp_path, make_app):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )
    return make_app(settings)


def test_root_serves_dashboard_html(app):
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Smokestack</title>" in response.text
    assert 'rel="manifest"' in response.text
    assert "static/app.js" in response.text


def test_static_assets_are_served(app):
    with TestClient(app) as client:
        js = client.get("/static/app.js")
        css = client.get("/static/app.css")
        sw = client.get("/sw.js")

    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "api/live" in js.text
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert sw.status_code == 200
    assert "javascript" in sw.headers["content-type"]


def test_manifest_is_installable_and_icons_exist(app):
    with TestClient(app) as client:
        response = client.get("/manifest.webmanifest")
        manifest = json.loads(response.text)
        icons = {
            icon["src"]: client.get("/" + icon["src"]) for icon in manifest["icons"]
        }

    assert response.headers["content-type"].startswith("application/manifest+json")
    assert manifest["display"] == "standalone"
    assert manifest["start_url"]
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for src, icon_response in icons.items():
        assert icon_response.status_code == 200, src


def test_service_worker_never_caches_api(app):
    with TestClient(app) as client:
        sw = client.get("/sw.js").text

    shell = sw.split("const SHELL = [", 1)[1].split("];", 1)[0]
    assert "api" not in shell


def test_api_and_docs_still_work_with_dashboard(app):
    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        assert client.get("/api/state").json()["available"] is True
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_bridge_exposes_no_write_endpoints(app):
    # Only cook sessions, bridge settings and the login may write, all in the
    # bridge's own database; nothing reaches the grill.
    write_paths = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.methods - {"GET", "HEAD"}:
            write_paths.add(route.path)

    assert write_paths
    assert all(
        path.startswith(
            ("/api/cooks", "/api/settings", "/api/setup", "/api/login", "/api/logout", "/api/auth")
        )
        for path in write_paths
    )


def test_dashboard_answers_head_requests(app):
    with TestClient(app) as client:
        assert client.head("/").status_code == 200
        assert client.head("/manifest.webmanifest").status_code == 200
