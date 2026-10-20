"""Command-line entrypoint for Smokestack.

`python -m pitboss_bridge` runs the server. `python -m pitboss_bridge
reset-password` forgets the dashboard password, so the next visit after a
restart shows the first-run setup again.
"""

from __future__ import annotations

import os
import sys

import uvicorn

from pitboss_bridge.auth import LEGACY_PASSWORD_ENV
from pitboss_bridge.config import Settings
from pitboss_bridge.store import Database


def reset_password() -> None:
    store = Database(Settings.from_env().db_path)
    store.initialize()
    if store.delete_auth():
        print("Password cleared. Restart the bridge, then open the dashboard to set a new one.")
    else:
        print("No password was set.")
    if os.getenv(LEGACY_PASSWORD_ENV):
        print(
            f"{LEGACY_PASSWORD_ENV} is still set, so the bridge will import it again "
            "on restart; remove it from .env to get the setup screen."
        )


def main(argv: list[str] | None = None) -> None:
    """Run the development server, or a maintenance command."""
    args = sys.argv[1:] if argv is None else argv
    if args == ["reset-password"]:
        reset_password()
        return
    if args:
        sys.exit("usage: python -m pitboss_bridge [reset-password]")
    uvicorn.run("pitboss_bridge.api:create_app", factory=True, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
