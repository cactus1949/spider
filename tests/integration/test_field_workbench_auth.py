"""10291600 工作台结构：任务入口不能因旧默认选择器误判已登录状态。"""

from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from task_runner import TaskRunner

from spider_gd.core.crawler.shared_browser import SharedBrowser


@pytest.mark.integration
@pytest.mark.parametrize("legacy_selector", [".el-menu", ".ant-menu-horizontal, .el-menu"])
async def test_runtime_legacy_selector_accepts_recorded_workbench(tmp_path, legacy_selector):
    session = SharedBrowser(config_path=tmp_path / "session.json")
    session.configure(success_selector=legacy_selector)
    html = (Path(__file__).parents[1] / "fixtures/gd_workbench_header.html").read_text()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("https://pm.gd.csg.cn/**", lambda route: route.fulfill(body=html))
        await page.goto("https://pm.gd.csg.cn/portal/#/powerEnterprise")
        await page.evaluate("localStorage.setItem('pfxh-token', 'test-token')")
        result = await session.check_auth(page)
        assert result["state"] == "authenticated", result
        # 工作台菜单可能尚未渲染，右上角用户栏仍可确认已进入工作台。
        await page.evaluate("document.querySelector('.ant-menu-horizontal').remove()")
        result = await session.check_auth(page)
        assert result["state"] == "authenticated", result
        await page.evaluate("localStorage.clear()")
        assert (await session.check_auth(page))["state"] == "authenticated"
        await page.evaluate("localStorage.setItem('pfxh-token', 'stale')")
        await page.evaluate("location.hash='/home'")
        assert (await session.check_auth(page))["state"] == "unauthenticated"
        await browser.close()


@pytest.mark.integration
async def test_auth_accepts_visible_user_when_first_matching_menu_is_hidden(tmp_path):
    session = SharedBrowser(config_path=tmp_path / "session.json")
    html = (Path(__file__).parents[1] / "fixtures/gd_workbench_header.html").read_text()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("https://pm.gd.csg.cn/**", lambda route: route.fulfill(body=html))
        await page.goto("https://pm.gd.csg.cn/portal/#/powerEnterprise")
        await page.evaluate("localStorage.setItem('pfxh-token', 'test-token')")
        await page.evaluate("document.querySelector('.ant-menu-horizontal').style.display = 'none'")
        assert (await session.check_auth(page))["state"] == "authenticated"
        await browser.close()


@pytest.mark.integration
async def test_compact_legacy_selector_accepts_workbench_user(tmp_path):
    session = SharedBrowser(config_path=tmp_path / "session.json")
    session.configure(success_selector=".ant-menu-horizontal,.el-menu")
    html = (Path(__file__).parents[1] / "fixtures/gd_workbench_header.html").read_text()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("https://pm.gd.csg.cn/**", lambda route: route.fulfill(body=html))
        await page.goto("https://pm.gd.csg.cn/portal/#/powerEnterprise")
        await page.evaluate("localStorage.setItem('pfxh-token', 'test-token')")
        await page.evaluate("document.querySelector('.ant-menu-horizontal').remove()")
        assert (await session.check_auth(page))["state"] == "authenticated"
        await browser.close()


@pytest.mark.integration
async def test_task_auto_login_then_indicator_uses_same_workbench(tmp_path):
    html = (Path(__file__).parents[1] / "fixtures/gd_workbench_header.html").read_text()
    configs = {
        "login": {"name": "login", "type": "login", "steps": [
            {"action": "click", "selector": "#login"},
            {"action": "ensure_login", "require_storage_keys": ["pfxh-token"]},
        ]},
        "indicator": {"name": "indicator", "needs_login": True, "steps": [
            {"action": "wait", "selector": '[class*="userName"]', "timeout": 1000},
        ]},
    }
    session = SharedBrowser()
    session.configure(success_selector=".el-menu")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.route("https://pm.gd.csg.cn/**", lambda route: route.fulfill(body=html))
        await page.goto("https://pm.gd.csg.cn/portal/#/home")
        await page.set_content('''<button id="login" onclick="
            localStorage.setItem('pfxh-token','test-token');location.hash='/powerEnterprise';
            document.body.innerHTML=window.workbench">登录</button>''')
        await page.evaluate("html => window.workbench=html", html)
        session._browser, session._pages = browser, [page]
        runner = TaskRunner({"name": "定时任务入口", "login": "login", "indicators": ["indicator"]},
                            download_dir=str(tmp_path), workflow_loader=configs.get)
        events = []
        await runner.run(events.append, session=session)
        assert not runner._failed, events
        assert any(e["event"] == "task_done" for e in events), events
        assert not any(e["event"] == "error_pause" for e in events)
        assert session.page is page
        await browser.close()
