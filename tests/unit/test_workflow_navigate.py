"""导航语义测试：已在目标页面时不要重载文档。

现场原因：pm.gd.csg.cn 的请求签名与文档实例绑定，重载同一个 URL 等于换一份文档，
站点可能把新文档踢回登录页，也会让别的标签页的签名失效。因此默认跳过重载，
只有 ``reload: true`` 或 ``skip_if_loaded: false`` 才强制重载。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from workflow_runner import WorkflowRunner


def _page(url: str) -> MagicMock:
    page = MagicMock()
    page.url = url
    page.is_closed.return_value = False
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    return page


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("https://pm.gd.csg.cn/portal/#/powerEnterprise", "https://pm.gd.csg.cn/portal/#/powerEnterprise"),
        ("https://pm.gd.csg.cn/portal/#/powerEnterprise", "https://pm.gd.csg.cn/portal#/powerEnterprise"),
    ],
)
async def test_navigate_skips_reload_when_already_there(tmp_path, current, target):
    runner = WorkflowRunner({"name": "同页导航"}, download_dir=str(tmp_path))
    page = _page(current)
    runner.pages = [page]

    await runner._exec_step({"action": "navigate", "url": target, "skip_if_loaded": True})

    page.goto.assert_not_awaited()


async def test_navigate_reloads_when_page_differs(tmp_path):
    runner = WorkflowRunner({"name": "换页导航"}, download_dir=str(tmp_path))
    page = _page("https://pm.gd.csg.cn/portal/#/home")
    runner.pages = [page]

    await runner._exec_step({
        "action": "navigate",
        "url": "https://pm.gd.csg.cn/portal/#/powerEnterprise",
        "skip_if_loaded": True,
    })

    page.goto.assert_awaited_once()


async def test_navigate_skips_reload_by_default(tmp_path):
    runner = WorkflowRunner({"name": "默认导航"}, download_dir=str(tmp_path))
    page = _page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner.pages = [page]

    await runner._exec_step({
        "action": "navigate",
        "url": "https://pm.gd.csg.cn/portal/#/powerEnterprise",
    })

    page.goto.assert_not_awaited()


@pytest.mark.parametrize("step", [
    {"reload": True},
    {"skip_if_loaded": False},
])
async def test_navigate_can_still_force_reload(tmp_path, step):
    runner = WorkflowRunner({"name": "强制重载"}, download_dir=str(tmp_path))
    page = _page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner.pages = [page]

    await runner._exec_step({
        "action": "navigate",
        "url": "https://pm.gd.csg.cn/portal/#/powerEnterprise",
        **step,
    })

    page.goto.assert_awaited_once()


async def test_navigate_reuses_the_open_portal_instead_of_reloading_the_business_tab(tmp_path):
    runner = WorkflowRunner({"name": "回到门户"}, download_dir=str(tmp_path))
    portal = _page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    spot = _page("https://pm.gd.csg.cn/pfxh/stc/index")
    runner.pages = [portal, spot]
    runner._current_page_idx = 1
    runner._protected_pages = {portal}

    await runner._exec_step({
        "action": "navigate",
        "url": "https://pm.gd.csg.cn/portal/#/powerEnterprise",
    })

    spot.goto.assert_not_awaited()
    portal.goto.assert_not_awaited()
    assert runner.page is portal


class _MenuTab:
    def __init__(self, url: str, visible: set[str]) -> None:
        self.url = url
        self.visible = visible
        self.closed = False
        self.front = 0

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.front += 1


async def test_click_binds_the_portal_tab_that_shows_the_menu(tmp_path):
    runner = WorkflowRunner({"name": "日清算"}, download_dir=str(tmp_path))
    good = _MenuTab("https://pm.gd.csg.cn/portal/#/powerEnterprise", {"text=结算管理"})
    broken = _MenuTab("https://pm.gd.csg.cn/portal/#/powerEnterprise", set())
    runner.pages = [good, broken]
    runner._current_page_idx = 1
    runner._protected_pages = {good}

    async def visible(selector, timeout=0, index=None):
        return selector in getattr(runner.page, "visible", set())

    async def click(selector, timeout=3000, index=None):
        return selector in getattr(runner.page, "visible", set())

    runner._dom_visible = visible
    runner._low_profile_click = click

    await runner._do_click({"selector": "text=结算管理"})

    assert runner.page is good
    assert broken.closed is True
    assert good.closed is False
