import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from smokestack.config import Settings


@pytest.fixture
def app(tmp_path, make_app):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "smokestack.sqlite",
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


def test_only_the_control_routes_can_write_to_the_grill(app):
    # Cook sessions, bridge settings, probe alert targets and the login write
    # only to the bridge's own database. The control routes are the only ones
    # that reach the grill, and they are exactly the approved commands.
    write_paths = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.methods - {"GET", "HEAD"}:
            write_paths.add(route.path)

    assert write_paths
    assert all(
        path.startswith(
            (
                "/api/cooks",
                "/api/settings",
                "/api/probe-targets",
                "/api/setup",
                "/api/login",
                "/api/logout",
                "/api/auth",
                "/api/control/",
                "/api/control-log/",
            )
        )
        for path in write_paths
    )
    assert {path for path in write_paths if path.startswith("/api/control/")} == {
        "/api/control/turn-off",
        "/api/control/set-temperature",
        "/api/control/reboot",
    }


def test_dashboard_answers_head_requests(app):
    with TestClient(app) as client:
        assert client.head("/").status_code == 200
        assert client.head("/manifest.webmanifest").status_code == 200


def test_dashboard_controls_confirm_first_and_word_every_result(app):
    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text

    assert 'id="controls"' in html and 'id="control-confirm"' in html
    # Sent with the header the control routes require, after the dialog.
    assert '"X-Smokestack-Control": "1"' in js
    assert js.count("await confirmControl(") == 3
    for outcome in ("confirmed", "unconfirmed", "failed", "already_applied"):
        assert f'case "{outcome}":' in js
    assert "The grill was already off." in js
    assert "Controls are paused until the grill is reporting again." in html
