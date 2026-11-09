"""SharedBrowser 单元测试（不依赖真实浏览器）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spider_gd.core.crawler.shared_browser import (
    GD_LOGIN_SUCCESS_SELECTOR,
    PAGE_LOAD_FAILED_MESSAGE,
    SharedBrowser,
    get_shared_browser,
    is_browser_error_page,
    startup_url,
)


def test_automated_download_does_not_create_managed_duplicate():
    session = SharedBrowser()
    page = MagicMock()
    session._active_runner = object()
    session._track_managed_downloads(page)
    handler = page.on.call_args.args[1]
    handler(MagicMock())
    assert not session._managed_download_tasks


@pytest.mark.parametrize(("enabled", "expected"), [(0, True), (1, False)])
def test_windows_uac_disabled(enabled, expected):
    import spider_gd.core.crawler.shared_browser as mod

    registry = MagicMock()
    registry.QueryValueEx.return_value = (enabled, 4)
    with patch.object(mod.sys, "platform", "win32"), patch.dict("sys.modules", winreg=registry):
        assert mod._windows_uac_disabled() is expected


def test_windows_uac_registry_unavailable():
    import spider_gd.core.crawler.shared_browser as mod

    registry = MagicMock()
    registry.OpenKey.side_effect = OSError("unavailable")
    with patch.object(mod.sys, "platform", "win32"), patch.dict("sys.modules", winreg=registry):
        assert mod._windows_uac_disabled() is False


def test_allocate_cdp_port_is_nonzero_and_available():
    import socket

    from spider_gd.core.crawler.shared_browser import _allocate_cdp_port

    port = _allocate_cdp_port()
    assert 0 < port < 65536
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_build_launch_args_rejects_invalid_cdp_port(tmp_path, port):
    from spider_gd.core.crawler.shared_browser import _build_launch_args

    with pytest.raises(ValueError, match="CDP"):
        _build_launch_args("chrome.exe", tmp_path, headless=False, cdp_port=port)


@pytest.mark.parametrize(
    ("uac_disabled", "integrity", "expect_in_hint"),
    [
        (True, 0x2000, "UAC"),  # UAC 关闭
        (False, 0x1000, "完整性"),  # Low 完整性
        (False, 0x2000, ""),  # 正常环境 → 无额外线索
        (False, None, ""),
    ],
)
def test_chrome_exit_hint(uac_disabled, integrity, expect_in_hint):
    import spider_gd.core.crawler.shared_browser as mod

    with (
        patch.object(mod.sys, "platform", "win32"),
        patch.object(mod, "_windows_uac_disabled", return_value=uac_disabled),
        patch.object(mod, "_windows_process_integrity", return_value=integrity),
    ):
        hint = mod._chrome_exit_hint()
    if expect_in_hint:
        assert expect_in_hint in hint
    else:
        assert hint == ""


def test_chrome_exit_hint_non_windows():
    import spider_gd.core.crawler.shared_browser as mod

    with patch.object(mod.sys, "platform", "linux"):
        assert mod._chrome_exit_hint() == ""


def test_build_launch_args_is_plain_chrome(tmp_path):
    """普通方式启动本机 Chrome：不带任何沙箱开关。"""
    from spider_gd.core.crawler.shared_browser import _build_launch_args

    args = _build_launch_args("chrome.exe", tmp_path, headless=False, cdp_port=59812)
    assert args[0] == "chrome.exe"
    assert not any("sandbox" in a.lower() for a in args)
    assert f"--user-data-dir={tmp_path}" in args
    assert "--remote-debugging-port=59812" in args
    assert "--remote-debugging-port=0" not in args
    assert "--enable-automation" not in args
    assert not any(a.startswith("--headless") for a in args)


def test_build_launch_args_supports_headless(tmp_path):
    from spider_gd.core.crawler.shared_browser import _build_launch_args

    args = _build_launch_args("chrome.exe", tmp_path, headless=True, cdp_port=59812)
    assert not any("sandbox" in a.lower() for a in args)
    assert any(a.startswith("--headless") for a in args)


def test_describe_exit_explains_status_breakpoint():
    import spider_gd.core.crawler.shared_browser as mod

    with patch.object(mod, "_chrome_exit_hint", return_value="完整性级别过低"):
        msg = mod.SharedBrowser._describe_exit("Chrome 进程提前退出", 2147483651, "boom")
    assert "0x80000003" in msg
    assert "完整性级别过低" in msg
    assert "boom" in msg


def test_describe_exit_plain_code():
    from spider_gd.core.crawler.shared_browser import SharedBrowser

    msg = SharedBrowser._describe_exit("Chrome 进程提前退出", 3221225477, "")
    assert "0xC0000005" in msg
    assert "沙箱" not in msg


class TestSharedBrowserInit:
    def test_initial_state_is_not_running(self):
        sb = SharedBrowser()
        assert not sb.is_running
        assert sb.status()["running"] is False
        assert sb.status()["page_count"] == 0
        assert sb.status()["current_url"] == ""

    def test_inject_into_raises_when_not_running(self):
        sb = SharedBrowser()
        runner = MagicMock()
        with pytest.raises(RuntimeError, match="未启动"):
            sb.inject_into(runner)


class TestSharedBrowserStatusWithMock:
    def test_status_returns_current_url(self):
        sb = SharedBrowser()
        # 模拟运行状态
        mock_page = MagicMock()
        mock_page.url = "https://example.com/test"
        sb._pages = [mock_page]
        sb._current_page_idx = 0
        sb._browser = MagicMock()  # 标记为"运行中"

        status = sb.status()
        assert status["running"] is True
        assert status["page_count"] == 1
        assert status["current_url"] == "https://example.com/test"

    def test_status_catches_url_exceptions(self):
        sb = SharedBrowser()
        mock_page = MagicMock()
        type(mock_page).url = property(MagicMock(side_effect=RuntimeError("page closed")))
        sb._pages = [mock_page]
        sb._browser = MagicMock()

        status = sb.status()
        assert status["current_url"] == ""


def test_startup_url_does_not_duplicate_portal_hash():
    doubled = "https://pm.gd.csg.cn/portal/#/home/portal/#/home"
    opened = startup_url("https://pm.gd.csg.cn/portal/#/home")
    assert opened == "https://pm.gd.csg.cn/portal/#/home"
    assert doubled not in opened
    assert "#/home/portal/#/home" not in opened
    assert startup_url("https://pm.gd.csg.cn") == "https://pm.gd.csg.cn/portal/#/home"


def test_configure_keeps_only_origin():
    browser = SharedBrowser()
    browser.configure(target_origin="https://pm.gd.csg.cn/portal/#/home")
    assert browser.status()["config"]["target_origin"] == "https://pm.gd.csg.cn"
    assert browser.status()["config"]["auth_policy"] == "reuse_only"


async def test_portal_workbench_is_recognized_without_element_menu():
    """门户工作台用 Ant 菜单；现货标签才用 Element 菜单。"""
    from playwright.async_api import async_playwright

    browser_session = SharedBrowser()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.route("https://pm.gd.csg.cn/**", lambda route: route.fulfill(
            body='<ul class="ant-menu ant-menu-horizontal"><li>首页</li></ul>'
                 '<div class="titleName___SP9tK">现货交易</div>',
            content_type="text/html; charset=utf-8",
        ))
        await page.goto("https://pm.gd.csg.cn/portal/#/powerEnterprise")
        await page.evaluate("localStorage.setItem('pfxh-token', 'test-token')")
        assert await page.locator(".el-menu").count() == 0
        auth = await browser_session.check_auth(page, require_storage_keys=["pfxh-token"])
        assert auth["state"] == "authenticated"
        await page.evaluate("localStorage.removeItem('pfxh-token')")
        auth = await browser_session.check_auth(page)
        assert auth["state"] == "authenticated"
        await browser.close()


@pytest.mark.parametrize("token", ["", "stale-token"])
async def test_public_portal_home_is_not_authenticated(token):
    """10281545/02：首页也有 Ant 菜单，旧 token 不能使登录表单被跳过。"""
    from playwright.async_api import async_playwright

    session = SharedBrowser()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("https://pm.gd.csg.cn/**", lambda route: route.fulfill(
            body='<ul class="ant-menu ant-menu-horizontal"><li>首页</li></ul>'
                 '<button onclick="window.loginClicked=true">登 录</button>',
            content_type="text/html; charset=utf-8",
        ))
        await page.goto("https://pm.gd.csg.cn/portal/#/home")
        if token:
            await page.evaluate("token => localStorage.setItem('pfxh-token', token)", token)
        auth = await session.check_auth(page)
        assert auth["state"] == "unauthenticated", auth
        await browser.close()


async def test_explicit_login_click_runs_on_public_home_with_stale_token(tmp_path):
    from playwright.async_api import async_playwright
    from workflow_runner import WorkflowRunner

    session = SharedBrowser()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("https://pm.gd.csg.cn/**", lambda route: route.fulfill(
            body='<ul class="ant-menu ant-menu-horizontal"><li>首页</li></ul>'
                 "<button onclick=\"window.loginClicked=true;location.hash='/powerEnterprise';"
                 "this.textContent='现货交易'\">登 录</button>",
            content_type="text/html; charset=utf-8",
        ))
        await page.goto("https://pm.gd.csg.cn/portal/#/home")
        await page.evaluate("localStorage.setItem('pfxh-token', 'stale-token')")
        session._browser = browser
        session._pages = [page]
        runner = WorkflowRunner({
            "name": "登陆流程测试", "type": "login", "needs_login": True,
            "steps": [{"action": "click", "selector": "button:has-text('登 录')"}],
        }, download_dir=str(tmp_path))
        events = []
        await runner.run(on_event=events.append, session=session)
        assert await page.evaluate("window.loginClicked === true"), events
        await browser.close()


def test_saved_legacy_portal_selector_is_migrated(tmp_path):
    import json

    path = tmp_path / "browser-session.json"
    path.write_text(json.dumps({"config": {
        "target_origin": "https://pm.gd.csg.cn", "success_selector": ".el-menu",
    }}), encoding="utf-8")
    browser = SharedBrowser(config_path=path)
    assert browser.status()["config"]["success_selector"] == GD_LOGIN_SUCCESS_SELECTOR


async def test_ensure_connected_starts_from_normalized_origin():
    browser = SharedBrowser()
    browser._config["mode"] = "managed"
    browser._config["target_origin"] = "https://pm.gd.csg.cn/portal/#/home"
    seen: list[str] = []

    async def fake_start(url: str = "") -> None:
        seen.append(url)

    browser.start = fake_start  # type: ignore[method-assign]
    await browser.ensure_connected()
    assert seen == ["https://pm.gd.csg.cn/portal/#/home"]
    assert "#/home/portal/#/home" not in seen[0]


async def test_connect_retries_without_download_override_when_chrome_rejects_it():
    session = SharedBrowser()
    temporary = MagicMock()
    temporary.contexts = [MagicMock(pages=[])]
    temporary.contexts[0].new_page = AsyncMock()
    temporary.close = AsyncMock()
    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.contexts = [MagicMock(pages=[])]
    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(side_effect=[
        RuntimeError("Protocol error (Browser.setDownloadBehavior): Browser context management is not supported."),
        temporary,
        browser,
    ])
    manager = MagicMock()
    manager.start = AsyncMock(return_value=playwright)
    with patch("playwright.async_api.async_playwright", return_value=manager):
        await session._connect("ws://127.0.0.1:62584/devtools/browser/example")
    assert session.is_running
    temporary.contexts[0].new_page.assert_awaited_once()
    temporary.close.assert_awaited_once()
    assert playwright.chromium.connect_over_cdp.await_count == 3
    assert playwright.chromium.connect_over_cdp.await_args_list[1].kwargs["no_defaults"] is True
    assert "no_defaults" not in playwright.chromium.connect_over_cdp.await_args_list[2].kwargs


def _open_page(url: str) -> MagicMock:
    page = MagicMock()
    page.is_closed.return_value = False
    page.url = url
    page.context = MagicMock()
    return page


def _running_browser() -> SharedBrowser:
    browser = SharedBrowser()
    browser._browser = MagicMock()
    browser._browser.is_connected.return_value = True
    browser._macos_active_tab_urls = AsyncMock(return_value=set())  # type: ignore[method-assign]

    async def fake_list() -> list[dict]:
        return []

    browser.list_pages = fake_list  # type: ignore[method-assign]
    return browser


async def test_follow_active_page_uses_actual_chrome_tab_when_both_pages_claim_focus():
    browser = _running_browser()
    notice = _open_page("https://pm.gd.csg.cn/portal/#/home/informationNotice?id=31")
    home = _open_page("https://pm.gd.csg.cn/portal/#/home")
    browser._page_ids = {notice: "notice", home: "home"}
    browser._pages = [notice]
    browser._macos_active_tab_urls = AsyncMock(return_value={home.url})  # type: ignore[method-assign]

    async def both_focused(_page: MagicMock) -> tuple[bool, bool]:
        return True, True

    browser._tab_focus = both_focused  # type: ignore[method-assign]
    with patch("spider_gd.core.crawler.shared_browser.sys.platform", "darwin"):
        await browser.follow_active_page()
    assert browser.page is home
    assert browser.status()["current_url"] == home.url


async def test_follow_active_page_binds_visible_tab_when_chrome_is_in_background():
    browser = _running_browser()
    hidden = _open_page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    shown = _open_page("https://pm.gd.csg.cn/pfxh/stc/index")
    browser._page_ids = {hidden: "h", shown: "s"}
    browser._pages = [hidden]
    browser._context = hidden.context

    async def focus(page: MagicMock) -> tuple[bool, bool]:
        return (page is shown, False)

    browser._tab_focus = focus  # type: ignore[method-assign]
    await browser.follow_active_page()
    assert browser.page is shown
    assert browser.status()["current_url"] == shown.url


async def test_follow_active_page_keeps_binding_when_two_windows_are_visible():
    browser = _running_browser()
    first = _open_page("https://pm.gd.csg.cn/portal/#/home")
    second = _open_page("https://pm.gd.csg.cn/pfxh/stc/index")
    browser._page_ids = {first: "a", second: "b"}
    browser._pages = [first]
    browser._context = first.context

    async def focus(_page: MagicMock) -> tuple[bool, bool]:
        return True, False

    browser._tab_focus = focus  # type: ignore[method-assign]
    await browser.follow_active_page()
    assert browser.page is first


async def test_follow_active_page_prefers_the_focused_window():
    browser = _running_browser()
    background = _open_page("https://pm.gd.csg.cn/portal/#/home")
    foreground = _open_page("https://pm.gd.csg.cn/pfxh/stc/index")
    browser._page_ids = {background: "a", foreground: "b"}
    browser._pages = [background]

    async def focus(page: MagicMock) -> tuple[bool, bool]:
        return True, page is foreground

    browser._tab_focus = focus  # type: ignore[method-assign]
    await browser.follow_active_page()
    assert browser.page is foreground


async def test_follow_active_page_does_not_move_while_a_run_holds_the_browser():
    browser = _running_browser()
    bound = _open_page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    other = _open_page("https://pm.gd.csg.cn/pfxh/stc/index")
    browser._page_ids = {bound: "a", other: "b"}
    browser._pages = [bound]
    calls = 0

    async def focus(_page: MagicMock) -> tuple[bool, bool]:
        nonlocal calls
        calls += 1
        return True, False

    browser._tab_focus = focus  # type: ignore[method-assign]
    await browser._execution_lock.acquire()
    try:
        await browser.follow_active_page()
    finally:
        browser._execution_lock.release()
    assert calls == 0
    assert browser.page is bound


async def test_follow_active_page_ignores_non_http_tabs():
    browser = _running_browser()
    blank = _open_page("about:blank")
    business = _open_page("https://pm.gd.csg.cn/portal/#/home")
    browser._page_ids = {blank: "a", business: "b"}
    seen: list[str] = []

    async def focus(page: MagicMock) -> tuple[bool, bool]:
        seen.append(page.url)
        return True, False

    browser._tab_focus = focus  # type: ignore[method-assign]
    await browser.follow_active_page()
    assert seen == [business.url]
    assert browser.page is business


async def test_follow_active_page_uses_newest_business_tab_when_all_unfocused():
    """浏览器在后台时没有“当前标签”，用最右侧（最新）的业务页，避开旧文档签名。"""
    browser = _running_browser()
    portal = _open_page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    spot_old = _open_page("https://pm.gd.csg.cn/pfxh/stc/index")
    spot_new = _open_page("https://pm.gd.csg.cn/pfxh/stc/check")
    browser._page_ids = {portal: "a", spot_old: "b", spot_new: "c"}

    async def focus(_page: MagicMock) -> tuple[bool, bool]:
        return False, False

    browser._tab_focus = focus  # type: ignore[method-assign]
    await browser.follow_active_page()
    assert browser.page is spot_new


async def test_follow_active_page_keeps_binding_when_newest_rule_is_off():
    browser = _running_browser()
    portal = _open_page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    spot = _open_page("https://pm.gd.csg.cn/pfxh/stc/index")
    browser._page_ids = {portal: "a", spot: "b"}
    browser._pages = [portal]
    browser._config["follow_newest_tab"] = "false"

    async def focus(_page: MagicMock) -> tuple[bool, bool]:
        return False, False

    browser._tab_focus = focus  # type: ignore[method-assign]
    await browser.follow_active_page()
    assert browser.page is portal


def _reset_page(url: str) -> MagicMock:
    """可被 reset_cookies_and_reload 直接驱动的假页：CDP 与 reload 都是异步的。"""
    page = _open_page(url)
    page.reload = AsyncMock()
    cdp = AsyncMock()
    page.context.new_cdp_session = AsyncMock(return_value=cdp)
    page.context.clear_cookies = AsyncMock()
    page.cdp = cdp
    return page


async def test_reset_cookies_and_reload_uses_cdp_then_reloads():
    browser = _running_browser()
    page = _reset_page("https://pm.gd.csg.cn/pfxh/stc/index?tab=1")
    browser._pages = [page]

    result = await browser.reset_cookies_and_reload()

    page.cdp.send.assert_awaited_once_with(
        "Storage.clearDataForOrigin",
        {"origin": "https://pm.gd.csg.cn", "storageTypes": "cookies"},
    )
    page.cdp.detach.assert_awaited_once()
    page.context.clear_cookies.assert_not_awaited()
    page.reload.assert_awaited_once_with(wait_until="domcontentloaded", timeout=30000)
    assert result["origin"] == "https://pm.gd.csg.cn"
    assert result["cleared_by"] == "cdp"


async def test_reset_cookies_and_reload_falls_back_to_context_clear():
    browser = _running_browser()
    page = _reset_page("https://pm.gd.csg.cn/portal/#/home")
    page.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("no cdp"))
    browser._pages = [page]

    result = await browser.reset_cookies_and_reload()

    page.context.clear_cookies.assert_awaited_once()
    page.reload.assert_awaited_once()
    assert result["cleared_by"] == "context"


@pytest.mark.parametrize("url", ["", "about:blank", "chrome://new-tab-page"])
async def test_reset_cookies_and_reload_rejects_non_web_page(url):
    browser = _running_browser()
    page = _reset_page(url)
    browser._pages = [page]

    with pytest.raises(RuntimeError, match="不是网页"):
        await browser.reset_cookies_and_reload()
    page.reload.assert_not_awaited()


async def test_reset_cookies_and_reload_refuses_while_task_holds_browser():
    browser = _running_browser()
    page = _reset_page("https://pm.gd.csg.cn/pfxh/stc/index")
    browser._pages = [page]

    async with browser._execution_lock:
        with pytest.raises(RuntimeError, match="正被任务占用"):
            await browser.reset_cookies_and_reload()
    page.reload.assert_not_awaited()


@pytest.mark.parametrize(
    ("url", "failed"),
    [
        ("chrome-error://chromewebdata/", True),
        ("chrome-error://chromewebdata/#url=https://pm.gd.csg.cn/", True),
        ("edge-error://blank", True),
        ("about:neterror", True),
        ("chrome://crash", True),
        ("about:blank", False),
        ("https://pm.gd.csg.cn/portal/#/home", False),
        ("", False),
    ],
)
def test_browser_error_page_detection(url, failed):
    assert is_browser_error_page(url) is failed


def test_bound_chrome_error_page_explains_load_failure():
    browser = _running_browser()
    page = _open_page("chrome-error://chromewebdata/")
    browser._pages = [page]
    with pytest.raises(RuntimeError, match="chrome-error"):
        _ = browser.page


def test_only_chrome_error_tab_is_not_a_generic_disconnect():
    browser = _running_browser()
    page = _open_page("chrome-error://chromewebdata/")
    browser._page_ids = {page: "err"}
    with pytest.raises(RuntimeError, match="证书客户端") as caught:
        _ = browser.page
    assert "尚未选择业务页面" not in str(caught.value)


def test_missing_page_stays_generic_when_browser_has_no_error_tab():
    browser = _running_browser()
    with pytest.raises(RuntimeError, match="尚未选择业务页面"):
        _ = browser.page


def test_lone_error_tab_is_bound_for_the_dropdown():
    browser = _running_browser()
    page = _open_page("chrome-error://chromewebdata/")
    browser._page_ids = {page: "err"}
    browser._bind_failed_page_if_alone()
    assert browser._pages == [page]
    business = _open_page("https://pm.gd.csg.cn/portal/#/home")
    browser._pages = []
    browser._page_ids = {page: "err", business: "ok"}
    browser._bind_failed_page_if_alone()
    assert browser._pages == []


async def test_list_pages_marks_chrome_error_tab():
    browser = SharedBrowser()
    browser._browser = MagicMock()
    browser._browser.is_connected.return_value = True
    page = _open_page("chrome-error://chromewebdata/")
    page.title = AsyncMock(return_value="pm.gd.csg.cn")
    context = MagicMock()
    context.pages = [page]
    browser._browser.contexts = [context]
    rows = await browser.list_pages()
    assert rows == [{
        "id": rows[0]["id"],
        "context": 0,
        "title": "pm.gd.csg.cn",
        "url": "chrome-error://chromewebdata/",
        "load_error": True,
    }]


async def test_running_chrome_error_page_fails_before_the_task_starts():
    browser = _running_browser()
    page = _open_page("chrome-error://chromewebdata/")
    browser._pages = [page]
    with pytest.raises(RuntimeError, match="证书客户端"):
        async with browser.execution("采集"):
            pass
    assert browser.status()["run"]["state"] == "failed"
    assert browser.status()["run"]["reason"] == PAGE_LOAD_FAILED_MESSAGE


async def test_check_auth_surfaces_chrome_error():
    browser = _running_browser()
    page = _open_page("chrome-error://chromewebdata/")
    browser._pages = [page]
    result = await browser.check_auth()
    assert result["state"] == "unauthenticated"
    assert "chrome-error" in result["reason"]


async def test_check_auth_disconnect_stays_unknown():
    browser = SharedBrowser()
    result = await browser.check_auth()
    assert result["state"] == "unknown"
    assert "尚未选择业务页面" in result["reason"]


async def test_check_auth_closed_page_stays_unknown():
    browser = _running_browser()
    page = _open_page("https://pm.gd.csg.cn/portal/#/home")
    page.is_closed.return_value = True
    browser._pages = [page]
    result = await browser.check_auth()
    assert result["state"] == "unknown"
    assert "被关闭" in result["reason"]


class TestGetSharedBrowser:
    def test_returns_singleton(self):
        # Reset module singleton for test isolation
        import spider_gd.core.crawler.shared_browser as _mod

        _mod._shared_browser = None
        try:
            sb1 = get_shared_browser()
            sb2 = get_shared_browser()
            assert sb1 is sb2
        finally:
            _mod._shared_browser = None
