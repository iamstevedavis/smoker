"""Fake grill source for local development and tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from smokestack.sources.commands import (
    MCU_COMMANDS,
    PB850PS2_SETPOINTS,
    Command,
    CommandUnauthorized,
    Job,
    JobQueue,
    PasswordCheck,
    celsius_setpoints,
    queue_timeout_for,
)
from smokestack.state import convert_temperature

# Failures `faults` can inject, for testing grill control without a fire.
FAULTS = frozenset(
    {
        "rpc_error",  # every command raises
        "ignore",  # commands are accepted but change nothing
        "blank_frame",  # the frame after an MCU command is blank, as on the grill
        "unauthorized_once",  # the next command is rejected as Unauthorized
        "reboot_never_returns",  # after a reboot the source stays unreachable
        "password_reply_lost",  # the password changed, but the reply never came
    }
)
AMBIENT_TEMP = 70


class FakeGrillSource:
    """Emit deterministic, realistic grill readings.

    It also takes the grill commands: turning off winds the temperature down,
    setting the temperature changes `grillSetTemp`, switching the unit flips
    `isFahrenheit` and converts every value, a reboot drops the stream for
    `reboot_seconds`, and changing the password changes the one the grill
    answers to.
    """

    name = "fake"

    def __init__(
        self,
        interval_seconds: float = 2.0,
        reboot_seconds: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        password: str = "",
    ) -> None:
        self.interval_seconds = interval_seconds
        self.reboot_seconds = reboot_seconds
        self._clock = clock
        self._tick = 0
        self.module_on = True
        self.set_temp = 225
        self.fahrenheit = True
        self.faults: set[str] = set()
        self.executed: list[Command] = []
        self.commands = JobQueue()
        # The loop serves commands between polls, so the wait for one has to
        # cover a poll interval.
        self.commands.timeout = queue_timeout_for(self.interval_seconds)
        self.blank_frame_expected = False
        # The password the grill answers to, and the ones a pending password
        # change may have left it with.
        self.password = password
        self.pending_passwords: tuple[str, ...] = ()
        self._off_temp: float | None = None
        self._blank_next = False
        self._drop_next = False
        self._down_until: float | None = None

    def accepted_setpoints(self, fahrenheit: bool) -> list[int]:
        if fahrenheit:
            return list(PB850PS2_SETPOINTS)
        return celsius_setpoints(PB850PS2_SETPOINTS)

    async def execute(self, command: Command) -> None:
        await self.commands.submit(command)

    async def check_password(self, password: str) -> bool:
        """Whether the grill accepts `password`."""
        return bool(await self.commands.submit(PasswordCheck(password)))

    async def readings(self) -> AsyncIterator[dict[str, Any]]:
        if self._down_until is not None:
            if self._clock() < self._down_until:
                raise ConnectionError("the WiFi module is restarting")
            self._down_until = None
        self.commands.open()
        try:
            while True:
                if self._drop_next:
                    self._drop_next = False
                    self._down_until = (
                        float("inf")
                        if "reboot_never_returns" in self.faults
                        else self._clock() + self.reboot_seconds
                    )
                    raise ConnectionError("the WiFi module is restarting")
                if self._blank_next:
                    self._blank_next = False
                    yield {}
                else:
                    reading = self._reading()
                    self._tick += 1
                    yield reading
                await self.commands.serve(self._run, self.interval_seconds)
        finally:
            self.commands.close()

    async def _run(self, job: Job) -> Any:
        if isinstance(job, PasswordCheck):
            return job.password == self.password
        command = job
        if command.name in MCU_COMMANDS:
            self.blank_frame_expected = True
            self._blank_next = "blank_frame" in self.faults
        if "unauthorized_once" in self.faults:
            self.faults.discard("unauthorized_once")
            raise CommandUnauthorized("Unauthorized")
        if "rpc_error" in self.faults:
            raise RuntimeError("RPC error -32000")
        self.executed.append(command)
        if "ignore" in self.faults:
            return
        if command.name == "turn-off" and self.module_on:
            self.module_on = False
            self._off_temp = self._grill_temp()
        elif command.name == "set-temperature" and command.temp is not None:
            self.set_temp = command.temp
        elif command.name == "reboot":
            self._drop_next = True
        elif command.name == "set-unit" and command.fahrenheit != self.fahrenheit:
            # The setpoint and cool-down temperature are kept in the grill's
            # unit, so they convert with it.
            before, after = self._unit(), "F" if command.fahrenheit else "C"
            # Snapped like any setpoint, so it stays one the board accepts
            # (it floors Celsius: 190 F is 87 C, not 88).
            converted = convert_temperature(self.set_temp, before, after)
            accepted = self.accepted_setpoints(command.fahrenheit)
            self.set_temp = min(accepted, key=lambda value: abs(value - converted))
            if self._off_temp is not None:
                self._off_temp = convert_temperature(self._off_temp, before, after)
            self.fahrenheit = bool(command.fahrenheit)
        elif command.name == "set-password" and command.password is not None:
            self.password = command.password
            if "password_reply_lost" in self.faults:
                # The grill took the change, but the reply never arrived.
                raise ConnectionError("the connection dropped")

    def _unit(self) -> str:
        return "F" if self.fahrenheit else "C"

    def _from_f(self, value: float) -> float:
        return round(convert_temperature(value, "F", self._unit()), 1)

    def _grill_temp(self) -> float:
        if not self.module_on and self._off_temp is not None:
            # Wind down towards ambient after the grill is turned off.
            step = 5 if self.fahrenheit else 5 * 5 / 9
            self._off_temp = max(self._from_f(AMBIENT_TEMP), self._off_temp - step)
            return self._off_temp
        return self.set_temp - 10 + (self._tick % 12)

    def _reading(self) -> dict[str, Any]:
        grill_temp = self._grill_temp()
        probe_1 = self._from_f(140 + min(self._tick, 35))
        probe_2 = self._from_f(128 + min(self._tick // 2, 30))
        on = self.module_on
        return {
            "grillTemp": grill_temp,
            "grillSetTemp": self.set_temp,
            "smokerActTemp": grill_temp,
            "isFahrenheit": self.fahrenheit,
            "p1Temp": probe_1,
            "p1Target": self._from_f(165),
            "p2Temp": probe_2,
            "moduleIsOn": on,
            # The fan keeps running through the shutdown cycle.
            "fanState": self._tick % 3 != 0 or not on,
            "hotState": on and self._tick < 6,
            "motorState": on and self._tick % 5 == 0,
            "primeState": False,
            "lightState": False,
            "err1": False,
            "err2": False,
            "err3": False,
            "highTempErr": False,
            "fanErr": False,
            "hotErr": False,
            "motorErr": False,
            "noPellets": False,
            "erL": False,
            "recipeStep": 1,
            "recipeTime": max(0, 3600 - self._tick * 2),
        }
