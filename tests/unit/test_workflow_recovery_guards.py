"""锁住审查报告里的反例：错误不能被当成成功，半登录不能被继续放行。"""

from __future__ import annotations

import pytest
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowExecutionError, WorkflowPaused, WorkflowRunner

from spider_gd.core.crawler.shared_browser import SharedBrowser


class _Tab:
    def __init__(self, url: str) -> None:
        self.url = url
        self.reloads = 0
        self.closed = False
        self.clicks: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    def on(self, *_args, **_kwargs) -> None:
        return None

    async def reload(self, **_kwargs) -> None:
        self.reloads += 1

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        return None

    async def title(self) -> str:
        return "mock"

    async def evaluate(self, *_args, **_kwargs):
        return []

    async def click(self, selector: str, **_kwargs) -> None:
        self.clicks.append(selector)
        raise AssertionError(f"不应点击 {selector}")


def _gateway(url: str = "https://pm.gd.csg.cn/pfxh/qctc/getJddjTree") -> dict:
    return {
        "status": 400,
        "url": url,
        "resource_type": "xhr",
        "content_type": "text/html",
        "body_size": 6,
        "body_blank": True,
        "page_url": "https://pm.gd.csg.cn/pfxh/stc/stale",
        "at": "00:00:00",
    }


def _steps() -> list[dict]:
    return [
        {"action": "click", "selector": "text=现货交易", "expect": "new_tab"},
        {"action": "export", "selector": "#export", "save_as": "out.xlsx", "timeout": 50},
    ]


def _runner(tmp_path, **config):
    runner = WorkflowRunner(
        {"name": "恢复守卫", "steps": _steps(), **config},
        download_dir=str(tmp_path),
    )
    parent = _Tab("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    child = _Tab("https://pm.gd.csg.cn/pfxh/stc/stale")
    runner.pages = [parent, child]
    runner._protected_pages = {parent}
    runner._current_page_idx = 1
    return runner, parent, child


async def _run_until_pause(runner, *, continues: int = 0):
    events: list[dict] = []
    calls = {"n": 0}

    async def wait() -> bool:
        calls["n"] += 1
        return calls["n"] <= continues

    await runner._execute_steps(events.append, wait)
    return events, calls["n"]


async def test_second_gateway_400_pauses_without_export_or_done(tmp_path):
    runner, parent, child = _runner(tmp_path)
    runner._recovery_budget[0] = 1
    runner._page_errors.append(_gateway())

    events, waits = await _run_until_pause(runner, continues=1)

    names = [event["event"] for event in events]
    assert waits >= 2
    assert "error_pause" in names
    assert "workflow_done" not in names
    assert "step_done" not in names
    assert "page_recovery" not in names
    assert child.closed is False
    assert parent.reloads == 0
    assert runner.failed is True


@pytest.mark.parametrize("http_error", [False, True])
async def test_wait_timeout_handles_late_http_error_before_failure(tmp_path, http_error):
    runner, _, child = _runner(tmp_path, page_recovery=False)
    runner.steps = [{"action": "wait", "selector": "#result"},
                    {"action": "export", "selector": "#export"}]
    executed = []

    async def execute(step):
        executed.append(step["action"])
        if http_error:
            runner._page_errors.append({**_gateway(), "_page": child})
        raise WorkflowExecutionError("等待元素超时：#result")

    runner._exec_step = execute
    events, _ = await _run_until_pause(runner)
    names = [event["event"] for event in events]
    assert ("error_pause" in names) is http_error
    assert ("step_error" in names) is not http_error
    assert "step_done" not in names
    assert "workflow_done" not in names
    assert executed == ["wait"]
    assert runner.failed


async def test_total_recovery_budget_pauses_without_export_or_done(tmp_path):
    runner, parent, child = _runner(tmp_path, max_page_recoveries=1)
    runner._recovery_attempts = 1
    runner._page_errors.append(_gateway())

    events, waits = await _run_until_pause(runner)

    names = [event["event"] for event in events]
    assert waits == 1
    assert "error_pause" in names
    assert "workflow_done" not in names
    assert "step_done" not in names
    assert child.closed is False
    assert parent.reloads == 0


async def test_page_recovery_off_does_not_treat_recorded_error_as_success(tmp_path):
    runner, _parent, child = _runner(tmp_path, page_recovery="off")
    runner._page_errors.append(_gateway())

    events, waits = await _run_until_pause(runner)

    names = [event["event"] for event in events]
    assert waits == 1
    assert "error_pause" in names
    assert "workflow_done" not in names
    assert "page_recovery" not in names
    assert child.closed is False


async def test_json_400_does_not_close_child_tab(tmp_path):
    runner, parent, child = _runner(tmp_path)
    runner._page_errors.append({
        "status": 400,
        "url": "https://pm.gd.csg.cn/pfxh/qctc/getJddjTree",
        "resource_type": "xhr",
        "content_type": "application/json;charset=UTF-8",
        "body_size": 120,
        "page_url": "https://pm.gd.csg.cn/pfxh/stc/stale",
        "at": "00:00:00",
    })

    events, waits = await _run_until_pause(runner)

    names = [event["event"] for event in events]
    assert waits == 1
    assert "error_pause" in names
    assert "page_recovery" not in names
    assert "workflow_done" not in names
    assert child.closed is False
    assert parent.reloads == 0


async def test_no_closable_tab_pauses_without_reload(tmp_path):
    page = _Tab("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner = WorkflowRunner({
        "name": "无可关页",
        "steps": [{"action": "click", "selector": "text=现货交易", "expect": "new_tab"}],
    }, download_dir=str(tmp_path))
    runner.pages = [page]
    runner._protected_pages = {page}
    runner._page_errors.append(_gateway())
    events: list[dict] = []
    calls = {"n": 0}

    async def wait() -> bool:
        calls["n"] += 1
        return False

    await runner._execute_steps(events.append, wait)

    names = [event["event"] for event in events]
    assert calls["n"] == 1
    assert "error_pause" in names
    assert "page_recovery" not in names
    assert "workflow_done" not in names
    assert "step_error" not in names
    assert page.reloads == 0
    assert page.closed is False


