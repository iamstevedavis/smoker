import pytest

Unauthorized = pytest.importorskip("pytboss.exceptions").Unauthorized

from smokestack.sources.pytboss_ble import poll_state  # noqa: E402


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


from smokestack.sources._pytboss import PollStats, UptimeRefresher  # noqa: E402


def test_uptime_refresher_drops_cache_only_when_due():
    now = [0.0]
    refresher = UptimeRefresher(interval_seconds=20, clock=lambda: now[0])
    boss = FakeBoss(failures=0)

    now[0] = 19
    assert refresher.refresh_if_due(boss) is False
    assert boss._last_uptime == 123.0

    now[0] = 20
    assert refresher.refresh_if_due(boss) is True
    assert boss._last_uptime is None

    boss._last_uptime = 5.0
    now[0] = 39
    assert refresher.refresh_if_due(boss) is False
    now[0] = 40
    assert refresher.refresh_if_due(boss) is True


async def test_poll_state_refreshes_on_schedule_before_polling():
    now = [100.0]
    refresher = UptimeRefresher(interval_seconds=20, clock=lambda: now[0])
    boss = FakeBoss(failures=0)
    seen = []
    original = boss.get_state

    async def get_state():
        seen.append(boss._last_uptime)
        return await original()

    boss.get_state = get_state
    stats = PollStats()

    await poll_state(boss, stats, refresher)
    now[0] = 121
    await poll_state(boss, stats, refresher)

    assert seen == [123.0, None]
    assert stats.unauthorized_retries == 0


async def test_unauthorized_retry_is_counted_once_per_failure():
    stats = PollStats()

    await poll_state(FakeBoss(failures=1), stats)
    assert stats.unauthorized_retries == 1
    with pytest.raises(Unauthorized):
        await poll_state(FakeBoss(failures=2), stats)
    assert stats.unauthorized_retries == 2
