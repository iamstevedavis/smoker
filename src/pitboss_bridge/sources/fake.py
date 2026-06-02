"""Fake grill source for local development and tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class FakeGrillSource:
    """Emit deterministic, realistic grill readings."""

    name = "fake"

    def __init__(self, interval_seconds: float = 2.0) -> None:
        self.interval_seconds = interval_seconds
        self._tick = 0

    async def readings(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            reading = self._reading()
            self._tick += 1
            yield reading
            if self.interval_seconds > 0:
                await asyncio.sleep(self.interval_seconds)

    def _reading(self) -> dict[str, Any]:
        grill_temp = 215 + (self._tick % 12)
        probe_1 = 140 + min(self._tick, 35)
        probe_2 = 128 + min(self._tick // 2, 30)
        return {
            "grillTemp": grill_temp,
            "grillSetTemp": 225,
            "smokerActTemp": grill_temp,
            "isFahrenheit": True,
            "p1Temp": probe_1,
            "p1Target": 165,
            "p2Temp": probe_2,
            "moduleIsOn": True,
            "fanState": self._tick % 3 != 0,
            "hotState": self._tick < 6,
            "motorState": self._tick % 5 == 0,
            "primeState": False,
            "lightState": False,
            "err1": False,
            "err2": False,
            "err3": False,
            "highTempErr": False,
            "fanErr": False,
            "hotErr": False,
            "motorErr": False,
            "noPellets": False,
            "erL": False,
            "recipeStep": 1,
            "recipeTime": max(0, 3600 - self._tick * 2),
        }
