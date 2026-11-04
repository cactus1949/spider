"""捕获实际 Chromium 中的主世界 InjectedScript 初始化，防止重引入已复现的路径。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from tests.integration import test_workflow_runner_mocksite_e2e as mock_workflows
from tests.preview_detector import MainWorldPreviewDetector
from workflow_runner import WorkflowExecutionError, WorkflowRunner

workflow_mock_server = mock_workflows.workflow_mock_server
_prepare_workflow = mock_workflows._prepare_workflow


@pytest.fixture
async def data_page(tmp_path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            session = await page.context.new_cdp_session(page)
            detector = MainWorldPreviewDetector(session)
            await detector.start()
            await page.set_content("""<style>button,input,img{margin:10px}</style>
                <button id="target" onclick="this.dataset.clicked='yes'">查询</button>
                <div class="style-box-head"><div class="title">日前节点电价查询</div>
                  <button class="exportBtnColor" id="dayahead">导出</button></div>
                <div class="style-box-head"><div class="title">实时节点电价查询</div>
                  <button class="exportBtnColor" id="realtime">导出</button></div>
                <input id="pin" type="password"><input id="captcha-code">
                <img id="captcha" width="100" height="40"><input id="agreement" type="checkbox">
            """)
            runner = WorkflowRunner({}, download_dir=str(tmp_path))
            runner.pages = [page]
            runner._pointer_settle_s = 0
            yield runner, page, detector
        finally:
            await browser.close()


@pytest.mark.integration
@pytest.mark.parametrize("action", ["click", "wait"])
async def test_click_and_wait_do_not_create_main_world_element_preview(data_page, action):
    runner, page, detector = data_page
    await runner.exec_step({"action": action, "selector": "#target", "timeout": 1000})
    if action == "click":
        assert await page.evaluate("document.querySelector('#target').dataset.clicked") == "yes"
    await detector.assert_clean()


@pytest.mark.integration
async def test_actual_yaml_nested_text_selectors_work_without_handles(data_page):
    runner, page, detector = data_page
    selector = ".style-box-head:has(.title:has-text('实时节点电价查询')) .exportBtnColor"
    box = await runner._dom_box(selector)
    assert isinstance(box, dict), "实际 YAML 的嵌套文字选择器未匹配"
    expected = await page.evaluate("""() => {
        const r=document.querySelector('#realtime').getBoundingClientRect();return {x:r.x,y:r.y};
    }""")
    assert box["x"] == expected["x"]
    assert box["y"] == expected["y"]
    assert await runner._dom_visible("text=实时节点电价查询")
    await detector.assert_clean()


@pytest.mark.integration
async def test_fill_check_and_captcha_do_not_initialize_element_preview(data_page, monkeypatch):
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    runner, page, detector = data_page
    await page.evaluate("""() => {
        document.querySelector('#captcha').src = 'data:image/png;base64,' +
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1sAAAAASUVORK5CYII=';
    }""")

    class Solver:
        def recognize(self, _image):
            return "1234"

    monkeypatch.setattr(captcha_solver, "CaptchaSolver", Solver)
    await runner.exec_step({"action": "fill", "selector": "#pin", "value": "mock-pin"})
    await runner.exec_step({"action": "check", "selector": "#agreement:not(:checked)"})
    await runner.exec_step(
        {
            "action": "captcha",
            "captcha_img": "#captcha",
            "captcha_input": "#captcha-code",
            "retry": 1,
        }
    )
    assert await page.evaluate("document.querySelector('#pin').value === 'mock-pin'")
    assert await page.evaluate("document.querySelector('#agreement').checked")
    assert await page.evaluate("document.querySelector('#captcha-code').value === '1234'")
    await detector.assert_clean()


@pytest.mark.integration
async def test_captcha_http_error_pauses_before_any_retry(data_page, monkeypatch):
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    runner, _page, detector = data_page
    monkeypatch.setattr(captcha_solver, "CaptchaSolver", lambda: object())
    runner._page_errors.append(
        {
            "status": 400,
            "url": "https://example.test/captcha",
            "resource_type": "xhr",
            "content_type": "text/html",
        }
    )
    events = []
    runner._on_event = events.append
    runner._low_profile_click = AsyncMock(side_effect=AssertionError("must not refresh"))
    with pytest.raises(WorkflowExecutionError, match="请求异常"):
        await runner._do_captcha({"pre_refresh": 4, "retry": 5})
    runner._low_profile_click.assert_not_awaited()
    assert any(event["event"] == "error_pause" for event in events)
    await detector.assert_clean()


@pytest.mark.integration
async def test_actual_login_yaml_from_logged_out_page(workflow_mock_server, tmp_path, monkeypatch):
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    class Solver:
        def recognize(self, _image):
            return "1234"

    monkeypatch.setattr(captcha_solver, "CaptchaSolver", Solver)
    config = _prepare_workflow("登陆流程测试.yml", workflow_mock_server)
    for step in config["steps"]:
        if step["action"] == "dismiss_modal":
            step["timeout"] = 100
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            detector = MainWorldPreviewDetector(await context.new_cdp_session(page))
            await detector.start()
            runner = WorkflowRunner(config, download_dir=str(tmp_path))
            runner._pointer_settle_s = 0
            events = []
            await runner.run(events.append, browser=browser, context=context, pages=[page])
            assert not runner.failed, runner.failure_reason
            assert await page.evaluate("localStorage.getItem('pfxh-token')") == "mock-token"
            assert any(event["event"] == "workflow_done" for event in events)
            await detector.assert_clean()
        finally:
            await browser.close()


@pytest.mark.integration
async def test_probe_failure_cannot_fall_back_to_element_api(data_page, monkeypatch):
    runner, page, _detector = data_page
    monkeypatch.setattr(
        page, "evaluate", AsyncMock(side_effect=RuntimeError("lost execution context"))
    )
    forbidden = AsyncMock(side_effect=AssertionError("must not fall back"))
    monkeypatch.setattr(page, "click", forbidden)
    with pytest.raises(WorkflowExecutionError, match="探测"):
        await runner._low_profile_click("#target", timeout=100)
    forbidden.assert_not_awaited()


@pytest.mark.integration
async def test_invalid_export_probe_cannot_bypass_failure_using_api_fallback(
    data_page, monkeypatch
):
    runner, _page, detector = data_page
    fallback = AsyncMock(side_effect=AssertionError("must not bypass broken probe"))
    monkeypatch.setattr(runner, "_try_api_fallback", fallback)
    with pytest.raises(WorkflowExecutionError, match="探测"):
        await runner._do_export({"selector": "button:invalid-pseudo()", "timeout": 100})
    fallback.assert_not_awaited()
    await detector.assert_clean()


@pytest.mark.integration
async def test_disabled_or_covered_target_is_not_reported_as_clicked(data_page):
    runner, page, detector = data_page
    await page.evaluate("document.querySelector('#target').disabled=true")
    with pytest.raises(WorkflowExecutionError, match="不可操作"):
        await runner.exec_step({"action": "click", "selector": "#target", "timeout": 50})
    assert await page.evaluate("!document.querySelector('#target').dataset.clicked")
    await page.evaluate("""() => {
        document.querySelector('#target').disabled=false;
        const cover=document.createElement('div');
        cover.style='position:fixed;inset:0;z-index:1000';document.body.appendChild(cover);
    }""")
    with pytest.raises(WorkflowExecutionError, match="不可操作"):
        await runner.exec_step({"action": "click", "selector": "#target", "timeout": 50})
    assert await page.evaluate("!document.querySelector('#target').dataset.clicked")
    await detector.assert_clean()


@pytest.mark.integration
async def test_captcha_capture_failure_does_not_use_element_screenshot(data_page, monkeypatch):
    runner, _page, detector = data_page
    monkeypatch.setattr(runner, "_captcha_cdp_screenshot", AsyncMock(return_value=None))
    with pytest.raises(WorkflowExecutionError, match="验证码"):
        await runner._captcha_image_bytes("#captcha")
    await detector.assert_clean()
