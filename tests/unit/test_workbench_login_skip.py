"""工作台跳过立即登录时，不能把还没关掉的弹窗当成登录完成。"""

from __future__ import annotations

import pytest
from workflow_runner import WorkflowRunner, login_submit_decision


class _Page:
    def __init__(self, popup: dict | None) -> None:
        self.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
        self.popup = popup

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, _script: str):
        return self.popup


def _runner(popup: dict | None, *, force: bool = False) -> WorkflowRunner:
    runner = WorkflowRunner(
        {"name": "登陆流程测试", "type": "login", "steps": []},
        download_dir="/tmp",
    )
    runner.pages = [_Page(popup)]
    if force:
        runner.config["_force_login"] = True

    async def dismiss() -> None:
        if force:
            raise AssertionError("强制重新登录不应先去关上一站的弹窗")

    runner.dismiss_workbench_popups = dismiss  # type: ignore[method-assign]
    return runner


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_workbench_skips_login_click():
    state = await _runner(None)._workbench_login_submit_state()
    assert state == "skip"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_popup_is_not_login_complete():
    runner = _runner({"text": "请确认交易承诺", "button": None})
    state = await runner._workbench_login_submit_state()
    assert state == "popup"
    assert (
        login_submit_decision(
            force_login=False,
            is_login=True,
            on_workbench=True,
            popup_left=True,
        )
        == "popup"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_force_login_does_not_reuse_previous_station_workbench():
    runner = _runner({"text": "上一站的弹窗", "button": None}, force=True)
    state = await runner._workbench_login_submit_state()
    assert state == "continue"
