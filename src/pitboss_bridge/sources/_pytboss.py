"""Helpers shared by the pytboss-backed sources."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
