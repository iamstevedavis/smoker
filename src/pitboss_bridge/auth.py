"""Optional shared-password login: signed session cookies and the gate.

Auth is on only when `PITBOSS_UI_PASSWORD` is set. A session cookie holds an
expiry timestamp and an HMAC over it, so the bridge keeps no session state.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

COOKIE_NAME = "smoker_session"
KEY_FILENAME = "session.key"
# Slows down password guessing; tests set it to 0.
LOGIN_FAILURE_DELAY_SECONDS = 1.0

# Reachable without a session: the container healthcheck, the login page and
# the files it needs. Static files are app code, not grill data.
_EXEMPT_PATHS = {
    "/api/health",
    "/login",
    "/api/login",
    "/api/logout",
    "/sw.js",
    "/manifest.webmanifest",
}
_EXEMPT_PREFIXES = ("/static/",)
_NO_STORE = {"Cache-Control": "no-store"}


def load_or_create_key(path: Path) -> bytes:
    """Return the signing key at `path`, creating it (mode 0600) on first run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    key = secrets.token_bytes(32)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


class SessionSigner:
    """Issue and verify `<expiry>.<hmac>` session tokens."""

    def __init__(self, key: bytes, password: str, max_age_seconds: float) -> None:
        # Mixing in the password means changing it logs everyone out.
        self._key = hmac.new(key, password.encode(), hashlib.sha256).digest()
        self.max_age_seconds = int(max_age_seconds)

    def _mac(self, expiry: int) -> str:
        return hmac.new(self._key, str(expiry).encode(), hashlib.sha256).hexdigest()

    def issue(self, now: float | None = None) -> str:
        expiry = int(now if now is not None else time.time()) + self.max_age_seconds
        return f"{expiry}.{self._mac(expiry)}"

    def verify(self, token: str | None, now: float | None = None) -> bool:
        if not token:
            return False
        expiry_text, _, mac = token.partition(".")
        try:
            expiry = int(expiry_text)
        except ValueError:
            return False
        if not hmac.compare_digest(mac.encode(), self._mac(expiry).encode()):
            return False
        return expiry > (now if now is not None else time.time())


def password_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


def is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


class AuthGate:
    """ASGI middleware that requires a valid session cookie.

    The dashboard shell redirects to the login page, other HTTP routes answer
    401 and WebSockets are closed with 1008 (policy violation).
    """

    def __init__(self, app: ASGIApp, signer: SessionSigner) -> None:
        self.app = app
        self.signer = signer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or is_exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        if self.signer.verify(HTTPConnection(scope).cookies.get(COOKIE_NAME)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocket(scope, receive, send).close(code=1008)
            return
        if scope["path"] == "/":
            response = RedirectResponse("login", status_code=303, headers=_NO_STORE)
        else:
            response = JSONResponse(
                {"detail": "not authenticated"}, status_code=401, headers=_NO_STORE
            )
        await response(scope, receive, send)
