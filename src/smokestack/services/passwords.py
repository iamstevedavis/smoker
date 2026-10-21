"""A grill password change that may be in flight.

The grill's password is the bridge's key to it: change it, and the bridge is
locked out until it learns the new one. So both passwords are kept until the
change is settled, and the bridge asks the grill which one it accepts rather
than trusting what the command returned: a reply lost after the grill applied
the change looks like a failure but isn't.

The pending row is written before anything is sent to the grill, so a bridge
that stops mid-change comes back knowing both passwords and can reconnect
without anyone touching the grill.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from smokestack.store import Database, PendingPassword

logger = logging.getLogger(__name__)

# Health events for a change that was settled outside a control command
# (the bridge restarted mid-change, say).
PASSWORD_EVENT = "grill_password_change"

CONFIRMED = "confirmed"
FAILED = "failed"
UNCONFIRMED = "unconfirmed"


def set_pending_passwords(source: Any, passwords: tuple[str, ...]) -> None:
    """Tell the source which passwords a pending change may have left behind.

    The source retries them when the grill rejects the password it is
    using, so the bridge reconnects whichever one the grill ended up with.
    """
    if hasattr(source, "pending_passwords"):
        source.pending_passwords = passwords


def source_connected(source: Any) -> bool:
    """Whether the source's connection to the grill is up."""
    queue = getattr(source, "commands", None)
    return bool(getattr(queue, "is_open", False))


class PasswordChange:
    """The pending grill password change, and how it is settled."""

    def __init__(
        self,
        store: Database,
        apply_password: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        # Called with the password the grill accepts, to make it the
        # bridge's own setting.
        self.apply_password = apply_password
        self.pending: PendingPassword | None = store.load_pending_password()
        self._lock = asyncio.Lock()

    @property
    def candidates(self) -> tuple[str, ...]:
        """The passwords to try, the new one first.

        The new one first: the change may have gone through even when the
        reply was lost.
        """
        if self.pending is None:
            return ()
        return (self.pending.new_password, self.pending.old_password)

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[None]:
        """Hold the password change lock.

        It is always taken before the settings lock, so a password that
        has just been confirmed cannot be discarded by a settings swap, and
        a settings save cannot slip past a reconcile.
        """
        async with self._lock:
            yield

    def begin(self, old_password: str, new_password: str) -> None:
        """Record the change, before anything is sent to the grill."""
        self.store.save_pending_password(old_password, new_password)
        self.pending = self.store.load_pending_password()

    def clear(self) -> None:
        """Forget the change in flight."""
        self.store.clear_pending_password()
        self.pending = None

    async def adopt(self, password: str) -> None:
        """Take `password` as the grill's password, and save it as the setting."""
        self.store.save_settings({"password": password})
        if self.apply_password is not None:
            await self.apply_password(password)
        self.clear()

    async def reconcile(self, source: Any) -> tuple[str, str]:
        """Settle the change by asking the grill which password it accepts."""
        async with self._lock:
            return await self.settle_by_asking(source)

    async def settle_by_asking(self, source: Any) -> tuple[str, str]:
        """Ask the grill which password it accepts, and settle the change.

        Returns the outcome (`confirmed`, `failed` or `unconfirmed`) and what
        to tell the user. The caller holds the lock (see `locked`).
        """
        pending = self.pending
        if pending is None:
            return (FAILED, "There is no password change to settle.")
        for candidate in (pending.new_password, pending.old_password):
            try:
                accepted = bool(await source.check_password(candidate))
            except Exception as exc:
                logger.warning("Could not check the grill's password: %s", exc)
                return (
                    UNCONFIRMED,
                    "The grill could not be reached, so the password change is "
                    "unconfirmed. Set the password in Settings, or reset it at "
                    "the grill.",
                )
            if not accepted:
                continue
            if candidate == pending.new_password:
                await self.adopt(candidate)
                return (
                    CONFIRMED,
                    "The grill now uses the new password, and the bridge "
                    "matches it.",
                )
            self.clear()
            return (FAILED, "The grill still uses the old password.")
        return (
            UNCONFIRMED,
            "The grill accepted neither the old nor the new password. Set the "
            "password in Settings, or reset it at the grill.",
        )

    async def watch(
        self,
        source_of: Callable[[], Any],
        interval: float = 30.0,
        retry_seconds: float = 2.0,
    ) -> None:
        """Settle a change that was in flight when the bridge stopped.

        Runs until the change is settled: the grill is asked which password
        it accepts, and the bridge keeps that one.
        """
        while self.pending is not None:
            source = source_of()
            if not source_connected(source):
                # The grill isn't there yet: ask again shortly.
                await asyncio.sleep(retry_seconds)
                continue
            try:
                outcome, detail = await self.reconcile(source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Could not settle the grill password change: %s", exc)
                outcome, detail = UNCONFIRMED, str(exc)
            if self.pending is None:
                logger.info("Grill password change settled: %s", outcome)
                self.record(outcome, detail)
                return
            await asyncio.sleep(interval)

    def record(self, outcome: str, detail: str) -> None:
        """Note a settled change in the health log (never a password)."""
        self.store.insert_health_event(
            PASSWORD_EVENT,
            f"grill password change: {outcome}",
            {"outcome": outcome, "detail": detail},
        )
