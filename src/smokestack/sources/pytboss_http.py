"""Real Pit Boss WiFi source using pytboss over the grill's local HTTP RPC.

This source is lazy-imported so local fake-source development does not require
pytboss. It is read-only: it only ever calls ``get_state()``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from smokestack.sources._pytboss import poll_state

logger = logging.getLogger(__name__)

_NOT_SERVED_HINT = (
    "the grill may not serve local HTTP (ESP-IDF boards never do, and Mongoose "
    "boards need http.enable set); use PITBOSS_SOURCE=ble instead"
)


@dataclass
class PytbossHttpSource:
    """Poll a Pit Boss grill over the local network using pytboss."""

    model: str
    host: str
    password: str
    poll_seconds: float = 2.0
    name: str = "http"

    def validate(self) -> None:
        if not self.host:
            raise ValueError("host is required for PITBOSS_SOURCE=http")

    async def readings(self) -> AsyncIterator[dict[str, Any]]:
        self.validate()
        from pytboss import HttpConnection, PitBoss
        from pytboss.exceptions import NotConnectedError

        logger.info("Connecting to Pit Boss over HTTP at %s", self.host)
        boss = PitBoss(HttpConnection(self.host), self.model, password=self.password)
        polled = False
        try:
            await boss.start()
            logger.info("Connected to %s", self.host)
            while True:
                state = await poll_state(boss)
                polled = True
                yield dict(state)
                await asyncio.sleep(self.poll_seconds)
        except NotConnectedError as exc:
            if polled:
                raise
            raise NotConnectedError(
                f"Could not read the grill at {self.host}: {exc}; {_NOT_SERVED_HINT}"
            ) from exc
        finally:
            logger.info("Disconnecting from %s", self.host)
            await boss.stop()
