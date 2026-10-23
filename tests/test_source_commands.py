import asyncio
from pathlib import Path

import pytest

from smokestack.services.collector import Collector, CollectorHealth, StateHub
from smokestack.sources import commands
from smokestack.sources.commands import (
    COMMANDS,
    Command,
    CommandNotSent,
    CommandUnauthorized,
    ConnectionLost,
    JobQueue,
    PasswordCheck,
    celsius_setpoints,
)
from smokestack.sources.commands import PB850PS2_SETPOINTS
from smokestack.sources.fake import FakeGrillSource
from smokestack.store import Database


# Commands ----------------------------------------------------------------


def test_only_the_approved_commands_exist():
    assert COMMANDS == (
        "turn-off",
        "set-temperature",
        "reboot",
        "set-unit",
        "set-password",
    )
    for name in ("turn-on", "power-on", "set-celsius", "set-fahrenheit", ""):
        with pytest.raises(ValueError):
            Command(name)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Command("set-temperature")
    with pytest.raises(ValueError):
        Command("turn-off", temp=225)
    with pytest.raises(ValueError):
        Command("set-unit")
    with pytest.raises(ValueError):
        Command("turn-off", fahrenheit=True)
    assert Command("set-temperature", 250).args == {"temp": 250}
    assert Command("set-unit", fahrenheit=False).args == {"unit": "C"}


def test_only_set_password_takes_a_password():
    assert Command("set-password", password="secret").args == {}
    with pytest.raises(ValueError):
        Command("set-password")
    with pytest.raises(ValueError):
        Command("turn-off", password="secret")


def test_no_power_on_anywhere_in_the_bridge():
    src = Path(__file__).parents[1] / "src" / "smokestack"
    for path in src.rglob("*.py"):
        text = path.read_text()
        for call in ("turn_grill_on", "FE0101FF", "turn_light_on", "set_probe_temperature"):
            assert call not in text, f"{call} in {path}"


def test_celsius_setpoints_floor_like_the_board():
    assert celsius_setpoints([180, 190, 500]) == [82, 87, 260]


def test_the_job_wait_covers_a_poll_interval():
    from smokestack.sources import commands

    assert commands.queue_timeout_for(30) >= 30
    assert commands.queue_timeout_for(0.5) > 0.5


def test_the_sources_wait_for_a_poll_interval():
    from smokestack.sources.pytboss_ble import PytbossBleSource
    from smokestack.sources.pytboss_http import PytbossHttpSource

    assert FakeGrillSource(interval_seconds=30).commands.timeout >= 30
    ble = PytbossBleSource(
        model="PB850PS2", device_name="PBL-1", password="", poll_seconds=30
    )
    http = PytbossHttpSource(
        model="PB850PS2", host="grill", password="", poll_seconds=30
    )
    assert ble.commands.timeout >= 30
    assert http.commands.timeout >= 30


# The queue ---------------------------------------------------------------


async def test_queue_refuses_commands_while_closed():
    with pytest.raises(CommandNotSent):
        await JobQueue().submit(Command("turn-off"))


async def test_a_command_waits_for_the_next_poll(monkeypatch):
    """A slow polling loop must not look like a dead connection.

    The loop serves the queue between polls, so the wait for a command has to
    cover a poll interval. Otherwise a bridge that polls every 30 seconds
    gives up on every command and reports it as never sent.
    """
    # The queue's own default, for a source that says nothing about how
    # often it polls.
    monkeypatch.setattr(commands, "QUEUE_TIMEOUT_SECONDS", 0.05)
    source = FakeGrillSource(interval_seconds=0.2)
    assert source.commands.timeout == pytest.approx(0.25)

    stream = source.readings()
    await anext(stream)  # the first frame, before the command
    frame = await _send(source, stream, Command("turn-off"))
    assert frame["moduleIsOn"] is False