async def test_continue_after_no_safe_path_pauses_again(tmp_path):
    page = _Tab("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner = WorkflowRunner({
        "name": "继续仍失败",
        "steps": [{"action": "click", "selector": "#stay"}],
    }, download_dir=str(tmp_path))
    runner.pages = [page]
    runner._page_errors.append(_gateway())
    events, waits = await _run_until_pause(runner, continues=1)
    # _run_until_pause uses the runner's own steps from _runner; this runner is separate.
    # Call execute directly so the custom steps are used.
    assert runner.steps[0]["selector"] == "#stay"
    names = [event["event"] for event in events]
    assert waits >= 2
    assert names.count("error_pause") >= 2
    assert page.reloads == 0
    assert "workflow_done" not in names


class _StorePage:
    def __init__(self) -> None:
        self.local = {"pfxh-token": "keep-local"}
        self.session = {"keep": "1", "drop": "2"}
        self.url = "https://pm.gd.csg.cn/portal/#/home"
        self.reloads = 0

    async def evaluate(self, script: str, arg=None):
        if "clear()" in script:
            raise AssertionError(script)
        store = self.local if "localStorage" in script else self.session
        if "removeItem" in script:
            store.pop(arg, None)
            return None
        return list(store)

    async def reload(self, **_kwargs) -> None:
        self.reloads += 1


async def test_reset_session_storage_keeps_local_storage_and_keep_keys(tmp_path):
    page = _StorePage()
    runner = WorkflowRunner({"name": "只清会话", "steps": []}, download_dir=str(tmp_path))
    runner.pages = [page]

    await runner._do_reset_site_storage({
        "action": "reset_site_storage",
        "types": ["session_storage"],
        "keep_keys": ["keep"],
        "reload": False,
    })

    assert page.local == {"pfxh-token": "keep-local"}
    assert page.session == {"keep": "1"}
    assert page.reloads == 0


class _ObsPage:
    def __init__(self) -> None:
        self.url = "https://example.test/app"
        self.listeners: dict[str, list] = {}

    def is_closed(self) -> bool:
        return False

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        self.listeners[event] = [
            item for item in self.listeners.get(event, []) if item is not handler
        ]

    async def title(self) -> str:
        return "obs"

    async def evaluate(self, *_args, **_kwargs):
        return []


async def test_finished_runner_does_not_record_later_responses(tmp_path):
    page = _ObsPage()
    runner = WorkflowRunner(
        {"name": "观测器释放", "steps": [{"action": "wait", "ms": 1}]},
        download_dir=str(tmp_path),
    )
    runner.pages = [page]
    events: list[dict] = []
    await runner._execute_steps(events.append, None)
    assert "workflow_done" in [event["event"] for event in events]
    assert page.listeners.get("response", []) == []
    assert page.listeners.get("console", []) == []
    assert page.listeners.get("pageerror", []) == []
    assert runner._page_observers == {}
    assert runner._page_errors == []


