"""State normalization for Pit Boss readings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

KNOWN_STATE_FIELDS = frozenset(
    {
        "grillTemp",
        "grillSetTemp",
        "smokerActTemp",
        "isFahrenheit",
        "p1Temp",
        "p1Target",
        "p2Temp",
        "p2Target",
        "p3Temp",
        "p4Temp",
        "moduleIsOn",
        "fanState",
        "hotState",
        "motorState",
        "primeState",
        "lightState",
        "err1",
        "err2",
        "err3",
        "highTempErr",
        "fanErr",
        "hotErr",
        "motorErr",
        "noPellets",
        "erL",
        "recipeStep",
        "recipeTime",
    }
)


DEFAULT_MODEL = "PB850PS2"

# Per-model fields that are always empty or meaningless. They are kept in `raw`
# but left out of `fields` (and `unknown`). See docs/fields.md.
UNUSABLE_FIELDS: dict[str, frozenset[str]] = {
    "PB850PS2": frozenset({"smokerActTemp", "p3Temp", "p4Temp"}),
}

# Per-model firmware defaults that mean "not set"; a field holding exactly this
# value is treated like an unusable field.
UNSET_DEFAULTS: dict[str, dict[str, Any]] = {
    "PB850PS2": {"p1Target": 50},
}

# Flags that count as a problem for aggregate "something is wrong" consumers.
ERROR_FLAGS_FOR_PROBLEM = (
    "err1",
    "err2",
    "err3",
    "highTempErr",
    "fanErr",
    "hotErr",
    "motorErr",
    "noPellets",
    "erL",
)


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class FieldReading:
    """One normalized field value with freshness metadata."""

    value: Any
    observed_at: datetime

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or utc_now()
        return max((current - self.observed_at).total_seconds(), 0.0)

    def to_api_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "value": self.value,
            "observed_at": _iso(self.observed_at),
            "age_seconds": self.age_seconds(now),
        }


@dataclass(frozen=True)
class StateSnapshot:
    """Normalized grill state from one source update."""

    source: str
    observed_at: datetime
    received_at: datetime
    fields: dict[str, FieldReading]
    unknown: dict[str, Any]
    raw: dict[str, Any]

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or utc_now()
        return max((current - self.observed_at).total_seconds(), 0.0)

    def is_stale(self, now: datetime | None = None, stale_after_seconds: int = 30) -> bool:
        return self.age_seconds(now) > stale_after_seconds

    def values_dict(self) -> dict[str, Any]:
        return {name: reading.value for name, reading in self.fields.items()}

    def to_api_dict(
        self,
        now: datetime | None = None,
        stale_after_seconds: int = 30,
    ) -> dict[str, Any]:
        current = now or utc_now()
        return {
            "source": self.source,
            "observed_at": _iso(self.observed_at),
            "received_at": _iso(self.received_at),
            "age_seconds": self.age_seconds(current),
            "stale": self.is_stale(current, stale_after_seconds),
            "fields": {
                name: reading.to_api_dict(current)
                for name, reading in sorted(self.fields.items())
            },
            "unknown": self.unknown,
            "raw": self.raw,
        }


def normalize_state(
    raw: dict[str, Any],
    source: str,
    observed_at: datetime | None = None,
    received_at: datetime | None = None,
    model: str = DEFAULT_MODEL,
) -> StateSnapshot:
    """Normalize a raw source payload into timestamped field readings."""
    observed = observed_at or utc_now()
    received = received_at or utc_now()
    unusable = UNUSABLE_FIELDS.get(model, frozenset())
    unset = UNSET_DEFAULTS.get(model, {})
    fields = {
        name: FieldReading(value=value, observed_at=observed)
        for name, value in raw.items()
        if name in KNOWN_STATE_FIELDS
        and name not in unusable
        and not (name in unset and value == unset[name])
    }
    unknown = {
        name: value
        for name, value in raw.items()
        if name not in KNOWN_STATE_FIELDS
    }
    return StateSnapshot(
        source=source,
        observed_at=observed,
        received_at=received,
        fields=fields,
        unknown=unknown,
        raw=dict(raw),
    )
