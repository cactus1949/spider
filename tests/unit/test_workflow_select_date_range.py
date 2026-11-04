"""select / pick_date_range 只走坐标点击 + 真实键盘；替身禁止 locator/page.click/fill/press。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowExecutionError, WorkflowRunner


class _WidgetPage(DomProbeFakePage):
    def __init__(self, options=()):
        super().__init__()
        self.url = "https://pm.gd.csg.cn/portal/#/x"
        self.bring_to_front = AsyncMock()
        self.open = False
        self.options = set(options)
        self.clicked: list[str] = []
        self.values: dict[str, str] = {}
        self.focused = ""

    def is_closed(self):
        return False

    def probe_visible(self, selector, index=None):
        if "dropdown__item" in selector or "ant-select-item" in selector:
            return self.open and any(f"'{o}'" in selector for o in self.options)
        return True

    def apply_click(self, selector, index):
        self.clicked.append(selector)
        if selector == "#trigger":
            self.open = True
        self.focused = selector

    def _probe(self, arg):
        op, sel = arg["op"], arg.get("selector", "")
        if op == "editable":
            return True
        if op == "focused":
            self.probes.append((op, sel, None))
            return self.focused == sel
        if op == "has_value":
            return bool(self.values.get(sel))
        if op in ("value_matches", "value"):
            typed = self.typed[-1] if self.typed else ""
            self.values[sel] = typed
            return typed == arg.get("value") if op == "value_matches" else typed
        return super()._probe(arg)


def _runner(tmp_path, page):
    runner = WorkflowRunner({"name": "t"}, download_dir=str(tmp_path))
    runner._pointer_settle_s = 0
    runner.pages = [page]
    return runner


async def test_select_clicks_trigger_and_exact_option_by_coordinates(tmp_path):
    page = _WidgetPage(options=["实时市场"])
    await _runner(tmp_path, page).exec_step(
        {"action": "select", "selector": "#trigger", "value": "实时市场"})
    assert page.clicked[0] == "#trigger"
    assert "text-is('实时市场')" in page.clicked[1]
    assert page.mouse_events == ["down", "up", "down", "up"]


async def test_select_missing_option_aborts(tmp_path):
    page = _WidgetPage(options=[])
    runner = _runner(tmp_path, page)
    with pytest.raises(WorkflowExecutionError, match="未找到下拉选项"):
        await runner._do_select({"selector": "#trigger", "value": "不存在", "timeout": 300})


async def test_pick_date_range_types_with_keyboard_and_enter(tmp_path):
    page = _WidgetPage()
    await _runner(tmp_path, page)._do_pick_date_range({
        "start_selector": "#s", "end_selector": "#e",
        "start_value": "2026-06-01", "end_value": "2026-06-15",
    })
    assert page.clicked == ["#s", "#e"]
    assert page.typed == ["2026-06-01", "2026-06-15"]
    assert page.keys == ["Enter", "Enter"]


async def test_pick_date_range_missing_input_aborts(tmp_path):
    page = _WidgetPage()
    page.probe_visible = lambda selector, index=None: selector != "#e"
    runner = _runner(tmp_path, page)
    with pytest.raises(WorkflowExecutionError):
        await runner._do_pick_date_range({
            "start_selector": "#s", "end_selector": "#e",
            "start_value": "2026-06-01", "end_value": "2026-06-15",
        })
