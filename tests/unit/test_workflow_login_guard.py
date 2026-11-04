"""登录后置条件测试：半登录状态不能算成功。

现场现象：点了「立即登录」后没有任何跳转，刷新得到白屏；
localStorage 里只剩 pfxh-userInfo，没有 pfxh-token。
旧逻辑只要看到 .el-menu 就判成功，于是把这种半登录状态当成了登录完成。
"""

from __future__ import annotations

import base64

import pytest
from workflow_runner import WorkflowExecutionError, WorkflowRunner

from tests.dom_probe_fakes import DomProbeFakePage


class _FakeLocator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> _FakeLocator:
        return self

    async def screenshot(self, **_kwargs) -> bytes:
        return b"fake-png"

    async def is_visible(self, **_kwargs) -> bool:
        return self._selector in self._page.visible

    async def wait_for(self, **kwargs) -> None:
        if self._selector not in self._page.visible:
            raise TimeoutError(f"{self._selector} not visible")
        if kwargs.get("state") == "visible":
            return None
        return None

    async def inner_text(self, **_kwargs) -> str:
        return self._page.texts.get(self._selector, "")

    async def click(self, **_kwargs) -> None:
        self._page.clicked.append(self._selector)

    async def press(self, key: str, **_kwargs) -> None:
        self._page.clicked.append(f"press:{self._selector}:{key}")

    async def press_sequentially(self, text: str, **_kwargs) -> None:
        self._page.clicked.append(f"type:{self._selector}:{text}")


class _FakePage:
    def __init__(self, storage: dict[str, str], *, menu_visible: bool = True) -> None:
        self.storage = dict(storage)
        self.visible: dict[str, bool] = {}
        if menu_visible:
            self.visible[".el-menu"] = True
        self.texts: dict[str, str] = {}
        self.clicked: list[str] = []
        self.reloads = 0
        self.screenshots = 0

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def click(self, selector: str, **_kwargs) -> None:
        self.clicked.append(selector)

    async def fill(self, selector: str, value: str) -> None:
        self.clicked.append(f"fill:{selector}")

    async def evaluate(self, script: str, arg=None):
        if "localStorage.getItem" in script:
            if ".length" in script:
                key = "pfxh-token" if "pfxh-token" in script else "pfxh-userInfo"
                return len(self.storage.get(key) or "")
            return [[key, self.storage.get(key, "")] for key in (arg or [])]
        if "removeItem" in script and isinstance(arg, str):
            self.storage.pop(arg, None)
            return None
        if isinstance(arg, str):
            return None
        return list(self.storage)

    async def reload(self, **_kwargs) -> None:
        self.reloads += 1

    async def screenshot(self, **_kwargs) -> None:
        self.screenshots += 1

    async def title(self) -> str:
        return "mock"


class _FixedCaptchaSolver:
    def recognize(self, _image_bytes: bytes) -> str:
        return "1234"


@pytest.fixture(autouse=True)
def _patch_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    monkeypatch.setattr(captcha_solver, "CaptchaSolver", _FixedCaptchaSolver)


_ENSURE_LOGIN_STEP = {
    "action": "ensure_login",
    "success_selector": ".el-menu",
    "require_storage_keys": ["pfxh-token"],
}


async def test_workbench_without_local_storage_token_is_reported_as_success(tmp_path):
    """工作台可见且无错误时，不用 localStorage token 阻止登录。"""
    page = _FakePage({"pfxh-userInfo": '{"screenName":"范智灏"}', "pfxh-token": ""})
    cfg = {"name": "登陆流程测试", "type": "login", "steps": [_ENSURE_LOGIN_STEP]}
    runner = WorkflowRunner(cfg, download_dir=str(tmp_path))
    runner.pages = [page]
    await runner._do_ensure_login(_ENSURE_LOGIN_STEP)
    assert page.reloads == 0


async def test_complete_login_state_is_reported_as_success(tmp_path):
    page = _FakePage({"pfxh-userInfo": "{}", "pfxh-token": "17c1f3a6-3499-48fc"})
    cfg = {"name": "登陆流程测试", "type": "login", "steps": [_ENSURE_LOGIN_STEP]}
    runner = WorkflowRunner(cfg, download_dir=str(tmp_path))
    runner.pages = [page]
    await runner._do_ensure_login(_ENSURE_LOGIN_STEP)

    assert page.reloads == 0


