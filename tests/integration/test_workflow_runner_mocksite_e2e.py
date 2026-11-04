from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.preview_detector import MainWorldPreviewDetector
from workflow_runner import WorkflowExecutionError, WorkflowRunner

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MOCK_DIR = _REPO_ROOT / "mock_site"
_VERIFIED_DIR = _REPO_ROOT / "workflows" / "yml"

_WORKFLOWS = [
    ("登陆流程测试.yml", 0),
    ("日前节点电价查询导出.yml", 1),
    ("实时节点电价查询导出.yml", 1),
    ("结算管理-日清算概览导出.yml", 1),
    ("现货交易-完整采集(含披露中心).yml", 5),
]


def _is_data_artifact(path: Path) -> bool:
    if path.name == ".file_versions.json":
        return False
    return "_versions" not in path.parts


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="module")
def workflow_mock_server() -> str:
    import uvicorn

    if str(_MOCK_DIR) not in sys.path:
        sys.path.insert(0, str(_MOCK_DIR))
    import main as mock_main

    port = _free_port()
    config = uvicorn.Config(mock_main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("mock_site 启动超时")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


def _prepare_workflow(filename: str, base_url: str) -> dict[str, Any]:
    cfg = yaml.safe_load((_VERIFIED_DIR / filename).read_text(encoding="utf-8"))
    replacements = {
        "https://pm.gd.csg.cn": base_url,
        "${CERT_PASSWORD}": "mock-cert-password",
        "${ACCOUNT}": "mock-account",
        "${ACCOUNT_PASSWORD}": "mock-account-password",
    }

    def _rewrite(value: Any) -> Any:
        if isinstance(value, str):
            for placeholder, replacement in replacements.items():
                value = value.replace(placeholder, replacement)
            return value
        if isinstance(value, list):
            return [_rewrite(item) for item in value]
        if isinstance(value, dict):
            rewritten = {key: _rewrite(item) for key, item in value.items()}
            action = rewritten.get("action")
            if action == "wait" and "ms" in rewritten:
                rewritten["ms"] = min(int(rewritten["ms"]), 20)
            if "timeout" in rewritten:
                rewritten["timeout"] = min(int(rewritten["timeout"]), 5000)
            if action == "captcha":
                rewritten["retry"] = 1
                rewritten["pre_refresh"] = 0
            return rewritten
        return value

    return _rewrite(cfg)


class _FixedCaptchaSolver:
    def recognize(self, _image_bytes: bytes) -> str:
        return "1234"


@pytest.mark.integration
@pytest.mark.parametrize(("filename", "expected_downloads"), _WORKFLOWS)
async def test_verified_workflow_runs_against_mocksite(
    filename: str,
    expected_downloads: int,
    workflow_mock_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    import spider_gd.core.crawler.captcha_solver as captcha_solver

    monkeypatch.setattr(captcha_solver, "CaptchaSolver", _FixedCaptchaSolver)

    cfg = _prepare_workflow(filename, workflow_mock_server)
    download_dir = tmp_path / filename.replace(".yml", "")
    events: list[dict] = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        pages = [await context.new_page()]
        detectors = []
        detector_tasks = []

        async def watch(page):
            session = await context.new_cdp_session(page)
            detector = MainWorldPreviewDetector(session)
            await detector.start()
            detectors.append(detector)

        await watch(pages[0])
        context.on("page", lambda page: detector_tasks.append(asyncio.create_task(watch(page))))
        # 模拟用户已经在门户手工登录；执行器不得替换该文档或写入凭据。
        await pages[0].goto(f"{workflow_mock_server}/portal/#/powerEnterprise")
        await pages[0].evaluate("""() => {
            localStorage.setItem('pfxh-token', 'mock-manual-token');
            window.__manual_document = 'keep';
        }""")
        runner = WorkflowRunner(cfg, download_dir=str(download_dir), headless=True)
        context.on("page", runner._on_new_page)
        try:
            await runner.run(events.append, None, browser=browser, context=context, pages=pages)
            if detector_tasks:
                await asyncio.gather(*detector_tasks)
            for detector in detectors:
                await detector.assert_clean()
            assert await pages[0].evaluate("localStorage.getItem('pfxh-token')") == "mock-manual-token"
        finally:
            await context.close()
            await browser.close()

    event_names = [event["event"] for event in events]
    assert runner.failed is False, runner.failure_reason
    assert "workflow_done" in event_names
    assert "step_error" not in event_names
    assert "site_unreachable" not in event_names

    downloaded_files = [
        p for p in download_dir.rglob("*")
        if p.is_file() and _is_data_artifact(p.relative_to(download_dir))
    ]
    assert len(downloaded_files) == expected_downloads


@pytest.mark.integration
@pytest.mark.parametrize(
    ("kind", "selector"),
    [
        ("element-ui", "#date-input"),
        ("element-plus", "#date-input"),
        ("antd", ".ant-picker-input input"),
    ],
)
async def test_pick_date_selects_specific_date_in_mock_picker_lab(
    kind: str,
    selector: str,
    workflow_mock_server: str,
    tmp_path: Path,
):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    target = "2026-06-15"
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        runner = WorkflowRunner({"name": "日期选择实验", "steps": []}, download_dir=str(tmp_path))
        runner.pages = [page]
        try:
            await page.goto(f"{workflow_mock_server}/date-picker-lab/{kind}")
            await runner._do_pick_date({"selector": selector, "value": target})
            value = await page.locator(selector).input_value()
        finally:
            await context.close()
            await browser.close()

    assert value == target


@pytest.mark.integration
async def test_pick_date_ignores_hidden_other_picker_header(
    workflow_mock_server: str, tmp_path: Path,
):
    """隐藏的旧日历不能让 09-30 实际落到当前月 30 日。"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1024, "height": 768})
        runner = WorkflowRunner({"name": "结算结束日期", "steps": []}, download_dir=str(tmp_path))
        runner.pages = [page]
        try:
            await page.goto(f"{workflow_mock_server}/date-picker-lab/element-ui")
            await page.locator("#date-input").click()
            await page.locator('.el-date-picker__header-label[data-open="month"]').click()
            await page.locator('[data-month-pick="10"]').click()
            await page.evaluate("""() => {
                const hidden = document.createElement('div');
                hidden.style.display = 'none';
                hidden.innerHTML = '<span class="el-date-picker__header-label">2026 年</span>'
                    + '<span class="el-date-picker__header-label">九月</span>';
                document.querySelector('.el-date-picker').before(hidden);
            }""")
            assert await runner._read_element_picker_header() == (2026, 10)
            await runner._do_pick_date({
                "selector": "#date-input", "value": "2026-09-30", "verify_value": True,
            })
            actual = await page.locator("#date-input").input_value()
        finally:
            await browser.close()

    assert actual == "2026-09-30"


@pytest.mark.integration
async def test_pick_date_rejects_calendar_success_when_input_stays_wrong(
    workflow_mock_server: str, tmp_path: Path,
):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1024, "height": 768})
        runner = WorkflowRunner({"name": "结算结束日期", "steps": []}, download_dir=str(tmp_path))
        runner.pages = [page]
        try:
            await page.goto(f"{workflow_mock_server}/date-picker-lab/element-ui")
            await page.evaluate("""() => document.querySelector('#date-input')
                .addEventListener('change', event => { event.target.value = '2026-10-30' })""")
            with pytest.raises(WorkflowExecutionError, match="输入框实际为 '2026-10-30'"):
                await runner._do_pick_date({
                    "selector": "#date-input", "value": "2026-09-30", "verify_value": True,
                })
        finally:
            await browser.close()


@pytest.mark.integration
async def test_stale_document_gateway_400_recovers_with_fresh_tab(
    workflow_mock_server: str,
    tmp_path: Path,
):
    """现场复现：新标签页的文档签名失效 → 业务 XHR 被网关 400。

    页面本身能渲染、菜单能点，但 getJddjTree / getFirstAndLastTen 全部 400，
    数据永远加载不出来，导出拿不到文件。人工的解法是关掉这个标签页、
    重新点现货交易（新文档）——执行器应当自动完成这个动作并回放步骤。
    """
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    cfg = {
        "name": "网关400恢复",
        "steps": [
            {
                "action": "navigate",
                "url": f"{workflow_mock_server}/portal/?mock=stale#/powerEnterprise",
            },
            {"action": "click", "selector": "text=现货交易", "expect": "new_tab", "timeout": 5000},
            {"action": "wait", "selector": ".el-menu", "timeout": 5000},
            {"action": "click", "selector": ".el-submenu__title:has-text('组织中心')"},
            {"action": "click", "selector": ".el-menu-item:has-text('日前节点电价查询')"},
            {
                "action": "wait",
                "selector": ".style-box-head .title:has-text('日前节点电价查询')",
                "timeout": 3000,
            },
            {"action": "pick_date", "selector": ".dateBox .el-input__inner", "value": "-1d"},
            {
                "action": "export",
                "selector": (
                    ".style-box-head:has(.title:has-text('日前节点电价查询')) .exportBtnColor"
                ),
                "save_as": "stale_recovered.xlsx",
                "timeout": 3000,
            },
        ],
    }
    events: list[dict] = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900}, accept_downloads=True
        )
        pages = [await context.new_page()]
        await context.add_init_script(
            """(() => {
              const key = '__doc_loads';
              const next = Number(sessionStorage.getItem(key) || '0') + 1;
              sessionStorage.setItem(key, String(next));
            })();"""
        )
        runner = WorkflowRunner(cfg, download_dir=str(tmp_path), headless=True)
        context.on("page", runner._on_new_page)
        parent_loads = None
        try:
            await runner.run(events.append, None, browser=browser, context=context, pages=pages)
            parent_loads = await pages[0].evaluate("() => sessionStorage.getItem('__doc_loads')")
        finally:
            await context.close()
            await browser.close()

    event_names = [event["event"] for event in events]
    assert runner.failed is False, runner.failure_reason
    assert "workflow_done" in event_names
    assert "page_recovery" in event_names, "网关 400 后应关掉新标签并从点击重放"
    recovery = next(event for event in events if event["event"] == "page_recovery")
    assert recovery["replay_from"] == 2
    assert parent_loads == "1", "恢复时不应刷新绑定页"
    assert list(tmp_path.rglob("stale_recovered.xlsx")), "恢复后应成功导出"


@pytest.mark.integration
async def test_stale_document_reload_stays_400_new_document_is_200(
    workflow_mock_server: str,
):
    """同一份文档刷新仍然 400；换 doc 的新标签才 200。"""
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    async def query_hint(page) -> str:
        await page.click("#staleMenuDayahead")
        await page.wait_for_function(
            """() => {
              const hint = document.getElementById('staleHint');
              const text = hint ? hint.textContent : '';
              return text.includes('HTTP') || text.includes('查询成功');
            }"""
        )
        return await page.inner_text("#staleHint")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        try:
            await page.goto(f"{workflow_mock_server}/pfxh/stc/stale?code=mock&doc=first")
            first = await query_hint(page)
            await page.reload()
            refreshed = await query_hint(page)
            fresh = await context.new_page()
            await fresh.goto(f"{workflow_mock_server}/pfxh/stc/stale?code=mock&doc=second")
            opened = await query_hint(fresh)
        finally:
            await context.close()
            await browser.close()

    assert "400" in first
    assert "400" in refreshed
    assert "查询成功" in opened


@pytest.mark.integration
async def test_export_failure_uses_mocksite_api_fallback(
    workflow_mock_server: str,
    tmp_path: Path,
):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    cfg = {
        "name": "API降级端到端",
        "api_fallback": {
            "enabled": True,
            "url": (
                "/pfjs/qctc-pm-jsgl-electricity-month-query/sbs/"
                "calDayStatQuery/exportItemsWithoutCalVersion"
            ),
            "method": "POST",
        },
        "steps": [
            {"action": "navigate", "url": f"{workflow_mock_server}/pfjs/stc/check?code=mock"},
            {"action": "wait", "selector": ".el-menu", "timeout": 5000},
            {
                "action": "export",
                "selector": ".missing-export-button",
                "save_as": "fallback-settlement.xlsx",
                "timeout": 200,
            },
        ],
    }
    events: list[dict] = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        pages = [await context.new_page()]
        runner = WorkflowRunner(cfg, download_dir=str(tmp_path), headless=True)
        try:
            await runner.run(events.append, None, browser=browser, context=context, pages=pages)
        finally:
            await context.close()
            await browser.close()

    event_names = [event["event"] for event in events]
    assert runner.failed is False, runner.failure_reason
    assert "api_fallback" in event_names
    assert any(
        event.get("status") == "success"
        for event in events
        if event["event"] == "api_fallback"
    )
    assert "step_error" not in event_names
    assert list(tmp_path.rglob("fallback-settlement.xlsx"))


@pytest.mark.integration
async def test_reset_site_storage_clears_local_state_before_login(
    workflow_mock_server: str,
    tmp_path: Path,
):
    """白屏/半登录的人工解法要能自动化：清本地状态 + cookie，再刷新。

    现场排查时人工就是清掉 localStorage / sessionStorage / cookie 后恢复正常，
    这里验证 reset_site_storage 步骤真的清得干净（含 httpOnly 之外的全部 cookie）。
    """
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    cfg = {
        "name": "清理站点残留",
        "steps": [
            {"action": "navigate", "url": f"{workflow_mock_server}/pfxh/stc/check?code=mock"},
            {
                "action": "reset_site_storage",
                "origin": workflow_mock_server,
                "types": ["local_storage", "session_storage", "cookies"],
                "reload": True,
                "settle_timeout": 1000,
            },
        ],
    }
    events: list[dict] = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        pages = [await context.new_page()]
        await pages[0].goto(f"{workflow_mock_server}/pfxh/stc/check?code=mock")
        await pages[0].evaluate(
            """() => {
                localStorage.setItem('pfxh-userInfo', '{"screenName":"x"}');
                sessionStorage.setItem('pfxh-ticket', 'half-login');
                document.cookie = 'mock_business_cookie=1; path=/';
            }"""
        )
        runner = WorkflowRunner(cfg, download_dir=str(tmp_path), headless=True)
        try:
            await runner.run(events.append, None, browser=browser, context=context, pages=pages)
            remaining = await pages[0].evaluate(
                """() => ({
                    local: Object.keys(localStorage),
                    session: Object.keys(sessionStorage),
                    cookie: document.cookie,
                })"""
            )
        finally:
            await context.close()
            await browser.close()

    assert runner.failed is False, runner.failure_reason
    assert "storage_reset" in [event["event"] for event in events]
    assert remaining["local"] == [], f"localStorage 未清理: {remaining['local']}"
    assert remaining["session"] == [], f"sessionStorage 未清理: {remaining['session']}"
    assert "mock_business_cookie" not in remaining["cookie"], f"cookie 未清理: {remaining}"
