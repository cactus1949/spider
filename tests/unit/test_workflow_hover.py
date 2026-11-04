"""hover 只走 DOM 探针坐标 + mouse.move 人形轨迹，不按键、不用 page.hover/locator。"""

from unittest.mock import AsyncMock

import pytest
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowExecutionError, WorkflowRunner


class _HoverPage(DomProbeFakePage):
    def __init__(self, visible=True):
        super().__init__()
        self.url = "https://pm.gd.csg.cn/portal/#/home"
        self.bring_to_front = AsyncMock()
        self.visible = visible

    def is_closed(self):
        return False

    def probe_visible(self, selector, index=None):
        return self.visible


def _runner(tmp_path, page):
    runner = WorkflowRunner({"name": "t"}, download_dir=str(tmp_path))
    runner.pages = [page]
    return runner


async def test_hover_moves_by_coordinates_without_pressing(tmp_path):
    page = _HoverPage()
    runner = _runner(tmp_path, page)
    phases = []

    async def trace(phase):
        phases.append(phase)

    runner._trace_browser_action = trace
    await runner.exec_step({"action": "hover", "selector": ".ant-menu-submenu-title"})
    assert len(page.mouse_moves) >= 7
    assert page.mouse_events == []
    x, y = page.mouse_moves[-1]
    assert 100 <= x <= 120 and 40 <= y <= 60  # 落在探针给出的 box 内
    assert phases == ["hover.before", "hover.after"]


async def test_hover_missing_target_raises(tmp_path):
    page = _HoverPage(visible=False)
    with pytest.raises(WorkflowExecutionError, match="悬停目标未出现"):
        await _runner(tmp_path, page)._do_hover({"selector": ".nope", "timeout": 200})


async def test_hover_without_selector_raises(tmp_path):
    with pytest.raises(WorkflowExecutionError):
        await _runner(tmp_path, _HoverPage())._do_hover({})
