"""测试会话守护：412 重登重试与重登耗尽终止。"""

from __future__ import annotations

import pytest

from spider_gd.core.crawler.exceptions import CaptchaError, TokenExpiredError
from spider_gd.core.crawler.session_keeper import ReloginExhaustedError, SessionKeeper
from spider_gd.interfaces.crawler import CrawlResult, CrawlTask, EndpointConfig, Session


class _Login:
    def __init__(self, *, fail_after: int | None = None) -> None:
        # fail_after=N：第 N+1 次及以后 execute 抛错（模拟 U-Key 被拔）
        self._fail_after = fail_after
        self.calls = 0

    async def execute(self) -> Session:
        self.calls += 1
        if self._fail_after is not None and self.calls > self._fail_after:
            raise CaptchaError("U-Key 被拔，无法登录")
        return Session(bearer_token="tok")


class _Provider:
    def __init__(self, *, refresh_fails: bool = False) -> None:
        self._refresh_fails = refresh_fails
        self.refresh_calls = 0

    async def get_session(self) -> Session:
        return Session(bearer_token="tok")

    def is_expired(self, status_code: int) -> bool:
        return status_code == 412

    async def refresh(self) -> Session:
        self.refresh_calls += 1
        if self._refresh_fails:
            raise RuntimeError("refresh 不可用")
        return Session(bearer_token="tok2")


class _Fetcher:
    """前 ``expire_times`` 次抛 412，之后成功。"""

    def __init__(self, expire_times: int) -> None:
        self._left = expire_times
        self.calls = 0

    async def fetch(
        self, session: Session, endpoint: EndpointConfig, task: CrawlTask
    ) -> CrawlResult:
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise TokenExpiredError("412")
        return CrawlResult(task_id=task.task_id, success=True, data=b"ok")


def _ep() -> EndpointConfig:
    return EndpointConfig(name="x", url="http://x")


def _task() -> CrawlTask:
    return CrawlTask(task_id="T1", date="20260618", data_type="x")


@pytest.mark.unit
async def test_relogin_then_succeeds():
    keeper = SessionKeeper(_Login(), _Provider(), max_relogin=2)
    fetcher = _Fetcher(expire_times=1)
    result = await keeper.fetch(fetcher, _ep(), _task())
    assert result.success is True
    assert fetcher.calls == 2  # 首次 412 + 重登后成功


@pytest.mark.unit
async def test_relogin_exhausted_raises():
    keeper = SessionKeeper(_Login(), _Provider(), max_relogin=2)
    fetcher = _Fetcher(expire_times=99)  # 永远 412
    with pytest.raises(ReloginExhaustedError):
        await keeper.fetch(fetcher, _ep(), _task())


@pytest.mark.unit
async def test_refresh_fail_falls_back_to_login():
    provider = _Provider(refresh_fails=True)
    login = _Login()
    keeper = SessionKeeper(login, provider, max_relogin=2)
    fetcher = _Fetcher(expire_times=1)
    result = await keeper.fetch(fetcher, _ep(), _task())
    assert result.success is True
    # refresh 失败 → 回退完整重登（execute 被调用：首次 get_session 1 次 + 重登 1 次）
    assert login.calls >= 2
    assert provider.refresh_calls == 1


@pytest.mark.unit
async def test_relogin_aborts_when_login_fails():
    # 初次登录成功；后续 refresh 失败且重登也失败（U-Key 拔出）→ 终止
    keeper = SessionKeeper(
        _Login(fail_after=1), _Provider(refresh_fails=True), max_relogin=2
    )
    fetcher = _Fetcher(expire_times=99)
    with pytest.raises(ReloginExhaustedError):
        await keeper.fetch(fetcher, _ep(), _task())
