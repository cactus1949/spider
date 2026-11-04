"""真实 Chromium + mock_site/widget-lab：select / pick_date_range 生效且事件为真实输入。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.test_workflow_runner_mocksite_e2e import workflow_mock_server  # noqa: F401
from tests.preview_detector import MainWorldPreviewDetector
from workflow_runner import WorkflowRunner


async def _with_lab(base_url, tmp_path, steps):
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        context = await browser.new_context(viewport={"width": 1024, "height": 768})
        page = await context.new_page()
        detector = MainWorldPreviewDetector(await context.new_cdp_session(page))
        await detector.start()
        await page.goto(f"{base_url}/widget-lab")
        runner = WorkflowRunner({"name": "widget-lab", "steps": []}, download_dir=str(tmp_path))
        runner._pointer_settle_s = 0
        runner.pages = [page]
        try:
            for step in steps:
                await runner.exec_step(step)
            await detector.assert_clean()
            state = await page.evaluate("""() => ({
                el: document.getElementById('el-select-input').value,
                ant: document.getElementById('ant-select-value').textContent,
                start: document.getElementById('range-start').value,
                end: document.getElementById('range-end').value,
                log: window.__inputLog,
            })""")
        finally:
            await browser.close()
    return state


def _press_durations(events):
    downs, out = [], []
    for e in events:
        if e["type"] == "pointerdown":
            downs.append(e["t"])
        elif e["type"] == "pointerup" and downs:
            out.append(e["t"] - downs.pop())
    return out


@pytest.mark.integration
async def test_select_element_and_antd(workflow_mock_server, tmp_path: Path):  # noqa: F811
    state = await _with_lab(workflow_mock_server, tmp_path, [
        {"action": "select", "selector": "#el-select-input", "value": "实时市场"},
        {"action": "select", "selector": "#ant-select .ant-select-selector", "value": "深圳"},
    ])
    assert state["el"] == "实时市场"  # 不误中“实时市场10”
    assert state["ant"] == "深圳"
    events = state["log"]["events"]
    assert all(e["trusted"] for e in events)
    presses = _press_durations(events)
    assert len(presses) == 4 and min(presses) >= 120  # 人形按压，非 locator.click 的 0ms
    assert sum(e["type"] == "mousemove" for e in events) >= 20


@pytest.mark.integration
async def test_pick_date_range_element(workflow_mock_server, tmp_path: Path):  # noqa: F811
    state = await _with_lab(workflow_mock_server, tmp_path, [{
        "action": "pick_date_range",
        "start_selector": "#range-start", "end_selector": "#range-end",
        "start_value": "2026-06-01", "end_value": "2026-06-15",
    }])
    assert (state["start"], state["end"]) == ("2026-06-01", "2026-06-15")
    assert state["log"]["committed"] == {"range-start": "2026-06-01", "range-end": "2026-06-15"}
    events = state["log"]["events"]
    keydowns = [e for e in events if e["type"] == "keydown"]
    # 每个字符都有真实 keydown（page.fill 只有 input、没有 keydown）
    assert len(keydowns) >= 20 + 2
    inputs = [e for e in events if e["type"] == "input"]
    assert inputs and all(e["inputType"] == "insertText" for e in inputs)


@pytest.mark.integration
async def test_hover_opens_submenu_with_moves_only(workflow_mock_server, tmp_path: Path):  # noqa: F811
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        page = await browser.new_page(viewport={"width": 1024, "height": 768})
        detector = MainWorldPreviewDetector(await page.context.new_cdp_session(page))
        await detector.start()
        await page.goto(f"{workflow_mock_server}/widget-lab")
        runner = WorkflowRunner({"name": "hover", "steps": []}, download_dir=str(tmp_path))
        runner.pages = [page]
        try:
            await runner.exec_step({"action": "hover", "selector": ".ant-menu-submenu-title:has-text('资讯公告')"})
            await detector.assert_clean()
            state = await page.evaluate("""() => ({
                shown: getComputedStyle(document.getElementById('hover-sub')).display,
                events: window.__inputLog.events,
            })""")
        finally:
            await browser.close()
    events = state["events"]
    assert state["shown"] == "block"
    assert events and all(e["trusted"] for e in events)
    assert sum(e["type"] == "mousemove" for e in events) >= 7
    assert sum(e["type"] == "pointermove" for e in events) >= 7
    assert not any(e["type"] in ("pointerdown", "pointerup") for e in events)