class _AuthPage:
    def __init__(self) -> None:
        self.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
        self.visible = {".el-menu"}
        self.storage = {"pfxh-userInfo": '{"screenName":"范智灏"}', "pfxh-token": ""}
        self.screenshots = 0

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str):
        page = self

        class _Locator:
            first = None

            async def is_visible(self, **_kwargs) -> bool:
                return selector in page.visible

            async def inner_text(self, **_kwargs) -> str:
                return ""

            async def wait_for(self, **_kwargs) -> None:
                return None

        locator = _Locator()
        locator.first = locator
        return locator

    async def evaluate(self, script: str, arg=None):
        if isinstance(arg, list):
            return [[key, self.storage.get(key, "")] for key in arg]
        if "pfxh-token" in script and ".length" in script:
            return len(self.storage.get("pfxh-token") or "")
        if "pfxh-userInfo" in script and ".length" in script:
            return len(self.storage.get("pfxh-userInfo") or "")
        return []

    async def screenshot(self, **_kwargs) -> None:
        self.screenshots += 1

    async def title(self) -> str:
        return "login"


async def test_continue_without_token_does_not_skip_login(tmp_path):
    page = _AuthPage()
    session = SharedBrowser()
    session._config["target_origin"] = "https://pm.gd.csg.cn"
    session._config["success_selector"] = ".el-menu"
    auth = await session.check_auth(page, require_storage_keys=["pfxh-token"])
    assert auth["state"] != "authenticated"
    assert "pfxh-token" in auth["reason"]

    runner = WorkflowRunner({
        "name": "登陆流程测试",
        "type": "login",
        "steps": [{"action": "captcha", "require_storage_keys": ["pfxh-token"]}],
    }, download_dir=str(tmp_path))
    runner.pages = [page]
    runner.browser = type("Browser", (), {"is_connected": lambda self: True})()
    runner._shared_session = session
    calls = {"n": 0}

    async def wait() -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    runner._wait_continue = wait
    with pytest.raises(WorkflowPaused):
        await runner._pause_login_error("半登录状态：缺少 localStorage 关键项 pfxh-token")

    assert calls["n"] == 2
    assert runner._skip_remaining_login_steps is False


async def test_require_auth_continue_does_not_accept_half_login():
    page = _AuthPage()
    session = SharedBrowser()
    session._config["target_origin"] = "https://pm.gd.csg.cn"
    session._config["success_selector"] = ".el-menu"
    events: list[dict] = []
    calls = {"n": 0}
    session.emit = lambda event: events.append(event)  # type: ignore[method-assign]

    async def wait() -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    session.wait_continue = wait  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="取消"):
        await session.require_auth(page=page, require_storage_keys=["pfxh-token"])

    assert calls["n"] == 2
    assert any(event.get("event") == "error_pause" for event in events)


def test_http_200_html_is_not_called_gateway_rejection(tmp_path):
    runner = WorkflowRunner({"name": "诊断", "steps": []}, download_dir=str(tmp_path))
    runner._auth_traffic.extend([
        {
            "method": "GET", "status": 200, "content_type": "text/html",
            "path": "/portal/xhr/sso-auth/login", "url": "https://pm.gd.csg.cn/login",
        },
        {
            "method": "GET", "status": 200, "content_type": "",
            "path": "/portal/xhr/sso-auth/login", "url": "https://pm.gd.csg.cn/login",
        },
        {
            "method": "POST", "status": 400, "content_type": "application/json",
            "path": "/portal/xhr/sso-auth/login", "url": "https://pm.gd.csg.cn/login",
        },
    ])
    text = runner._describe_auth_traffic()
    assert text.count("疑似网关拦截") == 0
    assert "业务响应" in text


class _Query:
    resource_type = "xhr"
    url = "https://pm.gd.csg.cn/pfxh/qctc/getJddjTree"


