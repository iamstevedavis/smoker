"""Settings saved from the dashboard: validation, loading and the API view.

Values resolve as defaults, then the environment (`.env`), then these UI
overrides, which live in the SQLite DB in the data dir.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pitboss_bridge.config import (
    EDITABLE_GROUPS,
    ENV_ONLY,
    SECRET_FIELDS,
    Settings,
    base_origin,
    env_name,
)
from pitboss_bridge.runtime import available_sources, make_source

logger = logging.getLogger(__name__)

# Changing any of these can leave the chosen source without what it needs.
_SOURCE_FIELDS = frozenset({"source", "host", "device_name"})


class SettingsUpdate(BaseModel):
    """A partial update. Omitted fields are left alone; unknown or env-only
    fields are rejected. Only the probe targets accept null (no alert)."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(default=None)
    model: str = Field(default=None, min_length=1)
    host: str = Field(default=None)
    device_name: str = Field(default=None)
    password: str = Field(default=None)
    poll_seconds: float = Field(default=None, gt=0, le=3600)
    stale_after_seconds: int = Field(default=None, ge=1, le=86400)
    mqtt_host: str = Field(default=None)
    mqtt_port: int = Field(default=None, ge=1, le=65535)
    mqtt_user: str = Field(default=None)
    mqtt_password: str = Field(default=None)
    mqtt_prefix: str = Field(default=None, min_length=1, pattern=r"^[^#+]+$")
    ntfy_url: str = Field(default=None)
    ntfy_token: str = Field(default=None)
    alert_probe1_target: float | None = Field(default=None, gt=0, le=1000)
    alert_probe2_target: float | None = Field(default=None, gt=0, le=1000)
    alert_drift_degrees: float = Field(default=None, gt=0)
    alert_drift_minutes: float = Field(default=None, gt=0)
    alert_stale_seconds: float = Field(default=None, gt=0)
    alert_cooldown_seconds: float = Field(default=None, ge=0)
    retention_days: float = Field(default=None, ge=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default=None
    )

    @field_validator("source")
    @classmethod
    def _source_installed(cls, value: str) -> str:
        if value not in available_sources():
            raise ValueError(
                f"source must be one of {', '.join(available_sources())} in this image"
            )
        return value

    @field_validator("host", "device_name", "mqtt_host", "ntfy_url")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("ntfy_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("ntfy_url must start with http:// or https://")
        return value

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


def check_source(settings: Settings, changes: dict[str, Any]) -> None:
    """Raise ValueError if an update leaves the source missing a host/name."""
    if not _SOURCE_FIELDS & changes.keys():
        return
    source = make_source(settings)
    validate = getattr(source, "validate", None)
    if validate is not None:
        validate()


def clean_overrides(stored: dict[str, Any]) -> dict[str, Any]:
    """Drop saved values that no longer validate (say, a source this image
    lacks), so a bad row falls back to `.env` instead of stopping startup."""
    clean: dict[str, Any] = {}
    for name, value in stored.items():
        try:
            clean.update(SettingsUpdate.model_validate({name: value}).changes())
        except ValidationError as exc:
            logger.warning("Ignoring saved setting %s: %s", name, exc.errors()[0]["msg"])
    return clean


def settings_payload(
    base: Settings,
    overrides: dict[str, Any],
    effective: Settings,
) -> dict[str, Any]:
    """Every editable field with its value and origin. Secrets only say
    whether they are set."""
    groups = []
    for group, names in EDITABLE_GROUPS.items():
        items = []
        for name in names:
            value = getattr(effective, name)
            secret = name in SECRET_FIELDS
            items.append(
                {
                    "name": name,
                    "env": env_name(name),
                    "value": {"set": bool(value)} if secret else value,
                    "origin": "ui" if name in overrides else base_origin(base, name),
                    "secret": secret,
                }
            )
        groups.append({"name": group, "fields": items})
    read_only = effective.settings_read_only()
    return {
        "editable": read_only is None,
        "read_only_reason": read_only,
        "sources": list(available_sources()),
        "groups": groups,
        "env_only": list(ENV_ONLY),
    }
