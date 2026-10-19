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


def _opt_float_env(name: str) -> float | None:
    raw = os.getenv(name)
    return float(raw) if raw else None


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
    ntfy_url: str = ""
    ntfy_token: str = ""
    alert_probe1_target: float | None = None
    alert_probe2_target: float | None = None
    alert_drift_degrees: float = 25.0
    alert_drift_minutes: float = 10.0
    alert_stale_seconds: float = 60.0
    alert_cooldown_seconds: float = 0.0
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
            ntfy_url=os.getenv("PITBOSS_NTFY_URL", ""),
            ntfy_token=os.getenv("PITBOSS_NTFY_TOKEN", ""),
            alert_probe1_target=_opt_float_env("PITBOSS_ALERT_PROBE1_TARGET"),
            alert_probe2_target=_opt_float_env("PITBOSS_ALERT_PROBE2_TARGET"),
            alert_drift_degrees=float(os.getenv("PITBOSS_ALERT_DRIFT_DEGREES", "25")),
            alert_drift_minutes=float(os.getenv("PITBOSS_ALERT_DRIFT_MINUTES", "10")),
            alert_stale_seconds=float(os.getenv("PITBOSS_ALERT_STALE_SECONDS", "60")),
            alert_cooldown_seconds=float(
                os.getenv("PITBOSS_ALERT_COOLDOWN_SECONDS", "0")
            ),
            retention_days=float(os.getenv("PITBOSS_RETENTION_DAYS", "0")),
        )
