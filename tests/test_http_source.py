import pytest

pytboss = pytest.importorskip("pytboss")

from pitboss_bridge.api import _make_source  # noqa: E402
from pitboss_bridge.config import Settings  # noqa: E402
from pitboss_bridge.sources import pytboss_http  # noqa: E402
from pitboss_bridge.sources.pytboss_http import PytbossHttpSource  # noqa: E402


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
