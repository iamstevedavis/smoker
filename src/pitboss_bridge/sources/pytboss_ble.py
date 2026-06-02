"""Real Pit Boss BLE source using pytboss.

This source is lazy-imported so local fake-source development does not require
Bluetooth dependencies.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


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

        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: d.name == self.device_name
            or ad.local_name == self.device_name
        )
        if device is None:
            raise RuntimeError(f"Pit Boss BLE device not found: {self.device_name}")

        boss = PitBoss(BleConnection(device), self.model, password=self.password)
        await boss.start()
        try:
            while True:
                state = await boss.get_state()
                yield dict(state)
                await asyncio.sleep(self.poll_seconds)
        finally:
            await boss.stop()