async def test_login_error_toast_is_surfaced_and_pauses(tmp_path):
    """验证码错误/证书失败可能是 toast 而不是弹窗，提示必须带出来。"""
    page = _FakePage({"pfxh-token": "present"})
    page.visible[".ant-message-error"] = True
    page.texts[".ant-message-error"] = "验证码错误，请重新输入"
    cfg = {"name": "登陆流程测试", "type": "login", "steps": [_ENSURE_LOGIN_STEP]}
    runner = WorkflowRunner(cfg, download_dir=str(tmp_path))
    runner.pages = [page]
    async def stop(reason: str) -> None:
        raise WorkflowExecutionError(reason)

    runner._pause_and_wait = stop

    with pytest.raises(WorkflowExecutionError, match="验证码错误，请重新输入"):
        await runner._do_ensure_login(_ENSURE_LOGIN_STEP)


class _CaptchaProbePage(DomProbeFakePage):
    """验证码页：只开放探测与鼠标/键盘，和改造后的实现一致。"""

    SRC = "data:image/png;base64," + base64.b64encode(b"captcha-png").decode("ascii")

    def __init__(self) -> None:
        super().__init__()
        self.clicked: list[str] = []

    def probe_visible(self, selector: str, index: int | None = None) -> bool:
        return selector in ("img[src^='data:image']", "#validateOnly_verifyCode")

    def probe_attr(self, selector: str, name: str) -> str | None:
        if selector == "img[src^='data:image']" and name == "src":
            return self.SRC
        return None

    def apply_click(self, selector: str, _index: int | None) -> None:
        self.clicked.append(selector)


async def test_captcha_refreshes_and_fills_without_submitting(monkeypatch, tmp_path):
    import workflow_runner as workflow_runner_module

    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow_runner_module.asyncio, "sleep", no_sleep)
    page = _CaptchaProbePage()
    runner = WorkflowRunner({"name": "验证码测试", "steps": []}, download_dir=str(tmp_path))
    runner.pages = [page]
    await runner._do_captcha({
        "captcha_img": "img[src^='data:image']",
        "captcha_input": "#validateOnly_verifyCode",
        "pre_refresh": 4,
        "pre_refresh_interval_ms": 0,
    })
    # 四次刷新，然后点进输入框逐键输入；不走 page.fill，也不点登录。
    assert page.clicked == ["img[src^='data:image']"] * 4 + ["#validateOnly_verifyCode"]
    assert page.typed == ["1234"]


class _FakeCdpSession:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.detached = False

    async def send(self, method: str, params: dict) -> None:
        self.sent.append((method, params))

    async def detach(self) -> None:
        self.detached = True


class _FakeContext:
    def __init__(self) -> None:
        self.session = _FakeCdpSession()

    async def new_cdp_session(self, _page):
        return self.session


async def test_reset_site_storage_clears_origin_state(tmp_path):
    """白屏/半登录的人工解法（清本地状态 + 刷新）要能作为步骤执行。"""
    page = _FakePage({"pfxh-userInfo": "{}", "pfxh-token": "old", "keep-me": "1"})
    page.url = "https://pm.gd.csg.cn/portal/#/home"
    context = _FakeContext()
    runner = WorkflowRunner({"name": "登陆流程测试", "type": "login", "steps": []},
                            download_dir=str(tmp_path))
    runner.pages = [page]
    runner.context = context

    await runner._do_reset_site_storage({"action": "reset_site_storage", "keep_keys": ["keep-me"]})

    assert page.storage == {"keep-me": "1"}, "keep_keys 之外的本站点数据应被清掉"
    method, params = context.session.sent[0]
    assert method == "Storage.clearDataForOrigin"
    assert params["origin"] == "https://pm.gd.csg.cn"
    assert "cookies" in params["storageTypes"]
    assert context.session.detached is True
    assert page.reloads == 1


