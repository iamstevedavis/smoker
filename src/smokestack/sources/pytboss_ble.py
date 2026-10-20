"""Real Pit Boss BLE source using pytboss.

This source is lazy-imported so local fake-source development does not require
Bluetooth dependencies.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from smokestack.sources._pytboss import PollStats, UptimeRefresher, poll_state

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

    def validate(self) -> None:
        if not self.device_name:
            raise ValueError("device_name is required for PITBOSS_SOURCE=ble")

    async def readings(self) -> AsyncIterator[dict[str, Any]]:
        self.validate()
        try:
            from bleak import BleakScanner
            from pytboss import BleConnection, PitBoss
        except ImportError as exc:
            raise RuntimeError(
                "BLE source requires installing the bridge with the 'ble' extra"
            ) from exc

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
            while True:
                yield dict(await poll_state(boss, self.stats, refresher))
                await asyncio.sleep(self.poll_seconds)
        finally:
            logger.info("Disconnecting from %s", self.device_name)
            await boss.stop()

