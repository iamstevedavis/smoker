import logging
import stat

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import finish_setup
from pitboss_bridge import __main__ as cli
from pitboss_bridge import auth
from pitboss_bridge.api import create_app
from pitboss_bridge.config import Settings
from pitboss_bridge.store import Database

PASSWORD = "brisket!"
PROTECTED = [
    "/api/state",
    "/api/history",
    "/api/cooks",
    "/api/settings",
    "/docs",
    "/redoc",
    "/openapi.json",
]


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_FAILURE_DELAY_SECONDS", 0)
    # A developer's shell may still export the old variable.
    monkeypatch.delenv(auth.LEGACY_PASSWORD_ENV, raising=False)


def _settings(tmp_path) -> Settings:
    return Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
    )


def _app(tmp_path, password=PASSWORD, required=True):
    settings = _settings(tmp_path)
    if password is not None:
        finish_setup(settings, password=password, required=required)
    return create_app(settings)


def _login(client, password=PASSWORD):
    return client.post("/api/login", json={"password": password})


def _signer(tmp_path):
    key = (tmp_path / auth.KEY_FILENAME).read_bytes()
    stored = Database(tmp_path / "pitboss.sqlite").load_auth()["password_hash"]
    return auth.SessionSigner(key, stored, max_age_seconds=3600)


# First run ---------------------------------------------------------------


def test_first_run_blocks_everything_until_a_password_is_set(tmp_path):
    with TestClient(_app(tmp_path, password=None)) as client:
        assert client.get("/api/session").json() == {
            "setup_required": True,
            "auth_enabled": True,
            "authenticated": False,
        }
        for path in PROTECTED:
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.json() == {"detail": "setup required"}
        root = client.get("/", follow_redirects=False)
        assert (root.status_code, root.headers["location"]) == (303, "login")
        assert _login(client).status_code == 409


def test_setup_stores_a_hash_and_signs_in(tmp_path):
    app = _app(tmp_path, password=None)
    with TestClient(app) as client:
        response = client.post("/api/setup", json={"password": PASSWORD})
        assert response.status_code == 204
        assert auth.COOKIE_NAME in response.cookies
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/session").json() == {
            "setup_required": False,
            "auth_enabled": True,
            "authenticated": True,
        }

    stored = app.state.store.load_auth()
    assert stored["required"] is True
    assert PASSWORD not in stored["password_hash"]
    assert auth.verify_password(PASSWORD, stored["password_hash"])


