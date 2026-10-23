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

from smokestack.sources._pytboss import (
    accepted_setpoints,
    check_password,
    poll_state,
    read_uptime,
    run_command,
)
from smokestack.sources.commands import (
    MCU_COMMANDS,
    Command,
    Job,
    JobQueue,
    RESTART_WATCH_SECONDS,
    PasswordCheck,
    RestartWatch,
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
    commands: JobQueue = field(default_factory=JobQueue)
    # Set when an MCU command was sent; the collector skips the blank frame.
    blank_frame_expected: bool = False
    # The passwords a pending password change may have left the grill with.
    pending_passwords: tuple[str, ...] = ()
    # Restarts of the WiFi module seen after a reboot, which over HTTP may
    # not drop the session.
    restart_watch: RestartWatch = field(default_factory=RestartWatch)

    def validate(self) -> None:
        if not self.host:
            raise ValueError("host is required for PITBOSS_SOURCE=http")

    def __post_init__(self) -> None:
        # The loop serves commands between polls, so the wait for one has to
        # cover a poll interval.
        self.commands.timeout = queue_timeout_for(self.poll_seconds)

    @property
    def module_restarts(self) -> int:
        return self.restart_watch.restarts

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
                state = await self._poll(boss)
                polled = True
                yield dict(state)
                await self.commands.serve(
                    lambda job: self._send(boss, job), self.poll_seconds
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

    async def _poll(self, boss: Any) -> Any:
        """One reading, with a pending password change as a fallback.

        While a password change is pending the grill may hold either
        password, so a rejected one is retried with the others: the bridge
        reconnects whichever one the grill ended up with.
        """
        from pytboss.exceptions import Unauthorized

        if self.restart_watch.armed:
            # Read before the state, so a module that restarted has its
            # fresh uptime in the key the poll is sent with.
            if self.restart_watch.observe(await read_uptime(boss)):
                logger.info("The WiFi module at %s restarted", self.host)
        try:
            return await poll_state(boss)
        except Unauthorized:
            for candidate in self.pending_passwords:
                if await check_password(boss, candidate):
                    self.password = candidate
                    return await poll_state(boss)
            raise

    async def _send(self, boss: Any, job: Job) -> Any:
        if isinstance(job, PasswordCheck):
            return await check_password(boss, job.password)
        if job.name in MCU_COMMANDS:
            self.blank_frame_expected = True
        if job.name == "reboot":
            self.restart_watch.arm(max(RESTART_WATCH_SECONDS, 4 * self.poll_seconds))
        await run_command(boss, job)
