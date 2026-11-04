"""交易日历 CSV 结构：固定表头、同日多事件、空日期占位。"""

from __future__ import annotations

import csv
from datetime import date

import pytest
from calendar_data import (
    CALENDAR_COLUMNS,
    EMPTY_DAY_STATUS,
    build_calendar_table,
    calendar_csv_text,
    merge_calendar_rows,
    normalize_month,
)
from workflow_runner import WorkflowExecutionError, WorkflowRunner


@pytest.mark.unit
def test_normalize_month_accepts_common_shapes():
    assert normalize_month("2026-10") == "2026-10"
    assert normalize_month("2026-3") == "2026-03"
    assert normalize_month("2026年3月") == "2026-03"
    assert normalize_month("") == ""
    assert normalize_month("2026-13") == ""


@pytest.mark.unit
def test_build_table_keeps_multiple_events_and_empty_days():
    raw = [
        {"date": "2026-10-08", "event": "多月连续集中交易", "status": "greyBg"},
        {"date": "2026-10-08", "event": "周双边协商交易", "status": "greyBg"},
        {"date": "2026-10-09", "event": "", "status": ""},
    ]

    table = build_calendar_table(
        raw,
        month="2026-10",
        view="月",
        include_empty_days=True,
        captured_at="2026-10-30 11:17:47",
    )

    assert list(table[0]) == list(CALENDAR_COLUMNS)
    assert len(table) == 1 + 31 + 1  # 31 天 + 10-08 的第二个事件
    assert table[8] == [
        "2026-10-08", "周四", "", "多月连续集中交易", "", "", "", "已结束", "月", "2026-10",
        "2026-10-30 11:17:47",
    ]
    assert table[9][3] == "周双边协商交易"
    assert table[10][7] == EMPTY_DAY_STATUS


@pytest.mark.unit
def test_build_table_splits_period_and_ignores_other_months():
    raw = [
        {
            "date": "2026-10-29",
            "event": "周双边协商交易",
            "time_range": "上午 09:00-11:00",
            "status": "交易中",
        },
        {"date": "2026-11-01", "event": "月度集中竞争交易", "status": "未开始"},
    ]

    table = build_calendar_table(
        raw, month="2026-10", view="周", include_empty_days=False, captured_at="x"
    )

    assert [row[0] for row in table[1:]] == ["2026-10-29"]
    assert table[1][2] == "上午"
    assert table[1][3] == "周双边协商交易"
    assert table[1][4] == "09:00-11:00"
    assert table[1][7] == "交易中"
    assert table[1][8] == "周"


@pytest.mark.unit
def test_calendar_csv_is_excel_friendly():
    table = build_calendar_table(
        [{"date": "2026-10-01", "event": "周双边协商交易", "status": "已结束"}],
        month="2026-10",
        include_empty_days=False,
        captured_at="2026-10-30 12:00:00",
    )

    text = calendar_csv_text(table)

    assert text.startswith("日期,星期,时段,交易名称")
    assert "\r\n" in text


class _FakeCalendarPage:
    def __init__(self, payload):
        self.payload = payload

    def is_closed(self):
        return False

    async def evaluate(self, _script, _selector):
        return self.payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_calendar_writes_month_named_csv(tmp_path):
    runner = WorkflowRunner({}, download_dir=str(tmp_path), target_date=date(2026, 10, 29))
    runner.pages = [_FakeCalendarPage({
        "view": "月",
        "month": "2026-10",
        "rows": [
            {"date": "2026-10-01", "event": "周双边协商交易", "time_range": "", "status": "已结束"},
        ],
    })]

    await runner._do_extract_calendar({"selector": ".calendar-box"})

    output = tmp_path / "交易日历2026-10.csv"
    assert output.exists()
    content = output.read_text(encoding="utf-8-sig")
    assert "日期,星期,时段,交易名称" in content
    assert "2026-10-01,周四,,周双边协商交易,,,,已结束,月,2026-10," in content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_calendar_refuses_empty_page(tmp_path):
    runner = WorkflowRunner({}, download_dir=str(tmp_path), target_date=date(2026, 10, 29))
    runner.pages = [_FakeCalendarPage({"view": "月", "month": "2026-10", "rows": []})]

    with pytest.raises(WorkflowExecutionError, match="没有找到任何日期"):
        await runner._do_extract_calendar({"selector": ".calendar-box"})


