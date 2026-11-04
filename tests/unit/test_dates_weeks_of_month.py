"""按月选、按周执行：一个月拆成与之相交的周（周一列表）。"""

from __future__ import annotations

from datetime import date

import pytest

from spider_gd.core.dates import weeks_of_month


@pytest.mark.unit
def test_weeks_of_month_october_2026():
    # 10-01 是周四：首周从 09-28 开始；末周 10-26 覆盖到 11-01。
    assert weeks_of_month(date(2026, 10, 1)) == [
        date(2026, 9, 28),
        date(2026, 10, 5),
        date(2026, 10, 12),
        date(2026, 10, 19),
        date(2026, 10, 26),
    ]


@pytest.mark.unit
def test_weeks_of_month_six_weeks():
    # 2026-08-01 是周六：首周 07-27，末周 08-31，共 6 周。
    weeks = weeks_of_month(date(2026, 8, 1))
    assert weeks[0] == date(2026, 7, 27)
    assert weeks[-1] == date(2026, 8, 31)
    assert len(weeks) == 6


@pytest.mark.unit
def test_weeks_of_month_starts_on_monday():
    # 2026-06-01 是周一：不含上一月的周，共 5 周。
    weeks = weeks_of_month(date(2026, 6, 1))
    assert weeks[0] == date(2026, 6, 1)
    assert weeks[-1] == date(2026, 6, 29)
    assert len(weeks) == 5


@pytest.mark.unit
def test_weeks_of_month_accepts_any_day_in_month():
    assert weeks_of_month(date(2026, 10, 31)) == weeks_of_month(date(2026, 10, 1))