def test_the_real_sources_wait_for_their_poll_interval():
    from smokestack.sources.pytboss_ble import PytbossBleSource
    from smokestack.sources.pytboss_http import PytbossHttpSource

    http = PytbossHttpSource(
        model="PB850PS2", host="grill", password="", poll_seconds=30
    )
    ble = PytbossBleSource(
        model="PB850PS2", device_name="PBL-1", password="", poll_seconds=30
    )
    assert http.commands.timeout >= 30
    assert ble.commands.timeout >= 30


async def test_queue_hands_commands_to_the_polling_loop():
    queue = JobQueue()
    queue.open()
    ran = []

    async def run(command):
        ran.append(command)
        return "ok"

    submitted = asyncio.create_task(queue.submit(Command("turn-off")))
    await asyncio.sleep(0)
    assert await queue.serve(run, timeout=1) == Command("turn-off")
    assert await submitted == "ok"
    assert ran == [Command("turn-off")]
    # Nothing waiting: serve just times out.
    assert await queue.serve(run, timeout=0) is None


async def test_queue_errors_go_to_the_caller_not_the_loop():
    queue = JobQueue()
    queue.open()

    async def run(command):
        raise CommandUnauthorized("Unauthorized")

    submitted = asyncio.create_task(queue.submit(Command("turn-off")))
    await asyncio.sleep(0)
    await queue.serve(run, timeout=1)
    with pytest.raises(CommandUnauthorized):
        await submitted


async def test_closing_the_queue_fails_waiting_commands():
    queue = JobQueue()
    queue.open()
    submitted = asyncio.create_task(queue.submit(Command("reboot")))
    await asyncio.sleep(0)
    queue.close()
    with pytest.raises(CommandNotSent):
        await submitted


async def test_a_command_the_loop_never_reaches_is_not_sent():
    queue = JobQueue()
    queue.open()
    with pytest.raises(CommandNotSent):
        await queue.submit(Command("turn-off"), queue_timeout=0.01)

    async def run(command):  # pragma: no cover - must not run
        raise AssertionError("sent after giving up")

    assert await queue.serve(run, timeout=0) is None


async def test_the_queue_carries_a_password_check_too():
    queue = JobQueue()
    queue.open()
    ran = []

    async def run(job):
        ran.append(job)
        return True

    submitted = asyncio.create_task(queue.submit(PasswordCheck("secret")))
    await asyncio.sleep(0)
    assert await queue.serve(run, timeout=1) == PasswordCheck("secret")
    assert await submitted is True
    assert ran == [PasswordCheck("secret")]


# pytboss sources -----------------------------------------------------------


class FakeBoss:
    """Records commands; only the three approved methods exist."""

    def __init__(self, error=None):
        self.error = error
        self.sent = []
        self._last_uptime = 12.0

    async def _send(self, name, *args):
        self.sent.append((name, *args))
        if self.error is not None:
            raise self.error

    async def turn_grill_off(self):
        await self._send("turn_grill_off")

    async def set_grill_temperature(self, temp):
        await self._send("set_grill_temperature", temp)

    async def reboot(self):
        await self._send("reboot")

    async def set_temperature_unit(self, fahrenheit):
        await self._send("set_temperature_unit", fahrenheit)


async def test_run_command_calls_only_the_matching_pytboss_method():
    pytest.importorskip("pytboss")
    from smokestack.sources._pytboss import run_command

    boss = FakeBoss()
    await run_command(boss, Command("turn-off"))
    await run_command(boss, Command("set-temperature", 250))
    await run_command(boss, Command("reboot"))
    await run_command(boss, Command("set-unit", fahrenheit=False))
    assert boss.sent == [
        ("turn_grill_off",),
        ("set_grill_temperature", 250),
        ("reboot",),
        ("set_temperature_unit", False),
    ]


