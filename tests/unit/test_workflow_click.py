"""默认点击只使用普通数据；保留新标签、失败和显式旧路径的语义。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowExecutionError, WorkflowRunner


class _ClickPage(DomProbeFakePage):
    def __init__(self, *, missing=False):
        super().__init__()
        self.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
        self.context = MagicMock()
        self.context.pages = [self]
        self.missing = missing
        self.clicked = []
        self.on_click = lambda: None
        self.bring_to_front = AsyncMock()

    def is_closed(self):
        return False

    def probe_visible(self, selector, index=None):
        return not self.missing

    def apply_click(self, selector, index):
        self.clicked.append(selector)
        self.on_click()


def _runner(tmp_path, **config):
    runner = WorkflowRunner(config, download_dir=str(tmp_path))
    runner._pointer_settle_s = 0
    return runner


@pytest.mark.parametrize("new_tab", [False, True])
async def test_click_without_element_handles_preserves_navigation(tmp_path, new_tab):
    runner = _runner(tmp_path)
    page = _ClickPage()
    popup = MagicMock()
    popup.is_closed.return_value = False
    popup.wait_for_load_state = AsyncMock()
    popup.opener = AsyncMock(return_value=page)
    popup.bring_to_front = AsyncMock()
    if new_tab:
        page.on_click = lambda: runner.pages.append(popup)
    runner.pages = [page]
    await runner.exec_step(
        {
            "action": "click",
            "selector": "button",
            "timeout": 1234,
            "expect": "new_tab" if new_tab else "",
        }
    )
    assert page.clicked == ["button"]
    assert runner.page is (popup if new_tab else page)
    page.bring_to_front.assert_awaited()
    if new_tab:
        popup.bring_to_front.assert_awaited()


async def test_click_failure_is_not_reported_as_success(tmp_path):
    runner = _runner(tmp_path)
    runner.pages = [_ClickPage(missing=True)]
    with pytest.raises(WorkflowExecutionError, match="不可操作"):
        await runner.exec_step({"action": "click", "selector": "button", "timeout": 1})
    assert runner.page.clicked == []


async def test_click_miss_explains_same_url_is_another_document(tmp_path):
    bound = _ClickPage(missing=True)
    duplicate = _ClickPage()
    duplicate.context = bound.context
    bound.context.pages = [bound, duplicate]
    runner = _runner(tmp_path)
    runner.pages = [bound]
    runner.context = bound.context
    with pytest.raises(WorkflowExecutionError, match="另一份文档") as caught:
        await runner.exec_step({"action": "click", "selector": "text=现货交易", "timeout": 1})
    message = str(caught.value)
    assert "地址与绑定页完全相同" in message
    assert "未绑定的业务标签：https://pm.gd.csg.cn/portal/#/powerEnterprise" not in message
    assert duplicate.clicked == []


async def test_click_miss_names_the_unbound_business_tab(tmp_path):
    portal = _ClickPage(missing=True)
    spot = _ClickPage()
    spot.url = "https://pm.gd.csg.cn/pfxh/stc/index"
    spot.context = portal.context
    portal.context.pages = [portal, spot]
    runner = _runner(tmp_path)
    runner.pages = [portal]
    runner.context = portal.context
    with pytest.raises(WorkflowExecutionError, match=r"pfxh/stc/index"):
        await runner.exec_step(
            {
                "action": "click",
                "selector": ".el-menu-item:has-text('组织中心')",
                "timeout": 1,
            }
        )
    assert portal.clicked == []
    assert spot.clicked == []
    assert spot.probes == []


async def test_click_moves_onto_front_tab_before_pressing(tmp_path):
    page = _ClickPage()
    runner = _runner(tmp_path)
    runner.pages = [page]
    page.on_click = lambda: page.bring_to_front.assert_awaited()
    await runner.exec_step({"action": "click", "selector": "button", "timeout": 100})
    assert page.mouse_moves
    assert page.mouse_events == ["down", "up"]


async def test_dashboard_probe_does_not_wait_for_absent_elements(tmp_path):
    runner = _runner(tmp_path)
    page = MagicMock()
    page.is_closed.return_value = False
    page.url = "https://pm.gd.csg.cn/pfxh/stc/index"
    target = page.locator.return_value.first
    target.is_visible = AsyncMock(return_value=False)
    target.wait_for = AsyncMock(side_effect=AssertionError("unexpected element handle"))
    runner.pages = [page]
    assert await runner._is_logged_in_dashboard() is False
    target.wait_for.assert_not_awaited()


async def test_click_uses_mouse_trajectory_and_press_duration(tmp_path):
    page = _ClickPage()
    runner = _runner(tmp_path)
    runner.pages = [page]
    await runner.exec_step({"action": "click", "selector": "button"})
    assert len(page.mouse_moves) >= 6
    assert page.mouse_events == ["down", "up"]
    assert page.clicked == ["button"]


@pytest.mark.parametrize("overrides", [
    {"human_input": False},
    {"low_profile_input": False},
    {"low_profile_input": "off", "human_input": "no"},
])
async def test_legacy_input_modes_are_rejected(tmp_path, overrides):
    with pytest.raises(WorkflowExecutionError, match="旧输入模式已移除"):
        _runner(tmp_path, **overrides)


async def test_click_never_falls_back_to_native_locator_or_fill(tmp_path):
    runner = _runner(tmp_path)
    assert not hasattr(runner, "_human_input")
    assert not hasattr(runner, "_low_profile_input")
    assert not hasattr(runner, "_human_click")
    page = _ClickPage()
    page.locator = MagicMock(side_effect=AssertionError("不应创建 locator"))
    page.fill = AsyncMock(side_effect=AssertionError("不应调用 page.fill"))
    runner.pages = [page]
    await runner.exec_step({"action": "click", "selector": "button"})
    assert page.mouse_events == ["down", "up"]
    assert page.clicked == ["button"]
