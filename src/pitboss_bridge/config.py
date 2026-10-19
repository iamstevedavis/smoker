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
    host: str = ""
    password: str = ""
    db_path: Path = Path("data/pitboss.sqlite")
    poll_seconds: float = 2.0
    stale_after_seconds: int = 30
    start_collector: bool = True
    log_level: str = "INFO"
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_prefix: str = "pitboss"
    retention_days: float = 0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=os.getenv("PITBOSS_SOURCE", "fake"),
            model=os.getenv("PITBOSS_MODEL", "PB850PS2"),
            device_name=os.getenv("PITBOSS_DEVICE_NAME", ""),
            host=os.getenv("PITBOSS_HOST", ""),
            password=os.getenv("PITBOSS_PASSWORD", ""),
            db_path=Path(os.getenv("PITBOSS_DB", "data/pitboss.sqlite")),
            poll_seconds=float(os.getenv("PITBOSS_POLL_SECONDS", "2")),
            stale_after_seconds=int(os.getenv("PITBOSS_STALE_AFTER_SECONDS", "30")),
            start_collector=_bool_env("PITBOSS_START_COLLECTOR", True),
            log_level=os.getenv("PITBOSS_LOG_LEVEL", "INFO").upper(),
            mqtt_host=os.getenv("PITBOSS_MQTT_HOST", ""),
            mqtt_port=int(os.getenv("PITBOSS_MQTT_PORT", "1883")),
            mqtt_user=os.getenv("PITBOSS_MQTT_USER", ""),
            mqtt_password=os.getenv("PITBOSS_MQTT_PASSWORD", ""),
            mqtt_prefix=os.getenv("PITBOSS_MQTT_PREFIX", "pitboss"),
            retention_days=float(os.getenv("PITBOSS_RETENTION_DAYS", "0")),
        )