async def test_export_pauses_while_query_is_inflight(tmp_path):
    page = _Tab("https://pm.gd.csg.cn/pfxh/stc/check")
    runner = WorkflowRunner({"name": "导出等待", "steps": []}, download_dir=str(tmp_path))
    runner.pages = [page]
    runner._inflight.add(_Query())
    calls = {"n": 0}

    async def wait() -> bool:
        calls["n"] += 1
        return False

    runner._wait_continue = wait
    with pytest.raises(WorkflowPaused, match="在途"):
        await runner._do_export({"selector": "#export", "timeout": 50})
    assert calls["n"] == 1
    assert page.clicks == []


@pytest.mark.parametrize(('content_type', 'body', 'recoverable'), [
    ('text/html; charset=utf-8', b'\r\n\r\n\r\n', True),
    ('text/html', b'', True),
    ('text/html', b'STALE!', False),
    ('', b'\r\n', False),
    ('application/json', b'{}', False),
    ('text/html', b' ' * 65, False),
])
async def test_response_classification_requires_small_blank_html(tmp_path, content_type, body, recoverable):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runner, _, child = _runner(tmp_path)
    request = SimpleNamespace(resource_type='xhr', frame=SimpleNamespace(page=child), method='GET')
    response = SimpleNamespace(request=request, status=400, url=child.url + '/query',
                               headers={'content-type': content_type}, body=AsyncMock(return_value=body))
    await runner._capture_page_response(response)
    error = runner._unacknowledged_page_errors()[0]
    assert error['_page'] is child
    assert error['body_size'] == len(body)
    assert runner._is_gateway_form(error) is recoverable


async def test_pending_error_body_already_blocks_export(tmp_path):
    import asyncio
    from types import SimpleNamespace

    runner, _, child = _runner(tmp_path)
    body_started, release = asyncio.Event(), asyncio.Event()

    async def body():
        body_started.set()
        await release.wait()
        return b'\r\n'

    response = SimpleNamespace(status=400, url=child.url + '/query',
        headers={'content-type': 'text/html'}, body=body,
        request=SimpleNamespace(resource_type='xhr', frame=SimpleNamespace(page=child)))
    task = asyncio.create_task(runner._capture_page_response(response))
    try:
        await body_started.wait()
        assert runner._unacknowledged_page_errors()[0]['body_size'] is None
        with pytest.raises(WorkflowPaused):
            await runner._do_export({'selector': '#export'})
        assert child.clicks == []
    finally:
        release.set()
        await task


async def test_parent_error_cannot_close_current_child(tmp_path):
    runner, parent, child = _runner(tmp_path)
    runner._page_errors.append({**_gateway(), '_page': parent})
    runner._tab_checkpoints[child] = (0, parent)
    with pytest.raises(WorkflowPaused, match='不属于当前唯一'):
        await runner._maybe_recover(1, stage='before')
    assert not child.closed
    assert not parent.reloads


async def test_no_popup_provenance_cannot_close_existing_page(tmp_path):
    runner, parent, child = _runner(tmp_path)
    runner._page_errors.append({**_gateway(), '_page': child})
    with pytest.raises(WorkflowPaused, match='没有可关闭的新标签'):
        await runner._maybe_recover(1, stage='before')
    assert not child.closed
    assert not parent.reloads


