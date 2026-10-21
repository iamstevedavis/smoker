"""Real Pit Boss WiFi source using pytboss over the grill's local HTTP RPC.

This source is lazy-imported so local fake-source development does not require
pytboss. Besides ``get_state()`` it only sends the commands in
`smokestack.sources.commands`, and only when grill control is enabled.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from smokestack.sources._pytboss import accepted_setpoints, poll_state, run_command
from smokestack.sources.commands import (
    MCU_COMMANDS,
    Command,
    CommandQueue,
    queue_timeout_for,
)

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
    commands: CommandQueue = field(default_factory=CommandQueue)
    # Set when an MCU command was sent; the collector skips the blank frame.
    blank_frame_expected: bool = False

    def validate(self) -> None:
        if not self.host:
            raise ValueError("host is required for PITBOSS_SOURCE=http")

    def __post_init__(self) -> None:
        # The loop serves commands between polls, so the wait for one has to
        # cover a poll interval.
        self.commands.timeout = queue_timeout_for(self.poll_seconds)

    def accepted_setpoints(self, fahrenheit: bool) -> list[int]:
        return accepted_setpoints(self.model, fahrenheit)

    async def execute(self, command: Command) -> None:
        """Send `command` between polls, on the polling loop's connection."""
        await self.commands.submit(command)

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
            self.commands.open()
            while True:
                state = await poll_state(boss)
                polled = True
                yield dict(state)
                await self.commands.serve(
                    lambda command: self._send(boss, command), self.poll_seconds
                )
        except NotConnectedError as exc:
            if polled:
                raise
            raise NotConnectedError(
                f"Could not read the grill at {self.host}: {exc}; {_NOT_SERVED_HINT}"
            ) from exc
        finally:
            self.commands.close()
            logger.info("Disconnecting from %s", self.host)
            await boss.stop()

    async def _send(self, boss: Any, command: Command) -> None:
        if command.name in MCU_COMMANDS:
            self.blank_frame_expected = True
        await run_command(boss, command)
