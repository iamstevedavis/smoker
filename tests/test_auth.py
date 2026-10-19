import stat

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pitboss_bridge import auth
from pitboss_bridge.api import create_app
from pitboss_bridge.config import Settings

PASSWORD = "brisket"
PROTECTED = [
    "/api/state",
    "/api/history",
    "/api/cooks",
    "/api/session",
    "/docs",
    "/redoc",
    "/openapi.json",
]


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_FAILURE_DELAY_SECONDS", 0)


def _app(tmp_path, password=PASSWORD, **overrides):
    settings = Settings(
        source="fake",
        db_path=tmp_path / "pitboss.sqlite",
        poll_seconds=0,
        start_collector=False,
        ui_password=password,
        **overrides,
    )
    return create_app(settings)


def _login(client, password=PASSWORD):
    return client.post("/api/login", json={"password": password})


def _signer(tmp_path, password=PASSWORD):
    key = (tmp_path / auth.KEY_FILENAME).read_bytes()
    return auth.SessionSigner(key, password=password, max_age_seconds=3600)


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


def test_health_login_page_and_assets_stay_open(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200
        login = client.get("/login")
        assert login.status_code == 200
        assert "api/login" in login.text
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
    with TestClient(_app(tmp_path, session_days=7)) as client:
        response = _login(client)

    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie
    assert f"Max-Age={7 * 86400}" in cookie


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


def test_changing_password_ends_sessions(tmp_path):
    token = None
    with TestClient(_app(tmp_path)) as client:
        _login(client)
        token = client.cookies.get(auth.COOKIE_NAME)

    with TestClient(_app(tmp_path, password="new one")) as client:
        client.cookies.set(auth.COOKIE_NAME, token)
        assert client.get("/api/state").status_code == 401


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


def test_session_survives_restart(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        _login(client)
        token = client.cookies.get(auth.COOKIE_NAME)

    with TestClient(_app(tmp_path)) as client:
        client.cookies.set(auth.COOKIE_NAME, token)
        assert client.get("/api/state").status_code == 200


def test_signing_key_is_created_once_owner_only(tmp_path):
    path = tmp_path / "data" / auth.KEY_FILENAME
    key = auth.load_or_create_key(path)

    assert len(key) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.load_or_create_key(path) == key


def test_auth_off_needs_no_cookie_and_has_no_login(tmp_path):
    with TestClient(_app(tmp_path, password="")) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/session").json() == {"auth_enabled": False}
        assert client.get("/login").status_code == 404
        assert _login(client).status_code in (404, 405)
        with client.websocket_connect("/api/live"):
            pass

    assert not (tmp_path / auth.KEY_FILENAME).exists()


def test_session_reports_auth_enabled(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        _login(client)
        assert client.get("/api/session").json() == {"auth_enabled": True}


def test_settings_read_auth_env(monkeypatch):
    monkeypatch.setenv("PITBOSS_UI_PASSWORD", "secret")
    monkeypatch.setenv("PITBOSS_SESSION_DAYS", "2")
    settings = Settings.from_env()

    assert settings.ui_password == "secret"
    assert settings.session_days == 2


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