async def test_run_command_maps_pytboss_errors():
    exceptions = pytest.importorskip("pytboss.exceptions")
    from smokestack.sources._pytboss import run_command

    boss = FakeBoss(exceptions.Unauthorized("Unauthorized", 401))
    with pytest.raises(CommandUnauthorized):
        await run_command(boss, Command("turn-off"))
    # The next key is derived from a fresh uptime, as for polls.
    assert boss._last_uptime is None

    for error in (exceptions.NotConnectedError("gone"), TimeoutError()):
        with pytest.raises(ConnectionLost):
            await run_command(FakeBoss(error), Command("reboot"))

    with pytest.raises(exceptions.RPCError):
        await run_command(FakeBoss(exceptions.RPCError("bad")), Command("turn-off"))


def test_pytboss_setpoints_come_from_the_grill_catalogue():
    pytest.importorskip("pytboss")
    from smokestack.sources._pytboss import accepted_setpoints
    from smokestack.sources.commands import PB850PS2_SETPOINTS

    assert accepted_setpoints("PB850PS2", True) == list(PB850PS2_SETPOINTS)
    assert accepted_setpoints("PB850PS2", False) == celsius_setpoints(
        PB850PS2_SETPOINTS
    )


async def test_http_source_sends_commands_between_polls(monkeypatch):
    pytboss = pytest.importorskip("pytboss")
    from smokestack.sources.pytboss_http import PytbossHttpSource

    events = []

    class Boss(FakeBoss):
        def __init__(self, connection, model, password=""):
            super().__init__()

        async def start(self):
            pass

        async def stop(self):
            events.append("stop")

        async def get_state(self):
            events.append("poll")
            return {"grillTemp": 225}

        async def _send(self, name, *args):
            events.append(name)

    monkeypatch.setattr(pytboss, "PitBoss", Boss)
    monkeypatch.setattr(pytboss, "HttpConnection", lambda host: host)
    source = PytbossHttpSource(model="PB850PS2", host="grill", password="", poll_seconds=5)
    stream = source.readings()

    await anext(stream)
    # The loop is now waiting out the poll interval; a command cuts it short.
    command = asyncio.create_task(source.execute(Command("turn-off")))
    await anext(stream)
    await command
    assert events == ["poll", "turn_grill_off", "poll"]
    assert source.blank_frame_expected is True
    await stream.aclose()
    with pytest.raises(CommandNotSent):
        await source.execute(Command("turn-off"))


async def test_ble_source_sends_commands_between_polls(monkeypatch):
    pytboss = pytest.importorskip("pytboss")
    bleak = pytest.importorskip("bleak")
    from smokestack.sources.pytboss_ble import PytbossBleSource

    events = []

    class Boss(FakeBoss):
        def __init__(self, connection, model, password=""):
            super().__init__()

        async def start(self):
            pass

        async def stop(self):
            pass

        async def get_state(self):
            events.append("poll")
            return {"grillTemp": 225}

        async def _send(self, name, *args):
            events.append((name, *args))

    async def find_device(_filter):
        return type("Device", (), {"address": "AA:BB"})()

    monkeypatch.setattr(pytboss, "PitBoss", Boss)
    monkeypatch.setattr(pytboss, "BleConnection", lambda device: device)
    monkeypatch.setattr(bleak.BleakScanner, "find_device_by_filter", find_device)
    source = PytbossBleSource(
        model="PB850PS2", device_name="PBL-1", password="", poll_seconds=5
    )
    stream = source.readings()

    await anext(stream)
    command = asyncio.create_task(source.execute(Command("set-temperature", 250)))
    await anext(stream)
    await command
    assert events == ["poll", ("set_grill_temperature", 250), "poll"]
    await stream.aclose()


# The fake source -----------------------------------------------------------


async def _send(source, stream, command):
    """Run `command` through the fake's polling loop; return the next frame."""
    task = asyncio.create_task(source.execute(command))
    frame = await anext(stream)
    await task
    return frame


