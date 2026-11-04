"""测试登录流程编排（注入假 page_ops / captcha / session_provider）。"""

from __future__ import annotations

import pytest

from spider_gd.core.crawler.exceptions import CaptchaError
from spider_gd.core.crawler.login_flow import LoginFlow
from spider_gd.interfaces.crawler import Session


class _FakePageOps:
    def __init__(self, login_after: int = 1):
        self.login_after = login_after
        self.submit_count = 0
        self.opened = False

    async def open(self):
        self.opened = True

    async def fill_credentials(self, username, password):
        self._creds = (username, password)

    async def get_captcha_image(self):
        return b"captcha-image"

    async def fill_captcha(self, code):
        self._code = code

    async def refresh_captcha(self):
        pass

    async def submit(self):
        self.submit_count += 1

    async def is_logged_in(self):
        return self.submit_count >= self.login_after


class _FakeSolver:
    def __init__(self, results):
        self._results = list(results)

    def recognize(self, _image):
        r = self._results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _FakeSessionProvider:
    async def get_session(self):
        return Session(bearer_token="tok", hdkgccbl="abc")

    def is_expired(self, code):
        return code == 412

    async def refresh(self):
        return await self.get_session()


@pytest.mark.unit
async def test_login_success_first_attempt():
    ops = _FakePageOps(login_after=1)
    flow = LoginFlow(ops, _FakeSolver(["1234"]), _FakeSessionProvider(), "u", "p")
    session = await flow.execute()
    assert session.bearer_token == "tok"
    assert ops.opened is True
    assert ops.submit_count == 1


@pytest.mark.unit
async def test_login_retries_on_captcha_error_then_succeeds():
    ops = _FakePageOps(login_after=2)
    solver = _FakeSolver([CaptchaError("bad"), "5678", "5678"])
    flow = LoginFlow(ops, solver, _FakeSessionProvider(), "u", "p", max_attempts=5)
    session = await flow.execute()
    assert session.bearer_token == "tok"


@pytest.mark.unit
async def test_login_exhausts_attempts_raises():
    ops = _FakePageOps(login_after=999)  # 永不登录成功
    flow = LoginFlow(
        ops, _FakeSolver(["1111"] * 3), _FakeSessionProvider(), "u", "p", max_attempts=3
    )
    with pytest.raises(CaptchaError, match="登录失败"):
        await flow.execute()