async def test_recovery_does_not_acknowledge_parent_error_during_close(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    runner, parent, child = _runner(tmp_path)
    runner._tab_checkpoints[child] = (0, parent)
    parent.locator = MagicMock()
    parent.locator.return_value.first.is_visible = AsyncMock(return_value=True)
    runner._page_errors.append({**_gateway(), '_page': child})
    parent_error = {**_gateway(), '_page': parent}

    async def close():
        child.closed = True
        runner._page_errors.append(parent_error)

    child.close = close
    assert await runner._maybe_recover(1, stage='before')
    assert runner._unacknowledged_page_errors() == [parent_error]
    with pytest.raises(WorkflowPaused):
        await runner._maybe_recover(0, stage='before')
    assert not parent.reloads


async def test_cannot_replay_completed_export(tmp_path):
    runner, parent, child = _runner(tmp_path)
    runner.steps.append({'action': 'pick_date', 'value': '-1d'})
    runner._tab_checkpoints[child] = (0, parent)
    runner._page_errors.append({**_gateway(), '_page': child})
    with pytest.raises(WorkflowPaused, match='有副作用'):
        await runner._maybe_recover(2, stage='after')
    assert not child.closed


class _LoginFormPage(DomProbeFakePage):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://pm.gd.csg.cn/portal/#/user/login"
        self.visible: set[str] = set()
        self.storage: dict[str, str] = {}
        self.clicked: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.typed: list[str] = []
        self.gotos: list[str] = []
        self.reloads = 0

    def probe_visible(self, selector, index=None):
        return selector in self.visible or selector in {
            "button:has-text('登 录')", "text=证书登录", "input[type='password']",
            "button:has-text('立即登录')",
        }

    def apply_click(self, selector, index):
        self.clicked.append(selector)
        if selector == "button:has-text('立即登录')":
            self.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
            self.storage["pfxh-token"] = "token"
            self.visible.add("text=现货交易")

    def is_closed(self) -> bool:
        return False

    def on(self, *_args, **_kwargs) -> None:
        return None

    def locator(self, selector: str):
        page = self

        class _Locator:
            first = None

            async def is_visible(self, **_kwargs) -> bool:
                return selector in page.visible

            async def click(self, **_kwargs) -> None:
                page.clicked.append(selector)
                if selector == "button:has-text('立即登录')":
                    page.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
                    page.storage["pfxh-token"] = "token"
                    page.visible.add("text=现货交易")

            async def wait_for(self, **_kwargs) -> None:
                if selector not in page.visible:
                    raise TimeoutError(selector)

            async def inner_text(self, **_kwargs) -> str:
                return ""

        locator = _Locator()
        locator.first = locator
        return locator

    async def goto(self, url: str, **_kwargs) -> None:
        self.gotos.append(url)
        self.url = url

    async def fill(self, selector: str, value: str) -> None:
        self.fills.append((selector, value))

    async def evaluate(self, _script: str, arg=None):
        if isinstance(arg, dict) and "op" in arg:
            return await super().evaluate(_script, arg)
        if isinstance(arg, list):
            return [[key, self.storage.get(key, "")] for key in arg]
        return []

    async def reload(self, **_kwargs) -> None:
        self.reloads += 1

    async def title(self) -> str:
        return "login"


async def test_explicit_login_executes_click_fill_and_captcha(tmp_path, monkeypatch):
    """显式登录不能因 manual/reuse_only 退化成只做 ensure_login。"""
    import workflow_runner as workflow_runner_module

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow_runner_module.asyncio, "sleep", _no_sleep)
    page = _LoginFormPage()
    session = SharedBrowser()
    session._config["auth_policy"] = "reuse_only"
    session._config["target_origin"] = "https://pm.gd.csg.cn"
    session._config["success_selector"] = "text=现货交易"
    runner = WorkflowRunner({
        "name": "登陆流程测试",
        "type": "login",
        "auth_policy": "manual",
        "require_storage_keys": ["pfxh-token"],
        "steps": [
            {"action": "login", "url": "https://pm.gd.csg.cn/portal/#/home"},
            {"action": "click", "selector": "button:has-text('登 录')"},
            {"action": "click", "selector": "text=证书登录"},
            {"action": "fill", "selector": "input[type='password']", "value": "secret"},
            {
                "action": "captcha",
                "require_storage_keys": ["pfxh-token"],
            },
            {"action": "click", "selector": "button:has-text('立即登录')"},
        ],
    }, download_dir=str(tmp_path))
    runner.pages = [page]
    runner.browser = type("Browser", (), {"is_connected": lambda self: True})()
    runner._shared_session = session
    events: list[dict] = []
    captcha_steps: list[dict] = []

    async def _captcha(step: dict) -> None:
        captcha_steps.append(step)

    runner._do_captcha = _captcha
    await runner._execute_steps(events.append, None, shared=True)

    assert page.gotos == ["https://pm.gd.csg.cn/portal/#/home"]
    assert page.clicked == [
        "button:has-text('登 录')",
        "text=证书登录",
        "input[type='password']",
        "button:has-text('立即登录')",
    ]
    assert page.fills == []
    assert page.typed == ["secret"]
    assert captcha_steps and "submit_selector" not in captcha_steps[0]
    assert page.reloads == 0
    assert runner.failed is False
    assert not any("手工完成登录" in str(event.get("message", "")) for event in events)


async def test_login_gateway_400_pauses_without_refresh(tmp_path):
    page = _Tab("https://pm.gd.csg.cn/portal/#/user/login")
    runner = WorkflowRunner({
        "name": "登陆流程测试",
        "type": "login",
        "auth_policy": "manual",
        "steps": [{"action": "captcha", "submit_selector": "button:has-text('立即登录')"}],
    }, download_dir=str(tmp_path))
    runner.pages = [page]
    runner._page_errors.append({
        **_gateway("https://pm.gd.csg.cn/portal/xhr/sso-auth/login"),
        "_page": page,
        "page_url": page.url,
    })
    with pytest.raises(WorkflowPaused, match="不要刷新当前文档"):
        await runner._maybe_recover(0, stage="after")
    assert page.reloads == 0
    assert page.clicks == []


