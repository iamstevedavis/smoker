"""Helpers shared by the pytboss-backed sources."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from smokestack.sources.commands import (
    Command,
    CommandUnauthorized,
    ConnectionLost,
    celsius_setpoints,
)

logger = logging.getLogger(__name__)


class PollStats:
    """Counters a source exposes so the collector can surface them in health."""

    def __init__(self) -> None:
        self.unauthorized_retries = 0


class UptimeRefresher:
    """Drops pytboss's cached uptime on a schedule.

    The next `get_state` then re-reads `Sys.GetInfo`, so the extrapolated
    uptime cannot drift into the wrong 10 second bucket between refreshes.
    """

    def __init__(
        self,
        interval_seconds: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._last_refresh = clock()

    def refresh_if_due(self, boss: Any) -> bool:
        now = self._clock()
        if now - self._last_refresh < self.interval_seconds:
            return False
        boss._last_uptime = None
        self._last_refresh = now
        logger.debug("Refreshing cached grill uptime")
        return True


async def poll_state(
    boss: Any,
    stats: PollStats | None = None,
    refresher: UptimeRefresher | None = None,
) -> Any:
    """Fetch grill state, retrying once if the grill rejects the password.

    pytboss derives the password key from the grill's uptime in 10 second
    buckets and extrapolates that uptime from a cached reading. Over BLE the
    grill intermittently answers Unauthorized even with the right password,
    most likely because slow round trips leave that estimate in the wrong
    bucket. A `refresher` proactively drops the cached uptime on a schedule;
    as a safety net, an Unauthorized also drops it (a pytboss private
    attribute) and forces a fresh read for the single retry.
    """
    from pytboss.exceptions import Unauthorized

    if refresher is not None:
        refresher.refresh_if_due(boss)
    try:
        return await boss.get_state()
    except Unauthorized:
        if stats is not None:
            stats.unauthorized_retries += 1
        logger.debug("Unauthorized retry #%s", getattr(stats, "unauthorized_retries", 1))
        logger.warning("Grill rejected the password; re-reading uptime and retrying")
        boss._last_uptime = None
        return await boss.get_state()


def accepted_setpoints(model: str, fahrenheit: bool) -> list[int]:
    """Setpoints `model`'s control board accepts, in the given unit.

    Read from pytboss's grill catalogue, the list `set_grill_temperature`
    snaps to, so no connection is needed.
    """
    from pytboss import grills

    spec = grills.get_grill(model)
    increments = list(spec.temp_increments or [])
    if fahrenheit:
        return increments
    return list(spec.celsius_temp_increments or []) or celsius_setpoints(increments)


async def run_command(boss: Any, command: Command) -> None:
    """Send one approved command through pytboss.

    Maps pytboss's errors onto the bridge's: `Unauthorized` (which also drops
    the cached uptime, as `poll_state` does, so a retry derives a fresh key)
    and lost connections. Nothing else is ever sent from here.
    """
    from pytboss.exceptions import NotConnectedError, Unauthorized

    lost: tuple[type[BaseException], ...] = (NotConnectedError, TimeoutError, ConnectionError)
    try:
        from bleak.exc import BleakError

        lost += (BleakError,)
    except ImportError:
        pass
    try:
        if command.name == "turn-off":
            await boss.turn_grill_off()
        elif command.name == "set-temperature":
            await boss.set_grill_temperature(command.temp)
        elif command.name == "reboot":
            await boss.reboot()
        elif command.name == "set-unit":
            await boss.set_temperature_unit(command.fahrenheit)
        elif command.name == "set-password":
            # What this returns is only a hint: a reply lost after the grill
            # applied the change looks like a failure but isn't, so the
            # bridge settles the change by authenticating, not by this.
            await boss.set_grill_password(command.password)
        else:  # Command rejects anything else; never fall through to a send
            raise ValueError(f"not a grill command: {command.name!r}")
    except Unauthorized as exc:
        boss._last_uptime = None
        raise CommandUnauthorized(str(exc) or "Unauthorized") from exc
    except lost as exc:
        raise ConnectionLost(str(exc) or type(exc).__name__) from exc


async def check_password(boss: Any, password: str) -> bool:
    """Whether the grill accepts `password`, on the current connection.

    The connection keeps the password that worked. One that does not work
    leaves the previous one in place, so a failed check never locks the
    bridge out.
    """
    from pytboss.exceptions import Unauthorized

    previous = boss._password
    boss._password = password.encode("utf-8")
    # A fresh uptime, so the key is derived from the grill's own clock.
    boss._last_uptime = None
    try:
        await boss.get_state()
    except Unauthorized:
        boss._password = previous
        boss._last_uptime = None
        return False
    except BaseException:
        # A timeout or a lost connection is not the password's fault, but
        # the key still goes back, so a failed check never leaves the
        # connection on a password the grill does not have.
        boss._password = previous
        boss._last_uptime = None
        raise
    return True
