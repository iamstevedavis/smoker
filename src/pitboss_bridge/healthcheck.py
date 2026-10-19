"""Container healthcheck: `python -m pitboss_bridge.healthcheck`.

Healthy when the source is delivering readings, or while the bridge is still
inside its startup grace period (so a slow first connect is not reported as a
failure). Exits 0 when healthy and 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any

DEFAULT_GRACE_SECONDS = 120.0


def is_healthy(
    health: dict[str, Any],
    now: datetime | None = None,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
) -> bool:
    if health.get("connected"):
        return True
    started_at = health.get("started_at")
    if started_at is None:
        return True  # collector has not started yet
    current = now or datetime.now(timezone.utc)
    return (current - datetime.fromisoformat(started_at)).total_seconds() < grace_seconds


def main() -> int:
    port = os.getenv("PITBOSS_PORT", "8000")
    grace = float(os.getenv("PITBOSS_HEALTH_GRACE_SECONDS", DEFAULT_GRACE_SECONDS))
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=4
        ) as response:
            health = json.load(response)
        return 0 if is_healthy(health, grace_seconds=grace) else 1
    except Exception as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
