"""Real Pit Boss BLE source using pytboss.

This source is lazy-imported so local fake-source development does not require
Bluetooth dependencies.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PytbossBleSource:
    """Poll a Pit Boss grill over BLE using pytboss."""

    model: str
    device_name: str
    password: str
    poll_seconds: float = 2.0
    name: str = "ble"

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
        try:
            # Inside the try so a failed connect still releases the BLE client.
            await boss.start()
            logger.info("Connected to %s", self.device_name)
            while True:
                yield dict(await poll_state(boss))
                await asyncio.sleep(self.poll_seconds)
        finally:
            logger.info("Disconnecting from %s", self.device_name)
            await boss.stop()


async def poll_state(boss: Any) -> Any:
    """Fetch grill state, retrying once if the grill rejects the password.

    pytboss derives the password key from the grill's uptime in 10 second
    buckets and extrapolates that uptime from a cached reading. Over BLE the
    grill intermittently answers Unauthorized even with the right password,
    most likely because slow round trips leave that estimate in the wrong
    bucket. Dropping the cached uptime (a pytboss private attribute) forces a
    fresh read for the retry.
    """
    from pytboss.exceptions import Unauthorized

    try:
        return await boss.get_state()
    except Unauthorized:
        logger.warning("Grill rejected the password; re-reading uptime and retrying")
        boss._last_uptime = None
        return await boss.get_state()