async def test_fake_source_turns_off_and_winds_down():
    source = FakeGrillSource(interval_seconds=0)
    stream = source.readings()
    before = await anext(stream)

    after = await _send(source, stream, Command("turn-off"))
    later = await anext(stream)

    assert before["moduleIsOn"] is True
    assert after["moduleIsOn"] is False
    assert later["grillTemp"] < after["grillTemp"] < before["grillTemp"] + 12
    assert source.blank_frame_expected is True


async def test_fake_source_sets_the_temperature():
    source = FakeGrillSource(interval_seconds=0)
    stream = source.readings()
    await anext(stream)

    after = await _send(source, stream, Command("set-temperature", 250))

    assert after["grillSetTemp"] == 250
    assert source.executed == [Command("set-temperature", 250)]


async def test_fake_source_reboot_drops_the_stream_and_comes_back():
    now = [0.0]
    source = FakeGrillSource(interval_seconds=0, reboot_seconds=10, clock=lambda: now[0])
    stream = source.readings()
    await anext(stream)

    task = asyncio.create_task(source.execute(Command("reboot")))
    with pytest.raises(ConnectionError):
        await anext(stream)
    await task
    assert source.blank_frame_expected is False
    with pytest.raises(ConnectionError):
        await anext(source.readings())
    now[0] = 10
    assert (await anext(source.readings()))["moduleIsOn"] is True


async def test_fake_source_reboot_that_never_comes_back():
    now = [0.0]
    source = FakeGrillSource(interval_seconds=0, reboot_seconds=1, clock=lambda: now[0])
    source.faults.add("reboot_never_returns")
    stream = source.readings()
    await anext(stream)
    task = asyncio.create_task(source.execute(Command("reboot")))
    with pytest.raises(ConnectionError):
        await anext(stream)
    await task
    now[0] = 1e9
    with pytest.raises(ConnectionError):
        await anext(source.readings())


async def test_fake_source_reboot_can_keep_the_session():
    now = [100.0]
    source = FakeGrillSource(interval_seconds=0, clock=lambda: now[0])
    source.faults.add("reboot_keeps_session")
    stream = source.readings()
    await anext(stream)
    assert source.uptime() == 0
    now[0] = 200
    assert source.uptime() == 100

    await _send(source, stream, Command("reboot"))
    now[0] = 205

    # The stream carried on, and the uptime reset shows the restart.
    assert source.uptime() == 5
    assert source.module_restarts == 1
    assert source.restart_watch.armed is False


async def test_fake_source_counts_the_restart_after_a_dropped_session():
    now = [100.0]
    source = FakeGrillSource(interval_seconds=0, reboot_seconds=10, clock=lambda: now[0])
    stream = source.readings()
    await anext(stream)
    task = asyncio.create_task(source.execute(Command("reboot")))
    with pytest.raises(ConnectionError):
        await anext(stream)
    await task
    now[0] = 110
    await anext(source.readings())

    assert source.module_restarts == 1


# Restart watch -------------------------------------------------------------


def test_restart_watch_counts_an_uptime_shorter_than_the_reboot():
    now = [1000.0]
    watch = commands.RestartWatch(lambda: now[0])
    assert watch.observe(0) is False  # not armed

    watch.arm()
    now[0] = 1010
    # Still running: it has been up for longer than the 10 s since.
    assert watch.observe(5000) is False
    assert watch.observe(None) is False
    assert watch.armed
    # Started 4 s ago, after the reboot was sent.
    assert watch.observe(4) is True
    assert watch.restarts == 1
    assert watch.armed is False
    assert watch.observe(4) is False
    assert watch.restarts == 1


def test_restart_watch_stops_watching_after_a_while():
    now = [0.0]
    watch = commands.RestartWatch(lambda: now[0])
    watch.arm(60)
    now[0] = 61

    assert watch.armed is False
    assert watch.observe(1) is False
    assert watch.restarts == 0


