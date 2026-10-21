"""Environment-backed settings for the bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any


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
    db_path: Path = Path("data/smokestack.sqlite")
    poll_seconds: float = 2.0
    stale_after_seconds: int = 30
    start_collector: bool = True
    log_level: str = "INFO"
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    mqtt_prefix: str = "smokestack"
    ntfy_url: str = ""
    ntfy_token: str = ""
    alert_drift_degrees: float = 25.0
    alert_drift_minutes: float = 10.0
    alert_stale_seconds: float = 60.0
    alert_cooldown_seconds: float = 0.0
    retention_days: float = 0
    enable_control: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=os.getenv("PITBOSS_SOURCE", "fake"),
            model=os.getenv("PITBOSS_MODEL", "PB850PS2"),
            device_name=os.getenv("PITBOSS_DEVICE_NAME", ""),
            host=os.getenv("PITBOSS_HOST", ""),
            password=os.getenv("PITBOSS_PASSWORD", ""),
            db_path=Path(os.getenv("SMOKESTACK_DB", "data/smokestack.sqlite")),
            poll_seconds=float(os.getenv("SMOKESTACK_POLL_SECONDS", "2")),
            stale_after_seconds=int(os.getenv("SMOKESTACK_STALE_AFTER_SECONDS", "30")),
            start_collector=_bool_env("SMOKESTACK_START_COLLECTOR", True),
            log_level=os.getenv("SMOKESTACK_LOG_LEVEL", "INFO").upper(),
            mqtt_host=os.getenv("SMOKESTACK_MQTT_HOST", ""),
            mqtt_port=int(os.getenv("SMOKESTACK_MQTT_PORT", "1883")),
            mqtt_user=os.getenv("SMOKESTACK_MQTT_USER", ""),
            mqtt_password=os.getenv("SMOKESTACK_MQTT_PASSWORD", ""),
            mqtt_prefix=os.getenv("SMOKESTACK_MQTT_PREFIX", "smokestack"),
            ntfy_url=os.getenv("SMOKESTACK_NTFY_URL", ""),
            ntfy_token=os.getenv("SMOKESTACK_NTFY_TOKEN", ""),
            alert_drift_degrees=float(os.getenv("SMOKESTACK_ALERT_DRIFT_DEGREES", "25")),
            alert_drift_minutes=float(os.getenv("SMOKESTACK_ALERT_DRIFT_MINUTES", "10")),
            alert_stale_seconds=float(os.getenv("SMOKESTACK_ALERT_STALE_SECONDS", "60")),
            alert_cooldown_seconds=float(
                os.getenv("SMOKESTACK_ALERT_COOLDOWN_SECONDS", "0")
            ),
            retention_days=float(os.getenv("SMOKESTACK_RETENTION_DAYS", "0")),
            enable_control=_bool_env("SMOKESTACK_ENABLE_CONTROL", False),
        )

    def with_overrides(self, overrides: dict[str, Any]) -> "Settings":
        """Return these settings with UI-saved values layered on top."""
        return replace(self, **overrides)


# Fields the dashboard may change, by group, in display order. Everything
# else (DB path, port, collector switch) is env only. The dashboard login lives
# in its own table; see auth.py.
EDITABLE_GROUPS: dict[str, tuple[str, ...]] = {
    "grill": (
        "source",
        "model",
        "host",
        "device_name",
        "password",
        "poll_seconds",
        "stale_after_seconds",
    ),
    "mqtt": ("mqtt_host", "mqtt_port", "mqtt_user", "mqtt_password", "mqtt_prefix"),
    "alerts": (
        "ntfy_url",
        "ntfy_token",
        "alert_drift_degrees",
        "alert_drift_minutes",
        "alert_stale_seconds",
        "alert_cooldown_seconds",
    ),
    "data": ("retention_days",),
    "control": ("enable_control",),
    "logging": ("log_level",),
}
EDITABLE_FIELDS = frozenset(name for group in EDITABLE_GROUPS.values() for name in group)
# Never sent back to the browser; the API only says whether they are set.
SECRET_FIELDS = frozenset({"password", "mqtt_password", "ntfy_token"})
ENV_ONLY = (
    "SMOKESTACK_DB",
    "SMOKESTACK_PORT",
    "SMOKESTACK_START_COLLECTOR",
)
_DEFAULTS = {f.name: f.default for f in fields(Settings)}


# Settings that describe the grill keep the PITBOSS_ prefix; the rest
# configure Smokestack itself.
GRILL_FIELDS = frozenset({"source", "model", "device_name", "host", "password"})


def env_name(field_name: str) -> str:
    prefix = "PITBOSS" if field_name in GRILL_FIELDS else "SMOKESTACK"
    return f"{prefix}_{field_name.upper()}"


def base_origin(settings: Settings, field_name: str) -> str:
    """Where a value without a UI override came from: `env` or `default`."""
    from_env = bool(os.getenv(env_name(field_name)))
    if from_env or getattr(settings, field_name) != _DEFAULTS[field_name]:
        return "env"
    return "default"
