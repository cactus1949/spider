"""工作台弹窗：没有、一个、两个、只有危险按钮、超过上限。"""

from __future__ import annotations

import pytest

from spider_gd.core.crawler.workbench_popup import (
    POPUP_PAUSE_MESSAGE,
    close_workbench_popups,
)


def _popup(text: str, *, y: float = 40, safe: bool = True) -> dict:
    return {
        "text": text,
        "button": {
            "label": "关闭",
            "x": 10,
            "y": y,
            "width": 64,
            "height": 28,
            "safe": safe,
        } if safe else None,
    }


async def _run(scripted: list[dict | None]):
    clicks: list[dict] = []
    logs: list[str] = []
    pauses: list[str] = []
    sleeps = {"n": 0}

    async def read():
        if not scripted:
            return None
        return scripted.pop(0)

    async def click(box):
        clicks.append(box)

    async def pause(message):
        pauses.append(message)

    async def sleep():
        sleeps["n"] += 1

    closed = await close_workbench_popups(read, click, pause, sleep, logs.append)
    return closed, clicks, logs, pauses, sleeps["n"]


@pytest.mark.unit
async def test_no_popup_does_nothing():
    closed, clicks, logs, pauses, sleeps = await _run([None])
    assert closed == 0
    assert clicks == []
    assert logs == []
    assert pauses == []
    assert sleeps == 0


@pytest.mark.unit
async def test_one_popup_is_closed_and_logged():
    text = "贵单位仍有协议待签署。为保障电力交易业务正常办理。"
    closed, clicks, logs, pauses, sleeps = await _run([_popup(text), None])
    assert closed == 1
    assert len(clicks) == 1
    assert logs == [text]
    assert pauses == []
    assert sleeps == 1


@pytest.mark.unit
async def test_two_stacked_popups_are_closed_in_order():
    first = _popup("第一个弹窗", y=40)
    second = _popup("第二个弹窗", y=80)
    closed, clicks, logs, pauses, _sleeps = await _run([first, second, None])
    assert closed == 2
    assert [item["y"] for item in clicks] == [40, 80]
    assert logs == ["第一个弹窗", "第二个弹窗"]
    assert pauses == []


@pytest.mark.unit
async def test_only_dangerous_button_pauses_without_clicking():
    closed, clicks, logs, pauses, sleeps = await _run([
        {"text": "请去签署协议", "button": None},
    ])
    assert closed == 0
    assert clicks == []
    assert logs == []
    assert pauses == [POPUP_PAUSE_MESSAGE]
    assert sleeps == 0


@pytest.mark.unit
async def test_more_than_five_popups_pauses_after_the_limit():
    dialogs = [_popup(f"弹窗{index}", y=20 + index) for index in range(6)]
    closed, clicks, logs, pauses, _sleeps = await _run(dialogs)
    assert closed == 5
    assert len(clicks) == 5
    assert logs == [f"弹窗{index}" for index in range(5)]
    assert pauses == [POPUP_PAUSE_MESSAGE]


@pytest.mark.unit
async def test_popup_that_stays_after_click_pauses():
    stuck = _popup("关不掉")
    closed, clicks, _logs, pauses, _sleeps = await _run([stuck, stuck])
    assert closed == 1
    assert len(clicks) == 1
    assert pauses == [POPUP_PAUSE_MESSAGE]
