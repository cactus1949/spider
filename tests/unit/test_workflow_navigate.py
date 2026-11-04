"""导航语义测试：已在目标页面时不要重载文档。

现场原因：pm.gd.csg.cn 的请求签名与文档实例绑定，重载同一个 URL 等于换一份文档，
站点可能把新文档踢回登录页，也会让别的标签页的签名失效。因此提供 skip_if_loaded。
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


async def test_navigate_without_flag_keeps_old_behaviour(tmp_path):
    runner = WorkflowRunner({"name": "默认导航"}, download_dir=str(tmp_path))
    page = _page("https://pm.gd.csg.cn/portal/#/powerEnterprise")
    runner.pages = [page]

    await runner._exec_step({
        "action": "navigate",
        "url": "https://pm.gd.csg.cn/portal/#/powerEnterprise",
    })

    page.goto.assert_awaited_once()
