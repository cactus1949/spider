"""运行日报标题日期识别。"""

from __future__ import annotations

from datetime import date

import pytest

from spider_gd.core.report_dates import (
    dates_in_report_title,
    report_matches_date,
    select_report_notices,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("广东电力现货市场2026年10月运行日报（10.27）", date(2026, 10, 27)),
        ("广东电力现货市场2026年10月27日运行日报", date(2026, 10, 27)),
        ("广东电力市场运行日报（2026-10-27）", date(2026, 10, 27)),
        ("运行日报（10月27日）", date(2026, 10, 27)),
        ("现货市场2026年10月运行日报（10.26）", date(2026, 10, 26)),
    ],
)
def test_dates_in_report_title(title: str, expected: date):
    assert expected in dates_in_report_title(title, 2026)


@pytest.mark.unit
def test_report_matches_title_date_not_publish_date():
    # 报告 10.27 发布在 10.28，目标日期 10.27 仍应命中
    assert report_matches_date(
        "广东电力现货市场2026年10月运行日报（10.27）",
        "2026-10-28",
        date(2026, 10, 27),
    )
    assert not report_matches_date(
        "广东电力现货市场2026年10月运行日报（10.27）",
        "2026-10-28",
        date(2026, 10, 28),
    )


@pytest.mark.unit
def test_report_matches_falls_back_to_publish_time():
    assert report_matches_date("运行日报", "2026-10-28", date(2026, 10, 28))


@pytest.mark.unit
def test_select_report_notices_deduplicates():
    notices = [
        {"noticeId": 1, "noticeTitle": "运行日报（10.27）", "publishTime": "2026-10-28"},
        {"noticeId": 1, "noticeTitle": "运行日报（10.27）", "publishTime": "2026-10-28"},
        {"noticeId": 2, "noticeTitle": "运行日报（10.28）", "publishTime": "2026-10-29"},
    ]
    picked = select_report_notices(notices, date(2026, 10, 27))
    assert [item["noticeId"] for item in picked] == [1]