def test_setup_runs_only_once(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = client.post("/api/setup", json={"password": "taking over"})
        assert response.status_code == 409
        assert _login(client, "taking over").status_code == 401


def test_setup_rejects_short_passwords(tmp_path):
    with TestClient(_app(tmp_path, password=None)) as client:
        assert client.post("/api/setup", json={"password": "short"}).status_code == 422
        assert client.get("/api/session").json()["setup_required"] is True


# Signing in ----------------------------------------------------------------


def test_protected_routes_need_a_session(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        for path in PROTECTED:
            assert client.get(path).status_code == 401, path
        assert _login(client).status_code == 204
        for path in PROTECTED:
            assert client.get(path).status_code == 200, path


def test_dashboard_redirects_to_login_when_signed_out(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "login"
        assert response.headers["cache-control"] == "no-store"
        _login(client)
        assert client.get("/").status_code == 200


def test_health_session_login_page_and_assets_stay_open(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/session").json()["authenticated"] is False
        login = client.get("/login")
        assert login.status_code == 200
        assert "api/login" in login.text
        assert "api/setup" in login.text
        assert login.headers["cache-control"] == "no-store"
        for path in ["/sw.js", "/manifest.webmanifest", "/static/app.css", "/static/icons/icon.svg"]:
            assert client.get(path).status_code == 200, path


def test_wrong_password_is_rejected(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = _login(client, "pulled pork")
        assert response.status_code == 401
        assert auth.COOKIE_NAME not in response.cookies
        assert client.get("/api/state").status_code == 401


def test_right_password_sets_session_cookie(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = _login(client)

    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie
    assert f"Max-Age={30 * 86400}" in cookie


def test_logout_clears_cookie(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        _login(client)
        assert client.get("/api/state").status_code == 200
        response = client.post("/api/logout")
        assert response.status_code == 204
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert client.get("/api/state").status_code == 401


def test_expired_and_tampered_cookies_are_rejected(tmp_path):
    app = _app(tmp_path)
    signer = _signer(tmp_path)
    expired = signer.issue(now=0)
    valid = signer.issue()
    expiry, mac = valid.split(".")
    tampered_expiry = f"{int(expiry) + 3600}.{mac}"
    tampered_mac = f"{expiry}.{'0' * len(mac)}"
    other_key = auth.SessionSigner(b"x" * 32, PASSWORD, 3600).issue()

    with TestClient(app) as client:
        for token in [expired, tampered_expiry, tampered_mac, other_key, "junk", ""]:
            client.cookies.set(auth.COOKIE_NAME, token)
            assert client.get("/api/state").status_code == 401, token
        client.cookies.set(auth.COOKIE_NAME, valid)
        assert client.get("/api/state").status_code == 200


def test_websocket_needs_a_session(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        app.state.collector_sync_collect_next()
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/api/live") as websocket:
                websocket.receive_json()
        assert refused.value.code == 1008

        _login(client)
        with client.websocket_connect("/api/live") as websocket:
            snapshot = websocket.receive_json()

    assert snapshot["fields"]["grillSetTemp"]["value"] == 225


def _next_reading(client, app):
    # Publish on the app's own loop so the socket's queue wakes up.
    client.portal.call(app.state.collector.collect_next)


def test_open_socket_closes_when_the_password_is_turned_on(tmp_path):
    app = _app(tmp_path, required=False)
    with TestClient(app) as client, TestClient(app) as admin:
        with client.websocket_connect("/api/live") as websocket:
            _next_reading(client, app)
            websocket.receive_json()

            response = admin.put("/api/auth", json={"required": True, "password": PASSWORD})
            assert response.status_code == 204
            _next_reading(client, app)
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        assert closed.value.code == 1008


def test_open_socket_closes_when_the_password_changes(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client, TestClient(app) as admin:
        _login(client)
        _login(admin)
        with client.websocket_connect("/api/live") as websocket, admin.websocket_connect(
            "/api/live"
        ) as admin_socket:
            response = admin.post(
                "/api/auth/password", json={"current": PASSWORD, "new": "new password"}
            )
            assert response.status_code == 204
            _next_reading(client, app)
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1008
            # The socket opened with the old cookie closes too; the browser
            # reconnects with the new one.
            with pytest.raises(WebSocketDisconnect):
                admin_socket.receive_json()
        with admin.websocket_connect("/api/live") as websocket:
            _next_reading(admin, app)
            assert websocket.receive_json()["available"] is True


def test_session_survives_restart(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        _login(client)
        token = client.cookies.get(auth.COOKIE_NAME)

    with TestClient(create_app(_settings(tmp_path))) as client:
        client.cookies.set(auth.COOKIE_NAME, token)
        assert client.get("/api/state").status_code == 200


def test_signing_key_is_created_once_owner_only(tmp_path):
    path = tmp_path / "data" / auth.KEY_FILENAME
    key = auth.load_or_create_key(path)

    assert len(key) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.load_or_create_key(path) == key


def test_password_hashes_are_salted_and_checked():
    first, second = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)

    assert first != second
    assert first.startswith("scrypt$")
    assert auth.verify_password(PASSWORD, first)
    assert not auth.verify_password("pulled pork", first)
    assert not auth.verify_password(PASSWORD, "garbage")
    assert not auth.verify_password(PASSWORD, "")


async def test_concurrent_failed_logins_are_serialized(tmp_path, monkeypatch):
    import asyncio
    import time

    import httpx

    monkeypatch.setattr(auth, "LOGIN_FAILURE_DELAY_SECONDS", 0.1)
    transport = httpx.ASGITransport(app=_app(tmp_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.monotonic()
        responses = await asyncio.gather(
            *(client.post("/api/login", json={"password": "guess"}) for _ in range(5))
        )
        elapsed = time.monotonic() - started

    assert all(response.status_code == 401 for response in responses)
    assert elapsed >= 0.5


# Turning the password off and on -------------------------------------------


def test_password_off_needs_no_cookie(tmp_path):
    with TestClient(_app(tmp_path, required=False)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/state").status_code == 200
        assert client.put("/api/settings", json={"poll_seconds": 3}).status_code == 200
        assert client.get("/api/session").json() == {
            "setup_required": False,
            "auth_enabled": False,
            "authenticated": False,
        }
        with client.websocket_connect("/api/live"):
            pass


def test_turning_the_password_off_takes_effect_at_once(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.put("/api/auth", json={"required": False}).status_code == 401
        _login(client)
        assert client.put("/api/auth", json={"required": False}).status_code == 204
        client.cookies.clear()
        assert client.get("/api/state").status_code == 200

    assert app.state.store.load_auth()["required"] is False


def test_turning_the_password_on_needs_it_and_signs_in(tmp_path):
    app = _app(tmp_path, required=False)
    with TestClient(app) as client:
        wrong = client.put("/api/auth", json={"required": True, "password": "guess"})
        missing = client.put("/api/auth", json={"required": True})
        assert (wrong.status_code, missing.status_code) == (403, 403)
        assert client.get("/api/session").json()["auth_enabled"] is False

        response = client.put("/api/auth", json={"required": True, "password": PASSWORD})
        assert response.status_code == 204
        assert client.get("/api/state").status_code == 200
        client.cookies.clear()
        assert client.get("/api/state").status_code == 401

    assert app.state.store.load_auth()["required"] is True


def test_turning_on_when_already_on_is_a_no_op(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        _login(client)
        response = client.put("/api/auth", json={"required": True})
        assert response.status_code == 204
        assert "set-cookie" not in response.headers


# Changing the password -----------------------------------------------------


def test_changing_the_password_signs_out_other_sessions(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as other, TestClient(app) as client:
        _login(other)
        _login(client)
        response = client.post(
            "/api/auth/password", json={"current": PASSWORD, "new": "new password"}
        )
        assert response.status_code == 204
        assert client.get("/api/state").status_code == 200
        assert other.get("/api/state").status_code == 401
        assert _login(other).status_code == 401
        assert _login(other, "new password").status_code == 204


def test_changing_the_password_needs_the_current_one(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        _login(client)
        wrong = client.post(
            "/api/auth/password", json={"current": "guess", "new": "new password"}
        )
        short = client.post("/api/auth/password", json={"current": PASSWORD, "new": "short"})
        assert (wrong.status_code, short.status_code) == (403, 422)
        assert _login(client).status_code == 204


def test_changing_the_password_needs_a_session(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/api/auth/password", json={"current": PASSWORD, "new": "new password"}
        )
        assert response.status_code == 401


# Lockout recovery ----------------------------------------------------------


def test_reset_password_command_returns_to_first_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PITBOSS_DB", str(tmp_path / "pitboss.sqlite"))
    finish_setup(_settings(tmp_path), password=PASSWORD)

    cli.main(["reset-password"])
    assert "Password cleared" in capsys.readouterr().out
    cli.main(["reset-password"])
    assert "No password was set" in capsys.readouterr().out

    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/api/session").json()["setup_required"] is True


def test_reset_password_warns_while_the_legacy_variable_is_set(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PITBOSS_DB", str(tmp_path / "pitboss.sqlite"))
    monkeypatch.setenv(auth.LEGACY_PASSWORD_ENV, "old password")
    finish_setup(_settings(tmp_path), password=PASSWORD)

    cli.main(["reset-password"])
    assert "remove it" in capsys.readouterr().out


def test_cli_rejects_unknown_commands():
    with pytest.raises(SystemExit):
        cli.main(["bogus"])


# Upgrading from PITBOSS_UI_PASSWORD ----------------------------------------


@pytest.fixture
def auth_log(caplog):
    # The bridge's logger doesn't propagate, so listen on it directly.
    logger = logging.getLogger("pitboss_bridge.auth")
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)


def test_legacy_env_password_is_imported_and_required(tmp_path, monkeypatch, auth_log):
    monkeypatch.setenv(auth.LEGACY_PASSWORD_ENV, "old password")
    app = _app(tmp_path, password=None)
    with TestClient(app) as client:
        assert client.get("/api/session").json()["setup_required"] is False
        assert client.get("/api/state").status_code == 401
        assert client.post("/api/setup", json={"password": "taking over"}).status_code == 409
        assert _login(client, "old password").status_code == 204

    stored = app.state.store.load_auth()
    assert stored["required"] is True
    assert "old password" not in stored["password_hash"]
    assert "Imported PITBOSS_UI_PASSWORD" in auth_log.text


def test_legacy_env_password_is_ignored_once_one_is_stored(tmp_path, monkeypatch, auth_log):
    monkeypatch.setenv(auth.LEGACY_PASSWORD_ENV, "old password")
    app = _app(tmp_path, required=False)
    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 200
        assert _login(client, "old password").status_code == 401
        assert _login(client).status_code == 204

    assert "PITBOSS_UI_PASSWORD is ignored" in auth_log.text
