import pytest

pytboss = pytest.importorskip("pytboss")

from smokestack.api import _make_source  # noqa: E402
from smokestack.config import Settings  # noqa: E402
from smokestack.sources import pytboss_http  # noqa: E402
from smokestack.sources.commands import Command, RestartWatch  # noqa: E402
from smokestack.sources.pytboss_http import PytbossHttpSource  # noqa: E402


class FakeBoss:
    instances = []

    def __init__(self, connection, model, password=""):
        self.connection = connection
        self.model = model
        self.password = password
        self.states = []
        self.started = False
        self.stopped = False
        FakeBoss.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def get_state(self):
        item = self.states.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_boss(monkeypatch):
    FakeBoss.instances = []
    monkeypatch.setattr(pytboss, "PitBoss", FakeBoss)
    monkeypatch.setattr(
        pytboss, "HttpConnection", lambda host: f"conn:{host}"
    )
    return FakeBoss


def make_source(**kwargs):
    return PytbossHttpSource(
        model="PB850PS2", host="192.168.1.50", password="", poll_seconds=0, **kwargs
    )


def test_http_source_requires_host():
    source = PytbossHttpSource(model="PB850PS2", host="", password="")

    with pytest.raises(ValueError, match="host"):
        source.validate()


def test_make_source_returns_http_source_with_settings():
    source = _make_source(
        Settings(
            source="http",
            model="PB850PS2",
            host="192.168.1.50",
            password="secret",
            poll_seconds=3,
        )
    )

    assert isinstance(source, PytbossHttpSource)
    assert source.name == "http"
    assert (source.host, source.password, source.poll_seconds) == (
        "192.168.1.50",
        "secret",
        3,
    )


def test_settings_reads_host_from_env(monkeypatch):
    monkeypatch.setenv("PITBOSS_HOST", "192.168.1.50")

    assert Settings.from_env().host == "192.168.1.50"


async def test_readings_polls_repeatedly(fake_boss, monkeypatch):
    async def preload_start(self):
        self.started = True
        self.states = [{"grillTemp": 225}, {"grillTemp": 226}]

    monkeypatch.setattr(fake_boss, "start", preload_start)
    gen = make_source().readings()

    assert await anext(gen) == {"grillTemp": 225}
    assert await anext(gen) == {"grillTemp": 226}
    boss = fake_boss.instances[0]
    assert boss.connection == "conn:192.168.1.50"
    assert not boss.stopped
    await gen.aclose()
    assert boss.stopped


async def test_not_connected_error_propagates_after_stop(fake_boss, monkeypatch):
    from pytboss.exceptions import NotConnectedError

    async def preload_start(self):
        self.states = [{"grillTemp": 225}, NotConnectedError("gone")]

    monkeypatch.setattr(fake_boss, "start", preload_start)
    gen = make_source().readings()

    await anext(gen)
    with pytest.raises(NotConnectedError):
        await anext(gen)
    assert fake_boss.instances[0].stopped


async def test_first_poll_failure_mentions_http_may_be_unavailable(fake_boss, monkeypatch):
    from pytboss.exceptions import NotConnectedError

    async def preload_start(self):
        self.states = [NotConnectedError("refused")]

    monkeypatch.setattr(fake_boss, "start", preload_start)

    with pytest.raises(NotConnectedError, match="may not serve local HTTP"):
        await anext(make_source().readings())
    assert fake_boss.instances[0].stopped


class UptimeBoss:
    """Reports an uptime the way pytboss caches it, and takes a reboot."""

    def __init__(self, uptimes):
        self.uptimes = list(uptimes)
        self.reads = 0
        self.rebooted = False
        self._last_uptime = 1234.0

    async def get_uptime(self):
        self.reads += 1
        uptime = self.uptimes.pop(0)
        if isinstance(uptime, Exception):
            raise uptime
        if uptime is None:
            # pytboss answers 0.0 for a reply without one, and caches nothing.
            return 0.0
        self._last_uptime = uptime
        return uptime

    async def reboot(self):
        self.rebooted = True

    async def get_state(self):
        return {"grillTemp": 225}


def _watched_source(now):
    return make_source(restart_watch=RestartWatch(lambda: now[0]))


async def test_a_reboot_between_two_polls_is_seen_from_the_uptime():
    # #77: the module restarts between two polls, so none fails and the
    # session never drops; its uptime resetting is what shows the restart.
    now = [500.0]
    source = _watched_source(now)
    boss = UptimeBoss([6000.0, 3.0])

    # Nothing extra is read before a reboot.
    assert await source._poll(boss) == {"grillTemp": 225}
    assert boss.reads == 0

    await source._send(boss, Command("reboot"))
    assert boss.rebooted
    now[0] = 505
    await source._poll(boss)  # the module hasn't restarted yet
    assert source.module_restarts == 0
    now[0] = 510
    await source._poll(boss)
    assert source.module_restarts == 1
    # The fresh uptime is what the next key is derived from.
    assert boss._last_uptime == 3.0

    # Once seen, polls go back to reading only the state.
    await source._poll(boss)
    assert boss.reads == 2


async def test_an_uptime_the_grill_doesnt_report_is_not_a_restart():
    now = [500.0]
    source = _watched_source(now)
    boss = UptimeBoss([None])

    await source._send(boss, Command("reboot"))
    now[0] = 510
    await source._poll(boss)

    assert source.module_restarts == 0
    assert source.restart_watch.armed


async def test_a_failed_uptime_read_ends_the_session():
    from pytboss.exceptions import NotConnectedError

    source = _watched_source([500.0])
    boss = UptimeBoss([NotConnectedError("gone")])

    await source._send(boss, Command("reboot"))
    # It ends readings(), so the collector reconnects and counts that instead.
    with pytest.raises(NotConnectedError):
        await source._poll(boss)
