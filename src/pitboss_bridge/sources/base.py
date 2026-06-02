"""Source interfaces for grill readings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class GrillSource(Protocol):
    """A stream of raw grill state dictionaries."""

    name: str

    def readings(self) -> AsyncIterator[dict[str, Any]]:
        """Yield raw grill states until cancelled or disconnected."""
        ...
