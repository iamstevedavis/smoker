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


def test_dashboard_puts_the_controls_before_the_other_cards(app):
    # The controls are the actions a cook reaches for, so they sit under the
    # grill readout instead of after the probes, components and errors.
    with TestClient(app) as client:
        html = client.get("/").text

    order = [
        html.index('class="card hero"'),
        html.index('id="controls"'),
        html.index('class="probes"'),
        html.index('class="card indicators"'),
        html.index('class="card errors"'),
        html.index('class="card chart-card"'),
    ]
    assert order == sorted(order)


def test_dashboard_names_the_active_source_in_the_header(app):
    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text

    # The source sits with the connection status, not only in the footer.
    header = html.split("</header>", 1)[0]
    assert 'id="status"' in header and 'id="source-badge"' in header
    assert header.index('id="status"') < header.index('id="source-badge"')
    # The names people know, never the internal source ids.
    assert 'const SOURCE_LABELS = { http: "WiFi", ble: "Bluetooth", fake: "Demo" };' in js
    assert 'setText($("source-text"), label)' in js
    assert 'parts.push("Source: " + sourceLabel(source))' in js
    # A connected source is the live one, so Save & apply shows the new
    # source once it reconnects; until then the last reading's source stays.
    active = js.split("function activeSource()", 1)[1].split("\n  }\n", 1)[0]
    assert "health.connected" in active and "health.source" in active
    assert "state.source" in active


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
                "/api/alerts/",
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
        "/api/control/set-unit",
        "/api/control/set-password",
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
    # One for each grill command, plus pruning the command log.
    assert js.count("await confirmControl(") == 6
    for outcome in ("confirmed", "unconfirmed", "failed", "already_applied"):
        assert f'case "{outcome}":' in js
    assert "The grill was already off." in js
    assert "Controls are paused until the grill is reporting again." in html


def test_dashboard_disables_set_temperature_while_the_grill_is_off(app):
    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text

    assert 'id="control-temp-hint"' in html
    assert "Turn the grill on at the grill to set its temperature." in html
    # The input, both stepper buttons and Set, not the other controls.
    assert html.count("data-temp-control") == 4
    controls = js.split("function renderControls()", 1)[1].split("\n  }\n", 1)[0]
    assert "data-temp-control" in controls
    assert "moduleIsOn === false" in controls


def test_the_dashboard_can_change_the_grill_password(app):
    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text

    assert 'id="control-password"' in html and 'id="password-dialog"' in html
    # The dashboard password is asked for again: this command can lock the
    # bridge out.
    assert 'name="dashboard_password"' in html
    assert 'sendControl("set-password"' in js
    # The dialog says what changes, where the new password is saved, and what
    # the Pit Boss app needs.
    assert "The bridge saves it in its settings, so nothing needs changing in .env" in js
    assert "the Pit Boss app will need the new password too" in js
    # An unconfirmed change says the bridge keeps both passwords and retries.
    assert "The bridge can't tell whether the grill took the new password." in js
    assert "It keeps both the old and the new one and keeps trying them" in js
    # A password change the bridge hasn't settled says so, and that it retries.
    assert "The grill's password may have changed." in html
    assert "The bridge keeps trying both passwords" in html


def test_dashboard_command_log_prunes_only_after_counting_and_confirming(app):
    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text

    assert 'id="control-log"' in html and 'id="control-log-prune"' in html
    prune = js.split("async function pruneCommandLog()", 1)[1].split("\n  }\n", 1)[0]
    # A dry run first, then the dialog, then the real prune.
    assert prune.index("dry_run: true") < prune.index("await confirmControl(") < prune.index(
        "await pruneRequest(cutoff ?"
    )
    for outcome in ("confirmed", "unconfirmed", "failed", "already_applied", "rejected"):
        assert f"{outcome}: " in js.split("const OUTCOME_LABELS", 1)[1].split("};", 1)[0]


def test_chart_range_handler_ignores_other_segmented_buttons(app):
    with TestClient(app) as client:
        js = client.get("/static/app.js").text
        html = client.get("/").text

    # The °F/°C switch is a .seg group too; it must not be read as a range.
    assert 'data-unit-target="C"' in html
    assert 'querySelectorAll(".seg button[data-hours]")' in js
    assert 'querySelectorAll(".seg button")' not in js


def test_settings_panel_only_shows_fields_that_apply(app):
    with TestClient(app) as client:
        js = client.get("/static/app.js").text
        html = client.get("/").text

    # Source-specific fields: WiFi gets the address, Bluetooth the name and
    # adapter, Demo neither.
    assert "const SETTING_SOURCES = {" in js
    assert 'host: ["http"]' in js
    assert 'device_name: ["ble"]' in js
    assert 'ble_adapter: ["ble"]' in js
    # The integration switches back the new settings.
    assert 'mqtt_enabled: { label: "Publish to Home Assistant", type: "bool"' in js
    assert 'ntfy_enabled: { label: "Send alerts", type: "bool"' in js
    assert 'textContent: "Turn on the dashboard password first."' in js
    # Only non-default values are marked, and values are stepped, not spun.
    assert 'textContent: "Custom"' in js and 'textContent: "from .env"' in js
    assert "function settingStepper(input, meta, commit)" in js
    assert "function commitNumber(name, input, meta)" in js
    # Developer text is folded away at the bottom of System.
    assert 'textContent: "Set only in .env"' in js


def test_settings_panel_has_sections_and_saves_as_you_go(app):
    with TestClient(app) as client:
        js = client.get("/static/app.js").text
        html = client.get("/").text
        css = client.get("/static/app.css").text

    # The five sections, with their state on the right of the side list.
    for section in ("grill", "alerts", "home", "access", "system"):
        assert f'{{ id: "{section}",' in js
    for label in ("Grill", "Alerts", "Home Assistant", "Access & control", "System"):
        assert f'label: "{label}"' in js
    assert "settings-nav-item" in js
    assert 'className: "settings-nav-state"' in js
    assert "settings-nav" in html and "settings-sections" in html

    # Phone: the list first, then a section with Back.
    assert 'id="settings-back"' in html
    assert 'data-view="section"' in html
    assert 'settings-panel[data-view="list"] .settings-sections' in css
    assert 'settings-panel[data-view="section"] .settings-nav' in css

    # Save as you go: no Save & apply, and text/number save on blur.
    assert "Save &amp; apply" not in html
    assert "settings-save" not in html and "settings-save" not in js
    assert 'input.addEventListener("blur"' in js
    assert "saveField(field.name, value)" in js
    assert "Changes save when you leave a field. Changing the connection reconnects to the grill." in html
    assert "Send test alert" in js and 'apiFetch("api/alerts/test"' in js
    # Typing a topic URL enables the test before the blur that saves it.
    assert "function alertTestReady()" in js
    # Rules are sentences with the number fields inline, not field-name text.
    assert "function renderRule(parts)" in js
    assert "const field = settings.fields[part];" in js

    # The Profile dialog moved into Access & control.
    assert 'id="profile"' not in html
    assert "setupProfile" not in js
    assert "settings-password-form" in js
