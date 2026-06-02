"""Environment-backed settings for the bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings."""

    source: str = "fake"
    model: str = "PB850PS2"
    device_name: str = ""
    password: str = ""
    db_path: Path = Path("data/pitboss.sqlite")
    poll_seconds: float = 2.0
    stale_after_seconds: int = 30
    start_collector: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=os.getenv("PITBOSS_SOURCE", "fake"),
            model=os.getenv("PITBOSS_MODEL", "PB850PS2"),
            device_name=os.getenv("PITBOSS_DEVICE_NAME", ""),
            password=os.getenv("PITBOSS_PASSWORD", ""),
            db_path=Path(os.getenv("PITBOSS_DB", "data/pitboss.sqlite")),
            poll_seconds=float(os.getenv("PITBOSS_POLL_SECONDS", "2")),
            stale_after_seconds=int(os.getenv("PITBOSS_STALE_AFTER_SECONDS", "30")),
            start_collector=_bool_env("PITBOSS_START_COLLECTOR", True),
        )
