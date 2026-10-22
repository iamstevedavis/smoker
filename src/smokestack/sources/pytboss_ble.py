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
    check_password,
    poll_state,
    run_command,
)
from smokestack.sources.commands import (
    MCU_COMMANDS,
    Command,
    Job,
    JobQueue,
    PasswordCheck,
    queue_timeout_for,
)

logger = logging.getLogger(__name__)


@dataclass
class PytbossBleSource:
    """Poll a Pit Boss grill over BLE using pytboss."""

    model: str
    device_name: str
    password: str
    poll_seconds: float = 2.0
    # The BlueZ adapter to scan and connect with, e.g. "hci1". Empty uses the
    # system default.
    adapter: str = ""
    name: str = "ble"
    uptime_refresh_seconds: float = 20.0
    stats: PollStats = field(default_factory=PollStats)
    commands: JobQueue = field(default_factory=JobQueue)
    # Set when an MCU command was sent; the collector skips the blank frame.
    blank_frame_expected: bool = False
    # The passwords a pending password change may have left the grill with.
    pending_passwords: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.device_name:
            raise ValueError("device_name is required for PITBOSS_SOURCE=ble")

    def __post_init__(self) -> None:
        # The loop serves commands between polls, so the wait for one has to
        # cover a poll interval.
        self.commands.timeout = queue_timeout_for(self.poll_seconds)

    def accepted_setpoints(self, fahrenheit: bool) -> list[int]:
        return accepted_setpoints(self.model, fahrenheit)

    async def execute(self, command: Command) -> None:
        """Send `command` between polls, on the polling loop's connection."""
        await self.commands.submit(command)

    async def check_password(self, password: str) -> bool:
        """Whether the grill accepts `password`, on the polling loop's connection."""
        accepted = await self.commands.submit(PasswordCheck(password))
        if accepted:
            self.password = password
        return bool(accepted)

    async def readings(self) -> AsyncIterator[dict[str, Any]]:
        self.validate()
        from pytboss import BleConnection, PitBoss

        from smokestack.runtime import DEFAULT_SYSTEM_BUS, system_bus_reachable

        # Without this, bleak fails with a bare "[Errno 2] No such file or
        # directory" when PITBOSS_SOURCE=ble but the host's D-Bus isn't there.
        if not system_bus_reachable():
            raise RuntimeError(
                "Can't reach the host's Bluetooth: no system D-Bus socket "
                f"at {DEFAULT_SYSTEM_BUS}. Bluetooth needs a Linux host with "
                "bluetoothd running; in Docker, start docker-compose.yml, "
                "which mounts /run/dbus."
            )
        device = await self._find_device()
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
                yield dict(await self._poll(boss, refresher))
                await self.commands.serve(
                    lambda job: self._send(boss, job), self.poll_seconds
                )
        finally:
            self.commands.close()
            logger.info("Disconnecting from %s", self.device_name)
            await boss.stop()

    async def _find_device(self) -> Any:
        """Scan for the grill, on `adapter` when one is set.

        On BlueZ the found device carries its adapter's D-Bus path, so
        BleConnection connects through the same adapter.
        """
        from bleak import BleakScanner
        from bleak.exc import BleakError

        kwargs = {"adapter": self.adapter} if self.adapter else {}
        logger.info(
            "Scanning for Pit Boss BLE device %r on %s",
            self.device_name,
            self.adapter or "the default adapter",
        )
        try:
            return await BleakScanner.find_device_by_filter(
                lambda d, ad: d.name == self.device_name
                or ad.local_name == self.device_name,
                **kwargs,
            )
        except BleakError as exc:
            if not self.adapter:
                raise
            raise RuntimeError(
                f"Can't scan on Bluetooth adapter {self.adapter!r} "
                f"(SMOKESTACK_BLE_ADAPTER): {exc}. "
                "List the adapters with `bluetoothctl list`."
            ) from exc

    async def _poll(self, boss: Any, refresher: UptimeRefresher) -> Any:
        """One reading, with a pending password change as a fallback.

        While a password change is pending the grill may hold either
        password, so a rejected one is retried with the others: the bridge
        reconnects whichever one the grill ended up with.
        """
        from pytboss.exceptions import Unauthorized

        try:
            return await poll_state(boss, self.stats, refresher)
        except Unauthorized:
            for candidate in self.pending_passwords:
                if await check_password(boss, candidate):
                    self.password = candidate
                    return await poll_state(boss, self.stats, refresher)
            raise

    async def _send(self, boss: Any, job: Job) -> Any:
        if isinstance(job, PasswordCheck):
            return await check_password(boss, job.password)
        if job.name in MCU_COMMANDS:
            self.blank_frame_expected = True
        await run_command(boss, job)
