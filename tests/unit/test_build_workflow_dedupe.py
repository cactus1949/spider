"""录制步骤清洗：重复点击只保留一次，并支持交易日历提取步骤。"""

from __future__ import annotations

from build_workflow import _clean_steps, _dedupe_raw_steps


def _click(selector: str, at: int, text: str = "中长期交易") -> dict:
    return {"action": "click", "selector": selector, "_text": text, "_time": at}


def test_dedupe_merges_double_bound_click():
    raw = [
        _click("div.titleName___SP9tK", 1_793_330_188_958),
        _click("div.titleName___SP9tK", 1_793_330_188_958),
        {
            "action": "click",
            "selector": "div.left > div > label > span",
            "_text": "周",
            "_time": 1_793_330_239_531,
        },
    ]

    assert [step.get("_text") for step in _dedupe_raw_steps(raw)] == ["中长期交易", "周"]


def test_dedupe_keeps_deliberate_second_click():
    raw = [
        _click("button.export", 1000, "导出"),
        _click("button.export", 5000, "导出"),
    ]

    assert len(_dedupe_raw_steps(raw)) == 2


def test_clean_steps_adds_wait_before_calendar_extract():
    raw = [
        _click("text=\"中长期交易\"", 1000),
        {
            "action": "extract_calendar",
            "selector": ".calendar-box",
            "_text": "2026年10月",
            "_time": 3000,
        },
    ]

    steps = _clean_steps(raw)

    actions = [step["action"] for step in steps]
    assert actions == ["login", "click", "wait", "extract_calendar"]
    assert steps[3]["save_as"] == "交易日历{month_iso}.csv"
