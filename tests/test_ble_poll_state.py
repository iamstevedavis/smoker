import pytest

Unauthorized = pytest.importorskip("pytboss.exceptions").Unauthorized

from pitboss_bridge.sources.pytboss_ble import poll_state  # noqa: E402


class FakeBoss:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0
        self._last_uptime = 123.0

    async def get_state(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise Unauthorized("Unauthorized", 401)
        return {"grillTemp": 225}


async def test_poll_state_retries_once_with_fresh_uptime_after_unauthorized():
    boss = FakeBoss(failures=1)

    assert await poll_state(boss) == {"grillTemp": 225}
    assert boss.calls == 2
    assert boss._last_uptime is None


async def test_poll_state_gives_up_after_second_unauthorized():
    boss = FakeBoss(failures=2)

    with pytest.raises(Unauthorized):
        await poll_state(boss)
    assert boss.calls == 2