def test_page_recovery_can_be_disabled_by_config(tmp_path):
    disabled = WorkflowRunner({"page_recovery": "off"}, download_dir=str(tmp_path))
    assert disabled._page_recovery_enabled is False
    assert WorkflowRunner({}, download_dir=str(tmp_path))._page_recovery_enabled is True
    limited = WorkflowRunner({"max_page_recoveries": "1"}, download_dir=str(tmp_path))
    assert limited._max_recoveries == 1


async def test_login_pause_separates_gateway_rejection_from_business_error(tmp_path):
    """暂停原因要能区分“网关拦截（text/html 400）”和“业务报错（JSON）”。

    现场那次登录就是只有“点了没反应”，没有错误文本；只有把登录阶段的请求
    连同 content-type 一起记下来，才能判断是业务拒绝还是被网关挡掉。
    """
    page = _FakePage({"pfxh-userInfo": '{"screenName":"x"}', "pfxh-token": ""})
    page.url = "https://pm.gd.csg.cn/portal/#/user/login"
    runner = WorkflowRunner({"name": "登陆流程测试", "type": "login", "steps": []},
                            download_dir=str(tmp_path))
    runner.pages = [page]
    events: list[dict] = []
    runner._on_event = events.append
    runner._auth_traffic.extend([
        {
            "at": "16:53:06", "method": "POST", "status": 200,
            "content_type": "application/json", "path": "/portal/xhr/sso-auth/verify/validCaptcha",
            "url": "https://pm.gd.csg.cn/portal/xhr/sso-auth/verify/validCaptcha",
        },
        {
            "at": "16:53:07", "method": "POST", "status": 400,
            "content_type": "text/html", "path": "/portal/xhr/sso-auth/login",
            "url": "https://pm.gd.csg.cn/portal/xhr/sso-auth/login",
        },
    ])

    with pytest.raises(WorkflowExecutionError, match="疑似网关拦截"):
        await runner._pause_login_error("登录提交后未进入工作台")

    pause = next(event for event in events if event["event"] == "error_pause")
    assert "validCaptcha" in pause["auth_traffic"]
    assert "业务响应" in pause["auth_traffic"]
    assert "text/html" in pause["auth_traffic"]
    assert "疑似网关拦截" in pause["auth_traffic"]

    diagnostics = list((tmp_path / "_failures").glob("login_diagnostics_*.log"))
    assert diagnostics, "登录失败应落盘诊断文件"
    text = diagnostics[0].read_text(encoding="utf-8")
    assert "pfxh-token length: 0" in text
    assert "HTTP 400 text/html" in text


class _Tab:
    def __init__(self, url: str) -> None:
        self.url = url
        self.reloads = 0
        self.closed = False
        self.front = 0

    def is_closed(self) -> bool:
        return self.closed

    def on(self, *_args, **_kwargs) -> None:
        return None

    async def reload(self, **_kwargs) -> None:
        self.reloads += 1

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.front += 1

    async def title(self) -> str:
        return "mock"

    async def evaluate(self, *_args, **_kwargs):
        return []


def _gateway_error(url: str) -> dict:
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


async def test_child_tab_400_replays_new_tab_click_without_reloading_parent(tmp_path):
    """子标签 400：关掉子标签，从 new_tab 点击重放，不刷新父页，也不关保护页。"""
    parent = _Tab("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    kept = _Tab("https://example.com/user-tab")
    child = _Tab("https://pm.gd.csg.cn/pfxh/stc/stale")
    runner = WorkflowRunner({
        "name": "子标签400",
        "steps": [
            {"action": "click", "selector": "text=现货交易", "expect": "new_tab"},
            {"action": "click", "selector": "#price"},
        ],
    }, download_dir=str(tmp_path))
    runner.pages = [parent, kept, child]
    runner._protected_pages = {parent, kept}
    runner._current_page_idx = 2
    runner._page_errors.append(_gateway_error(
        "https://pm.gd.csg.cn/pfxh/qctc-pm-trade-market-out-zzzx/daNodePriceQuery/getJddjTree"
    ))

    runner._page_errors[-1]["_page"] = child
    runner._tab_checkpoints[child] = (0, parent)
    from unittest.mock import AsyncMock, MagicMock
    parent.locator = MagicMock()
    parent.locator.return_value.first.is_visible = AsyncMock(return_value=True)

    recovered = await runner._maybe_recover(1, stage="after")

    assert recovered is True
    assert runner._replay_index == 0
    assert child.closed is True
    assert parent.closed is False
    assert parent.reloads == 0
    assert kept.closed is False
    assert kept.reloads == 0
    assert runner.pages[0] is parent
    assert runner._current_page_idx == 0


async def test_binding_page_400_does_not_reload(tmp_path):
    """绑定页自己 400、没有可关的新标签：不 reload，失败并说明不要刷新。"""
    page = _Tab("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner = WorkflowRunner({
        "name": "绑定页400",
        "steps": [{"action": "click", "selector": "#stay"}],
    }, download_dir=str(tmp_path))
    runner.pages = [page]
    runner._protected_pages = {page}
    runner._page_errors.append(_gateway_error(
        "https://pm.gd.csg.cn/pfxh/qctc-pm-trade-market-out-zzzx/daNodePriceQuery/getJddjTree"
    ))
    events: list[dict] = []

    await runner._execute_steps(events.append, None)

    assert page.reloads == 0
    assert page.closed is False
    assert runner.failed is True
    assert "现货交易" in runner.failure_reason
    assert "不要刷新" in runner.failure_reason
    assert not any(event["event"] == "page_recovery" for event in events)