@pytest.mark.unit
def test_resolve_save_path_supports_month_iso(tmp_path):
    runner = WorkflowRunner({}, download_dir=str(tmp_path), target_date=date(2026, 10, 29))

    path = runner._resolve_save_path("交易日历{month_iso}.csv")

    assert path.name == "交易日历2026-10.csv"


@pytest.mark.unit
def test_build_week_detail_row_and_period_from_apply_time():
    table = build_calendar_table(
        [
            {
                "date": "2026-10-26",
                "event": "2026年10月周双边协商交易(2026-10-26)（灾备）",
                "time_range": "2026-10-28 ~ 2026-10-31",
                "apply_time": "10-26 14:00 ~ 10-26 15:30",
                "event_type": "正式",
                "status": "已结束",
            }
        ],
        month="2026-10",
        view="周",
        include_empty_days=False,
        captured_at="2026-10-30 13:10:00",
    )

    assert table[1] == [
        "2026-10-26",
        "周一",
        "下午",
        "2026年10月周双边协商交易(2026-10-26)（灾备）",
        "2026-10-28 ~ 2026-10-31",
        "10-26 14:00 ~ 10-26 15:30",
        "正式",
        "已结束",
        "周",
        "2026-10",
        "2026-10-30 13:10:00",
    ]


@pytest.mark.unit
def test_merge_updates_same_event_and_keeps_others():
    existing = [
        {
            "日期": "2026-10-26",
            "交易名称": "事件A",
            "申报时间": "10-26 14:00 ~ 10-26 15:30",
            "交易时间范围": "2026-10-28 ~ 2026-10-31",
            "状态": "未开始",
        }
    ]
    new_table = build_calendar_table(
        [
            {
                "date": "2026-10-26",
                "event": "事件A",
                "time_range": "2026-10-28 ~ 2026-10-31",
                "apply_time": "10-26 14:00 ~ 10-26 15:30",
                "event_type": "正式",
                "status": "已结束",
            },
            {
                "date": "2026-10-27",
                "event": "事件B",
                "time_range": "2026-10-29 ~ 2026-10-31",
                "apply_time": "10-27 09:00 ~ 10-27 10:00",
                "event_type": "正式",
                "status": "已结束",
            },
        ],
        month="2026-10",
        view="周",
        include_empty_days=False,
        captured_at="2026-10-30 13:10:00",
    )

    merged = merge_calendar_rows(existing, new_table)

    assert len(merged) == 3
    by_title = {row[3]: row for row in merged[1:]}
    assert by_title["事件A"][7] == "已结束"   # 同一事件重抓后更新状态
    assert by_title["事件A"][5] == "10-26 14:00 ~ 10-26 15:30"
    assert by_title["事件B"][2] == "上午"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_calendar_merge_accumulates(tmp_path):
    runner = WorkflowRunner({}, download_dir=str(tmp_path), target_date=date(2026, 10, 26))

    def payload(events):
        return {
            "view": "周",
            "month": "2026-10",
            "rows": [
                {
                    "date": "2026-10-26",
                    "event": title,
                    "time_range": "2026-10-28 ~ 2026-10-31",
                    "apply_time": "10-26 14:00 ~ 10-26 15:30",
                    "event_type": "正式",
                    "status": status,
                }
                for title, status in events
            ],
        }

    runner.pages = [_FakeCalendarPage(payload([("事件A", "未开始")]))]
    await runner._do_extract_calendar({
        "selector": ".calendar-box:has(.fc-dayGridWeek-view)",
        "save_as": "交易日历明细{month_iso}.csv",
        "month_mode": "target",
        "include_empty_days": False,
        "merge": True,
    })
    runner.pages = [_FakeCalendarPage(payload([("事件A", "已结束"), ("事件B", "已结束")]))]
    await runner._do_extract_calendar({
        "selector": ".calendar-box:has(.fc-dayGridWeek-view)",
        "save_as": "交易日历明细{month_iso}.csv",
        "month_mode": "target",
        "include_empty_days": False,
        "merge": True,
    })

    output = tmp_path / "交易日历明细2026-10.csv"
    rows = list(csv.DictReader(output.open(encoding="utf-8-sig", newline="")))
    assert [row["交易名称"] for row in rows] == ["事件A", "事件B"]
    assert rows[0]["状态"] == "已结束"
