"""Dashboard login: the stored password, signed session cookies and the gate.

On first run there is no password and every route except the login page sends
the user there to create one. The password is stored as a scrypt hash in the
database, and it can be switched off (and back on) from the settings panel. A
session cookie holds an expiry timestamp and an HMAC over it, so the bridge
keeps no session state.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

if TYPE_CHECKING:
    from pitboss_bridge.store import Database

COOKIE_NAME = "smoker_session"
KEY_FILENAME = "session.key"
# Slows down password guessing; tests set it to 0.
LOGIN_FAILURE_DELAY_SECONDS = 1.0
SESSION_MAX_AGE_SECONDS = 30 * 86400
PASSWORD_MIN_LENGTH = 8
# scrypt cost; the parameters are stored with each hash.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

# Reachable without a session: the container healthcheck, the login (and
# first-run) page and the files and endpoints it needs. Static files are app code, not grill data.
_EXEMPT_PATHS = {
    "/api/health",
    "/login",
    "/api/session",
    "/api/setup",
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

    def __init__(self, key: bytes, secret: str, max_age_seconds: float) -> None:
        # Mixing in the password hash means changing it logs everyone out.
        self._key = hmac.new(key, secret.encode(), hashlib.sha256).digest()
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


def hash_password(password: str) -> str:
    """Return `scrypt$n$r$p$salt$hash` for storing."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected)


class AuthState:
    """The login as stored in the database, and the signer derived from it.

    The gate reads this on every request, so changes from the settings panel
    take effect at once.
    """

    def __init__(self, store: Database, key: bytes) -> None:
        self._store = store
        self._key = key
        self._record = store.load_auth()
        self._signer = self._make_signer()

    def _make_signer(self) -> SessionSigner | None:
        if self._record is None:
            return None
        return SessionSigner(
            self._key, self._record["password_hash"], SESSION_MAX_AGE_SECONDS
        )

    @property
    def setup_required(self) -> bool:
        return self._record is None

    @property
    def required(self) -> bool:
        """Whether a session is needed (always true before setup)."""
        return self._record is None or self._record["required"]

    @property
    def max_age_seconds(self) -> int:
        return SESSION_MAX_AGE_SECONDS

    def check_password(self, password: str) -> bool:
        if self._record is None:
            return False
        return verify_password(password, self._record["password_hash"])

    def set_password(self, password: str) -> None:
        """Store a new password, which signs out every existing session."""
        required = True if self._record is None else self._record["required"]
        self._save(hash_password(password), required)

    def set_required(self, required: bool) -> None:
        if self._record is None:
            raise RuntimeError("no password set")
        self._save(self._record["password_hash"], required)

    def _save(self, password_hash: str, required: bool) -> None:
        self._store.save_auth(password_hash, required)
        self._record = {"password_hash": password_hash, "required": required}
        self._signer = self._make_signer()

    def issue(self) -> str:
        if self._signer is None:
            raise RuntimeError("no password set")
        return self._signer.issue()

    def session_valid(self, token: str | None) -> bool:
        return self._signer is not None and self._signer.verify(token)


def is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


class AuthGate:
    """ASGI middleware that requires a valid session cookie, unless the
    password has been switched off. Before first-run setup nothing gets
    through.

    The dashboard shell redirects to the login page, other HTTP routes answer
    401 and WebSockets are closed with 1008 (policy violation).
    """

    def __init__(self, app: ASGIApp, state: AuthState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or is_exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        if not self.state.required or self.state.session_valid(
            HTTPConnection(scope).cookies.get(COOKIE_NAME)
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocket(scope, receive, send).close(code=1008)
            return
        if scope["path"] == "/":
            response = RedirectResponse("login", status_code=303, headers=_NO_STORE)
        else:
            detail = "setup required" if self.state.setup_required else "not authenticated"
            response = JSONResponse(
                {"detail": detail}, status_code=401, headers=_NO_STORE
            )
        await response(scope, receive, send)
