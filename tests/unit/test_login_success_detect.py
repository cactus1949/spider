"""点立即登录后，用登录接口或新开的工作台判断成功，不再只等固定 5 秒。"""

from __future__ import annotations

import time

import pytest
from workflow_runner import WorkflowPaused, WorkflowRunner, interpret_login_submit, login_wait_ms


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        self.front = 0

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.front += 1

    def on(self, *_args, **_kwargs) -> None:
        return None

    async def evaluate(self, *_args, **_kwargs):
        return []


def _runner(tmp_path, page: _Page) -> WorkflowRunner:
    runner = WorkflowRunner(
        {"name": "登陆流程测试", "type": "login", "steps": []},
        download_dir=str(tmp_path),
    )
    runner.pages = [page]
    return runner


def test_login_wait_and_submit_interpretation():
    assert login_wait_ms({}) == 30000
    assert login_wait_ms({"login_wait_ms": 800}) == 30000
    assert interpret_login_submit(200, "application/json", '{"code":0,"token":"secret"}') == {
        "outcome": "success",
        "message": "",
    }
    failed = interpret_login_submit(
        200, "application/json;charset=UTF-8", '{"code":1,"msg":"验证码错误"}',
    )
    assert failed == {"outcome": "failed", "message": "验证码错误"}
    assert interpret_login_submit(400, "text/html", "\r\n\r\n\r\n") is None


@pytest.mark.unit
async def test_new_workbench_tab_is_bound_and_duplicate_is_closed(tmp_path):
    login = _Page("https://pm.gd.csg.cn/portal/#/user/login")
    stale = _Page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    opened = _Page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner = _runner(tmp_path, login)
    runner.pages = [stale, login]
    runner._current_page_idx = 1

    async def click(_selector, timeout=0):
        runner.pages.append(opened)
        return True

    runner._low_profile_click = click
    await runner._do_click({"selector": "button:has-text('立即登录')"})

    assert runner.page is opened
    assert opened.front >= 1
    assert stale.closed is True
    assert login.closed is False


@pytest.mark.unit
async def test_existing_workbench_alone_does_not_count_as_this_login(tmp_path):
    login = _Page("https://pm.gd.csg.cn/portal/#/user/login")
    existing = _Page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner = _runner(tmp_path, login)
    runner.pages = [existing, login]
    runner._current_page_idx = 1
    runner._protected_pages = {existing}

    async def click(_selector, timeout=0):
        return True

    runner._low_profile_click = click
    started = time.monotonic()
    with pytest.raises(WorkflowPaused, match="没有进入工作台"):
        await runner._do_click({
            "selector": "button:has-text('立即登录')",
            "login_wait_ms": 1000,
        })
    assert time.monotonic() - started < 3
    assert existing.closed is False
    assert runner.page is login


@pytest.mark.unit
async def test_login_api_failure_is_reported_without_waiting_out(tmp_path):
    login = _Page("https://pm.gd.csg.cn/portal/#/user/login")
    runner = _runner(tmp_path, login)

    async def click(_selector, timeout=0):
        runner._login_submit_verdicts.append({
            "outcome": "failed",
            "message": "验证码错误",
            "at": time.monotonic(),
        })
        return True

    runner._low_profile_click = click
    started = time.monotonic()
    with pytest.raises(WorkflowPaused, match="验证码错误"):
        await runner._do_click({
            "selector": "button:has-text('立即登录')",
            "login_wait_ms": 30000,
        })
    assert time.monotonic() - started < 2


@pytest.mark.unit
async def test_bound_page_leaving_login_counts_as_success(tmp_path):
    login = _Page("https://pm.gd.csg.cn/portal/#/user/login")
    runner = _runner(tmp_path, login)

    async def click(_selector, timeout=0):
        login.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
        return True

    runner._low_profile_click = click
    await runner._do_click({"selector": "button:has-text('立即登录')"})
    assert runner.page is login
    assert login.front >= 1