@pytest.mark.parametrize(
    ("fault", "error"),
    [("rpc_error", RuntimeError), ("unauthorized_once", CommandUnauthorized)],
)
async def test_fake_source_failing_commands_change_nothing(fault, error):
    source = FakeGrillSource(interval_seconds=0)
    source.faults.add(fault)
    stream = source.readings()
    await anext(stream)

    with pytest.raises(error):
        await _send(source, stream, Command("turn-off"))
    assert source.module_on is True
    if fault == "unauthorized_once":
        await _send(source, stream, Command("turn-off"))
        assert source.module_on is False


async def test_fake_source_can_ignore_commands():
    source = FakeGrillSource(interval_seconds=0)
    source.faults.add("ignore")
    stream = source.readings()
    await anext(stream)

    after = await _send(source, stream, Command("set-temperature", 300))

    assert after["grillSetTemp"] == 225
    assert source.executed == [Command("set-temperature", 300)]


async def test_fake_source_changes_the_password():
    source = FakeGrillSource(interval_seconds=0, password="old")
    stream = source.readings()
    await anext(stream)

    await _send(source, stream, Command("set-password", password="new"))

    assert source.password == "new"
    assert source.executed == [Command("set-password", password="new")]


async def test_fake_source_password_change_whose_reply_was_lost():
    source = FakeGrillSource(interval_seconds=0, password="old")
    source.faults.add("password_reply_lost")
    stream = source.readings()
    await anext(stream)

    task = asyncio.create_task(source.execute(Command("set-password", password="new")))
    await anext(stream)
    with pytest.raises(ConnectionError):
        await task

    # The change took, even though the reply never arrived.
    assert source.password == "new"


async def test_fake_source_answers_password_checks():
    source = FakeGrillSource(interval_seconds=0, password="old")
    stream = source.readings()
    await anext(stream)

    task = asyncio.create_task(source.check_password("old"))
    await anext(stream)
    assert await task is True

    task = asyncio.create_task(source.check_password("new"))
    await anext(stream)
    assert await task is False


# A pending password change -------------------------------------------------


class PasswordBoss:
    """A boss that only answers for the right password."""

    def __init__(self, password: str = "new") -> None:
        self.password = password
        self._password = b""
        self._last_uptime = 12.0

    async def get_state(self):
        if self._password.decode() != self.password:
            raise pytest.importorskip("pytboss.exceptions").Unauthorized(
                "Unauthorized", 401
            )
        return {"grillTemp": 225}


async def test_the_source_retries_the_pending_passwords_after_unauthorized():
    from smokestack.sources._pytboss import UptimeRefresher
    from smokestack.sources.pytboss_ble import PytbossBleSource

    source = PytbossBleSource(model="PB850PS2", device_name="PBL-1", password="old")
    source.pending_passwords = ("new", "old")

    state = await source._poll(PasswordBoss("new"), UptimeRefresher())

    assert state == {"grillTemp": 225}
    # The connection keeps the password that worked.
    assert source.password == "new"


async def test_the_source_gives_up_when_no_pending_password_works():
    exceptions = pytest.importorskip("pytboss.exceptions")
    from smokestack.sources._pytboss import UptimeRefresher
    from smokestack.sources.pytboss_ble import PytbossBleSource

    source = PytbossBleSource(model="PB850PS2", device_name="PBL-1", password="old")
    source.pending_passwords = ("new", "old")

    with pytest.raises(exceptions.Unauthorized):
        await source._poll(PasswordBoss("something else"), UptimeRefresher())

    assert source.password == "old"


# A password check ----------------------------------------------------------


class KeyBoss:
    """A boss that only answers with the right password, or not at all."""

    def __init__(self, password: str = "new", error: Exception | None = None) -> None:
        self.password = password
        self.error = error
        self._password = b"old"
        self._last_uptime = 12.0

    async def get_state(self):
        if self.error is not None:
            raise self.error
        if self._password.decode() != self.password:
            raise pytest.importorskip("pytboss.exceptions").Unauthorized(
                "Unauthorized", 401
            )
        return {"grillTemp": 225}


