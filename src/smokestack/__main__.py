"""Command-line entrypoint for Smokestack.

`python -m smokestack` runs the server. `python -m smokestack
reset-password` forgets the dashboard password, so the next visit after a
restart shows the first-run setup again.
"""

from __future__ import annotations

import sys

import uvicorn

from smokestack.config import Settings
from smokestack.store import Database


def reset_password() -> None:
    store = Database(Settings.from_env().db_path)
    store.initialize()
    if store.delete_auth():
        print("Password cleared. Restart the bridge, then open the dashboard to set a new one.")
    else:
        print("No password was set.")


def main(argv: list[str] | None = None) -> None:
    """Run the development server, or a maintenance command."""
    args = sys.argv[1:] if argv is None else argv
    if args == ["reset-password"]:
        reset_password()
        return
    if args:
        sys.exit("usage: python -m smokestack [reset-password]")
    uvicorn.run("smokestack.api:create_app", factory=True, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
