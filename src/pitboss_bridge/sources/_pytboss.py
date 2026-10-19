"""Helpers shared by the pytboss-backed sources."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

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
