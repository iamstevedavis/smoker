"""Serve the web dashboard (a PWA) from the bridge."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"

# The shell must revalidate so a bridge upgrade reaches installed apps.
_NO_CACHE = {"Cache-Control": "no-cache"}
_NO_STORE = {"Cache-Control": "no-store"}
_READ_METHODS = ["GET", "HEAD"]


def mount_dashboard(app: FastAPI) -> None:
    """Serve the dashboard at `/`, leaving `/api/*` and `/docs` untouched."""

    @app.api_route("/", methods=_READ_METHODS, include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html", media_type="text/html", headers=_NO_CACHE
        )

    # The service worker and manifest live at the root so their scope is `/`.
    @app.api_route("/sw.js", methods=_READ_METHODS, include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "sw.js", media_type="text/javascript", headers=_NO_CACHE
        )

    @app.api_route(
        "/manifest.webmanifest", methods=_READ_METHODS, include_in_schema=False
    )
    async def manifest() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers=_NO_CACHE,
        )

    # Also the first-run page that creates the password.
    @app.api_route("/login", methods=_READ_METHODS, include_in_schema=False)
    async def login() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "login.html", media_type="text/html", headers=_NO_STORE
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
