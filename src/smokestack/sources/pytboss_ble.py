"""Real Pit Boss BLE source using pytboss.

This source is lazy-imported so local fake-source development does not require
Bluetooth dependencies.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from smokestack.sources._pytboss import (
    PollStats,
    UptimeRefresher,
    accepted_setpoints,
    poll_state,
    run_command,
)
from smokestack.sources.commands import MCU_COMMANDS, Command, CommandQueue

logger = logging.getLogger(__name__)


@dataclass
class PytbossBleSource:
    """Poll a Pit Boss grill over BLE using pytboss."""

    model: str
    device_name: str
    password: str
    poll_seconds: float = 2.0
    name: str = "ble"
    uptime_refresh_seconds: float = 20.0
    stats: PollStats = field(default_factory=PollStats)
    commands: CommandQueue = field(default_factory=CommandQueue)
    # Set when an MCU command was sent; the collector skips the blank frame.
    blank_frame_expected: bool = False

    def validate(self) -> None:
        if not self.device_name:
            raise ValueError("device_name is required for PITBOSS_SOURCE=ble")

    def accepted_setpoints(self, fahrenheit: bool) -> list[int]:
        return accepted_setpoints(self.model, fahrenheit)

    async def execute(self, command: Command) -> None:
        """Send `command` between polls, on the polling loop's connection."""
        await self.commands.submit(command)

    async def readings(self) -> AsyncIterator[dict[str, Any]]:
        self.validate()
        from bleak import BleakScanner
        from pytboss import BleConnection, PitBoss

        logger.info("Scanning for Pit Boss BLE device %r", self.device_name)
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: d.name == self.device_name
            or ad.local_name == self.device_name
        )
        if device is None:
            raise RuntimeError(f"Pit Boss BLE device not found: {self.device_name}")

        logger.info("Found %s (%s); connecting", self.device_name, device.address)
        boss = PitBoss(BleConnection(device), self.model, password=self.password)
        refresher = UptimeRefresher(self.uptime_refresh_seconds)
        try:
            # Inside the try so a failed connect still releases the BLE client.
            await boss.start()
            logger.info("Connected to %s", self.device_name)
            self.commands.open()
            while True:
                yield dict(await poll_state(boss, self.stats, refresher))
                await self.commands.serve(
                    lambda command: self._send(boss, command), self.poll_seconds
                )
        finally:
            self.commands.close()
            logger.info("Disconnecting from %s", self.device_name)
            await boss.stop()

    async def _send(self, boss: Any, command: Command) -> None:
        if command.name in MCU_COMMANDS:
            self.blank_frame_expected = True
        await run_command(boss, command)

