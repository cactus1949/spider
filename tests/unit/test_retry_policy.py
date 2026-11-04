"""测试指数退避重试。"""

from __future__ import annotations

import pytest

from spider_gd.core.scheduler.retry_policy import retry_with_backoff


@pytest.mark.unit
async def test_retry_succeeds_after_failures():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    delays: list[float] = []

    async def fake_sleep(d):
        delays.append(d)

    result = await retry_with_backoff(flaky, max_retries=4, base_delay=1.0, sleep_fn=fake_sleep)
    assert result == "ok"
    assert delays == [1.0, 2.0]  # 指数退避 1 -> 2


@pytest.mark.unit
async def test_retry_exhausts_and_raises():
    async def always_fail():
        raise RuntimeError("nope")

    async def fake_sleep(_d):
        return None

    with pytest.raises(RuntimeError, match="nope"):
        await retry_with_backoff(always_fail, max_retries=3, sleep_fn=fake_sleep)
