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


class _ListeningPage(_Page):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.events: dict[str, list] = {}

    def on(self, event, handler) -> None:
        self.events.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler) -> None:
        self.events[event].remove(handler)


class _LoginResponse:
    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.url = "https://pm.gd.csg.cn/portal/xhr/sso-auth/login"
        self.headers = {"content-type": content_type}
        self.request = type("Request", (), {"method": "POST"})()
        self._body = body

    async def body(self) -> bytes:
        return self._body


@pytest.mark.unit
async def test_login_listener_is_armed_before_click_and_removed_after(tmp_path):
    login = _ListeningPage("https://pm.gd.csg.cn/portal/#/user/login")
    runner = _runner(tmp_path, login)
    seen_during_click: list[int] = []

    async def click(_selector, timeout=0):
        handlers = login.events.get("response", [])
        seen_during_click.append(len(handlers))
        body = '{"code":1,"message":"验证码错误"}'.encode()
        handlers[0](_LoginResponse(200, "application/json", body))
        await __import__("asyncio").sleep(0)
        return True

    runner._low_profile_click = click
    with pytest.raises(WorkflowPaused, match="验证码错误"):
        await runner._do_click({"selector": "button:has-text('立即登录')"})

    assert seen_during_click == [1]
    assert login.events["response"] == []
    assert runner._login_listeners == []


@pytest.mark.unit
async def test_late_login_response_does_not_leak_into_the_next_attempt(tmp_path):
    login = _ListeningPage("https://pm.gd.csg.cn/portal/#/user/login")
    runner = _runner(tmp_path, login)
    saved: list = []

    async def click(_selector, timeout=0):
        saved.append(login.events["response"][0])
        return True

    runner._low_profile_click = click
    with pytest.raises(WorkflowPaused, match="没有进入工作台"):
        await runner._do_click({
            "selector": "button:has-text('立即登录')",
            "login_wait_ms": 1000,
        })
    assert login.events["response"] == []
    saved[0](_LoginResponse(200, "application/json", '{"code":1,"message":"验证码错误"}'.encode()))
    await __import__("asyncio").sleep(0)
    assert runner._login_submit_verdicts == []

    async def succeed(_selector, timeout=0):
        login.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
        return True

    runner._low_profile_click = succeed
    await runner._do_click({"selector": "button:has-text('立即登录')"})
    assert runner.page is login
    assert runner._login_listeners == []
    runner._release_page_observers()
    assert login.events.get("response", []) == []


@pytest.mark.unit
async def test_gateway_html_400_is_not_a_business_login_failure(tmp_path):
    login = _ListeningPage("https://pm.gd.csg.cn/portal/#/user/login")
    runner = _runner(tmp_path, login)

    async def click(_selector, timeout=0):
        login.events["response"][0](_LoginResponse(400, "text/html", b"\r\n\r\n\r\n"))
        await __import__("asyncio").sleep(0)
        runner._page_errors.append({
            "status": 400,
            "url": "https://pm.gd.csg.cn/portal/xhr/sso-auth/login",
            "resource_type": "xhr",
            "content_type": "text/html",
            "body_size": 6,
            "body_blank": True,
        })
        return True

    runner._low_profile_click = click
    started = time.monotonic()
    with pytest.raises(WorkflowPaused, match="网关拦截") as caught:
        await runner._do_click({"selector": "button:has-text('立即登录')", "login_wait_ms": 30000})
    assert time.monotonic() - started < 2
    assert "验证码错误" not in str(caught.value)
    assert "不是验证码或证书错误" in str(caught.value)
    assert runner._login_submit_verdicts == []
    assert login.events["response"] == []


@pytest.mark.unit
async def test_blank_new_tab_is_not_treated_as_leaving_login(tmp_path):
    login = _Page("https://pm.gd.csg.cn/portal/#/user/login")
    blank = _Page("about:blank")
    runner = _runner(tmp_path, login)

    async def click(_selector, timeout=0):
        runner.pages.append(blank)
        runner._current_page_idx = len(runner.pages) - 1
        return True

    runner._low_profile_click = click
    with pytest.raises(WorkflowPaused, match="没有进入工作台"):
        await runner._do_click({
            "selector": "button:has-text('立即登录')",
            "login_wait_ms": 1000,
        })
    assert blank.front == 0
    assert login.closed is False


@pytest.mark.unit
async def test_already_logged_in_skips_the_login_click(tmp_path):
    login = _ListeningPage("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner = _runner(tmp_path, login)
    runner._skip_remaining_login_steps = True
    runner.steps = [{"action": "click", "selector": "button:has-text('立即登录')"}]
    called = {"n": 0}

    async def click(_selector, timeout=0):
        called["n"] += 1
        return True

    runner._low_profile_click = click
    await runner._execute_steps(lambda _event: None, None, shared=True)
    assert called["n"] == 0
    assert runner.failed is False
    assert login.events.get("response", []) == []
    assert runner._login_listeners == []


@pytest.mark.unit
async def test_fixed_wait_after_success_does_not_leave_the_workbench(tmp_path, monkeypatch):
    import workflow_runner as workflow_runner_module

    slept: list[float] = []

    async def _sleep(seconds, *_args, **_kwargs):
        slept.append(seconds)

    monkeypatch.setattr(workflow_runner_module.asyncio, "sleep", _sleep)
    login = _Page("https://pm.gd.csg.cn/portal/#/user/login")
    runner = _runner(tmp_path, login)

    async def click(_selector, timeout=0):
        login.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
        return True

    runner._low_profile_click = click
    await runner._do_click({"selector": "button:has-text('立即登录')"})
    await runner._do_wait({"action": "wait", "ms": 5000})
    assert slept == [5]
    assert runner.page is login
    assert login.url.endswith("#/powerEnterprise")


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
