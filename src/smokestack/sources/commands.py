"""Grill commands and the queue a source's polling loop serves them from.

These are the only commands the bridge sends to the grill (see
docs/specs/2026-10-19-grill-control.md). There is deliberately no power-on.
Sources own their connection and BLE allows one at a time, so commands are
never sent from the API's task: they wait in a `CommandQueue` that the
source's polling loop serves between polls.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

CommandName = Literal["turn-off", "set-temperature", "reboot", "set-unit"]
COMMANDS: tuple[CommandName, ...] = ("turn-off", "set-temperature", "reboot", "set-unit")
# Sent to the control board (MCU). The firmware blanks its status cache when
# it forwards one, so the next state frame comes back empty.
MCU_COMMANDS = frozenset({"turn-off", "set-temperature", "set-unit"})

# Setpoints the PB850PS2's PBL board accepts, in Fahrenheit. pytboss sources
# read these from the grill catalogue instead.
PB850PS2_SETPOINTS = (
    180, 190, 200, 210, 220, 230, 240, 250, 260, 270,
    280, 290, 300, 325, 350, 375, 400, 450, 500,
)  # fmt: skip


@dataclass(frozen=True)
class Command:
    name: CommandName
    temp: int | None = None
    # set-unit only: True for Fahrenheit, False for Celsius.
    fahrenheit: bool | None = None

    def __post_init__(self) -> None:
        if self.name not in COMMANDS:
            raise ValueError(f"not a grill command: {self.name!r}")
        if (self.name == "set-temperature") != (self.temp is not None):
            raise ValueError("set-temperature, and only it, takes a temperature")
        if (self.name == "set-unit") != (self.fahrenheit is not None):
            raise ValueError("set-unit, and only it, takes a unit")

    @property
    def args(self) -> dict[str, Any]:
        if self.fahrenheit is not None:
            return {"unit": "F" if self.fahrenheit else "C"}
        return {} if self.temp is None else {"temp": self.temp}


class CommandError(Exception):
    """A command could not be completed."""


class CommandNotSent(CommandError):
    """The command never reached the grill (no connection to send it on)."""


class CommandUnauthorized(CommandError):
    """The grill rejected the command's password key."""


class ConnectionLost(CommandError):
    """The connection failed while sending; the grill may have got it."""


def celsius_setpoints(fahrenheit: tuple[int, ...] | list[int]) -> list[int]:
    """The Celsius setpoints for a board that converts a Fahrenheit list.

    Matches pytboss's `accepted_setpoints`: such boards floor, so 190 F is
    87 C to the grill rather than 88.
    """
    return [math.floor((value - 32) / 1.8) for value in fahrenheit]


# How long a command may wait for the polling loop to reach it, on top of the
# poll interval itself.
QUEUE_TIMEOUT_SECONDS = 15.0


def queue_timeout_for(poll_seconds: float) -> float:
    """How long a command may wait for a source that polls every
    `poll_seconds`.

    The polling loop serves the queue between polls, so a command waits for
    the next one. A bridge that polls every 30 seconds must not give up on a
    command after 15 and report it as never sent.
    """
    return poll_seconds + QUEUE_TIMEOUT_SECONDS


@dataclass
class _Pending:
    command: Command
    future: asyncio.Future[Any]
    started: bool = False


@dataclass
class CommandQueue:
    """Commands waiting for a source's polling loop.

    The loop opens the queue once connected, calls `serve` instead of
    sleeping between polls, and closes it when the connection ends, which
    fails anything still waiting with `CommandNotSent`.
    """

    _items: asyncio.Queue[_Pending] = field(default_factory=asyncio.Queue)
    _open: bool = False
    # How long a command may wait for the polling loop. The source sets this
    # from its poll interval, so a slow poll is not a dead connection.
    timeout: float = QUEUE_TIMEOUT_SECONDS

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        while not self._items.empty():
            pending = self._items.get_nowait()
            if not pending.future.done():
                pending.future.set_exception(
                    CommandNotSent("the grill connection closed first")
                )

    async def submit(
        self, command: Command, queue_timeout: float | None = None
    ) -> Any:
        """Queue `command` and wait for the polling loop to send it.

        Raises `CommandNotSent` when the source isn't connected or doesn't
        reach the command within `queue_timeout` (the queue's own timeout
        unless one is given); once sending has begun it waits for the outcome.
        """
        if not self._open:
            raise CommandNotSent("the grill isn't connected")
        pending = _Pending(command, asyncio.get_running_loop().create_future())
        self._items.put_nowait(pending)
        timeout = self.timeout if queue_timeout is None else queue_timeout
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout)
        except TimeoutError:
            if pending.started:
                return await pending.future
            pending.future.cancel()
            raise CommandNotSent("the grill connection didn't take the command") from None

    async def serve(self, run: Any, timeout: float) -> Command | None:
        """Wait up to `timeout` for a command and run it with `run(command)`.

        Returns the command run, if any, so the loop can poll straight away.
        The outcome, or the error, goes to the waiting `submit` and never
        into the polling loop.
        """
        if self._items.empty() and timeout <= 0:
            await asyncio.sleep(0)  # let a command that is on its way arrive
        if self._items.empty():
            if timeout <= 0:
                return None
            try:
                pending = await asyncio.wait_for(self._items.get(), timeout)
            except TimeoutError:
                return None
        else:
            pending = self._items.get_nowait()
        if pending.future.done():  # given up on while queued
            return None
        pending.started = True
        logger.info("Sending %s %s to the grill", pending.command.name, pending.command.args)
        try:
            result = await run(pending.command)
        except asyncio.CancelledError:
            if not pending.future.done():
                pending.future.set_exception(
                    ConnectionLost("the grill connection closed while sending")
                )
            raise
        except Exception as exc:
            if not pending.future.done():
                pending.future.set_exception(exc)
        else:
            if not pending.future.done():
                pending.future.set_result(result)
        return pending.command