async def test_a_password_check_keeps_the_password_that_worked():
    from smokestack.sources._pytboss import check_password

    boss = KeyBoss("new")

    assert await check_password(boss, "new") is True
    assert boss._password == b"new"


async def test_a_rejected_password_puts_the_old_key_back():
    from smokestack.sources._pytboss import check_password

    boss = KeyBoss("new")

    assert await check_password(boss, "old") is False
    assert boss._password == b"old"


async def test_a_check_that_fails_for_another_reason_puts_the_old_key_back():
    """A timeout is not the password's fault, but the key still goes back,
    so a failed check never leaves the connection on a wrong password."""
    from smokestack.sources._pytboss import check_password

    boss = KeyBoss("new", error=TimeoutError("the answer did not come"))

    with pytest.raises(TimeoutError):
        await check_password(boss, "new")
    assert boss._password == b"old"


# The blank frame -------------------------------------------------------------


async def test_collector_skips_the_blank_frame_after_a_command(tmp_path):
    source = FakeGrillSource(interval_seconds=0)
    source.faults.add("blank_frame")
    db = Database(tmp_path / "smokestack.sqlite")
    db.initialize()
    hub = StateHub(stale_after_seconds=30)
    health = CollectorHealth(source="fake", stale_after_seconds=30)
    collector = Collector(source=source, store=db, hub=hub, health=health)
    first = await collector.collect_next()
    published = hub.subscribe()

    task = asyncio.create_task(source.execute(Command("turn-off")))
    assert await collector.collect_next() is None
    await task
    after = await collector.collect_next()

    assert after is not None and after.fields["moduleIsOn"].value is False
    assert hub.current is after
    assert published.qsize() == 1
    assert len(db.list_history()) == 2
    assert health.last_update_at == after.received_at != first.received_at


async def test_collector_keeps_blank_frames_nobody_expects(tmp_path):
    class Blank:
        name = "blank"

        async def readings(self):
            while True:
                yield {}

    db = Database(tmp_path / "smokestack.sqlite")
    db.initialize()
    collector = Collector(
        source=Blank(),
        store=db,
        hub=StateHub(stale_after_seconds=30),
        health=CollectorHealth(source="blank", stale_after_seconds=30),
    )

    assert await collector.collect_next() is not None
    assert len(db.list_history()) == 1


async def test_fake_source_switches_unit_and_converts_its_values():
    source = FakeGrillSource(interval_seconds=0)
    stream = source.readings()
    before = await anext(stream)

    after = await _send(source, stream, Command("set-unit", fahrenheit=False))
    back = await _send(source, stream, Command("set-unit", fahrenheit=True))

    assert after["isFahrenheit"] is False
    # 225 F is 107.2 C; the nearest Celsius setpoint is 110 (230 F).
    assert after["grillSetTemp"] == 110
    assert after["p1Temp"] < before["p1Temp"]
    assert after["p1Target"] == round((165 - 32) * 5 / 9, 1)
    assert back["isFahrenheit"] is True
    assert back["grillSetTemp"] == 230
    assert source.blank_frame_expected is True


@pytest.mark.parametrize("fahrenheit", PB850PS2_SETPOINTS)
async def test_fake_unit_switch_keeps_setpoints_on_the_board_list(fahrenheit):
    source = FakeGrillSource(interval_seconds=0)
    source.set_temp = fahrenheit
    stream = source.readings()
    await anext(stream)

    after = await _send(source, stream, Command("set-unit", fahrenheit=False))
    back = await _send(source, stream, Command("set-unit", fahrenheit=True))

    assert after["grillSetTemp"] in source.accepted_setpoints(False)
    # The board's floored list maps each Celsius setpoint back to its own.
    assert back["grillSetTemp"] == fahrenheit