async def test_unreadable_token_is_not_authenticated(tmp_path):
    from unittest.mock import AsyncMock

    page = _AuthPage()
    page.evaluate = AsyncMock(side_effect=RuntimeError('document changed'))
    runner = WorkflowRunner({'steps': []}, download_dir=str(tmp_path))
    runner.pages = [page]
    assert await runner._missing_storage_keys(['pfxh-token']) == ['pfxh-token']
    session = SharedBrowser()
    page.evaluate = AsyncMock(return_value=[])
    assert await session._missing_storage_keys(page, ['pfxh-token']) == ['pfxh-token']


def test_verified_login_executes_and_indicators_only_reuse_session():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    directory = root / '参考/workflows_export_已验证单个运行通过'
    configs = [yaml.safe_load(path.read_text(encoding='utf-8')) for path in directory.glob('*.yml')]
    assert len(configs) == 5
    login = next(cfg for cfg in configs if cfg['name'] == '登陆流程测试')
    assert login['type'] == 'login'
    assert login['require_storage_keys'] == ['pfxh-token']
    assert [step['action'] for step in login['steps']] == [
        'login', 'click', 'wait', 'click', 'wait', 'fill', 'captcha',
        'wait', 'check', 'click', 'dismiss_modal', 'ensure_login',
    ]
    assert [step['ms'] for step in login['steps'] if step.get('ms')] == [2000, 1000, 800]
    assert login['steps'][0]['url'] == 'https://pm.gd.csg.cn/portal/#/home'
    assert login['steps'][1]['selector'] == "button:has-text('登 录')"
    assert login['steps'][3]['selector'] == 'text=证书登录'
    assert '${CERT_PASSWORD}' in login['steps'][5]['value']
    captcha = login['steps'][6]
    assert 'submit_selector' not in captcha
    assert captcha['pre_refresh'] == 4
    assert login['steps'][9]['selector'] == "button:has-text('立即登录')"
    assert login['steps'][10]['report_error'] is True
    assert login['steps'][11]['require_storage_keys'] == ['pfxh-token']
    assert not any(step['action'] == 'reset_site_storage' for step in login['steps'])
    disk_copy = (root / 'workflows/login_test.yml').read_text(encoding='utf-8')
    assert disk_copy == (directory / '登陆流程测试.yml').read_text(encoding='utf-8')

    expected_dates = {
        '结算管理-日清算概览导出': ['yesterday'],
        '日前节点电价查询导出': ['-1d'],
        '实时节点电价查询导出': ['-1d'],
        '现货交易-完整采集(含披露中心)': ['today', 'today', 'today'],
    }
    expected_waits = {
        '结算管理-日清算概览导出': [2000, 800, 800, 2000, 3000],
        '日前节点电价查询导出': [2000, 500, 2000, 2000],
        '实时节点电价查询导出': [2000, 500, 2000, 2000],
        '现货交易-完整采集(含披露中心)': [
            2000, 2000, 2000, 1000, 2000, 1000, 2000, 500, 2000, 500, 2000, 500, 2000,
        ],
    }
    indicators = [cfg for cfg in configs if cfg['type'] == 'indicator']
    assert len(indicators) == 4
    for cfg in indicators:
        rendered = yaml.safe_dump(cfg, allow_unicode=True)
        assert cfg['auth_policy'] == 'manual'
        assert cfg['steps'][0]['action'] == 'ensure_login'
        assert cfg['steps'][0]['require_storage_keys'] == ['pfxh-token']
        assert not cfg.get('api_fallback', {}).get('enabled', False)
        assert not any(step['action'] in ('login', 'navigate', 'reset_site_storage', 'captcha', 'fill')
                       for step in cfg['steps'])
        assert '#/powerEnterprise' not in rendered
        assert cfg['steps'][1]['expect'] == 'new_tab'
        assert cfg['max_page_recoveries'] == 1
        assert [step['value'] for step in cfg['steps'] if step['action'] == 'pick_date'] == expected_dates[cfg['name']]
        assert [step['ms'] for step in cfg['steps'] if step.get('ms')] == expected_waits[cfg['name']]