async def test_single_step_400_reports_endpoint_without_mislabeling_portal(tmp_path):
    """绑定现货页单步选日期时，门户是正常打开的页，不是可重放检查点。"""
    from unittest.mock import MagicMock

    spot = _Tab("https://pm.gd.csg.cn/pfxh/stc/index")
    portal = _Tab("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    context = MagicMock()
    context.pages = [spot, portal]
    spot.context = portal.context = context
    endpoint = "https://pm.gd.csg.cn/pfxh/qctc-pm-trade-market-out-plzx/DaMarkjsc/homeXhMarketAllInfo"
    runner = WorkflowRunner({
        "name": "日期单步", "steps": [{"action": "pick_date", "selector": ".dateBox input", "value": "-1d"}],
    }, download_dir=str(tmp_path))
    runner.pages = [spot]
    runner.context = context
    runner._protected_pages = {spot, portal}
    runner._page_errors.append({**_gateway_error(endpoint), "_page": spot, "page_url": spot.url})
    events: list[dict] = []
    runner._on_event = events.append

    with pytest.raises(WorkflowExecutionError, match="检查点不是"):
        await runner._maybe_recover(0, stage="after")

    rejection = next(event for event in events if event["event"] == "gateway_rejected")
    assert "homeXhMarketAllInfo" in rejection["message"]
    assert not any(event["event"] == "stale_tabs_warning" for event in events)
    assert portal.closed is False


async def test_same_origin_old_tab_blocks_replay(tmp_path):
    """同域旧标签还在时不重放，也不关闭那些页。"""
    parent = _Tab("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    old = _Tab("https://pm.gd.csg.cn/pfxh/stc/check")
    child = _Tab("https://pm.gd.csg.cn/pfxh/stc/stale")
    runner = WorkflowRunner({
        "name": "旧标签",
        "steps": [
            {"action": "click", "selector": "text=现货交易", "expect": "new_tab"},
            {"action": "click", "selector": "#price"},
        ],
    }, download_dir=str(tmp_path))
    runner.pages = [parent, old, child]
    runner._protected_pages = {parent, old}
    runner._current_page_idx = 2
    runner._page_errors.append(_gateway_error(
        "https://pm.gd.csg.cn/pfxh/qctc-pm-trade-market-out-zzzx/daNodePriceQuery/getJddjTree"
    ))
    events: list[dict] = []
    runner._on_event = events.append

    runner._page_errors[-1]["_page"] = child
    runner._tab_checkpoints[child] = (0, parent)
    with pytest.raises(WorkflowExecutionError, match="同域旧标签"):
        await runner._maybe_recover(1, stage="after")

    assert parent.reloads == 0
    assert parent.closed is False
    assert old.closed is False
    assert child.closed is False
    warning = next(event for event in events if event["event"] == "stale_tabs_warning")
    assert old.url in warning["pages"]
    assert any(event["event"] == "error_pause" for event in events)
    assert not any(event["event"] == "page_recovery" for event in events)
