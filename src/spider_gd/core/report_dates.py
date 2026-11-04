"""识别运行日报标题里的日期，并按目标日期筛选公告。"""

from __future__ import annotations

import re
from datetime import date

__all__ = [
    "dates_in_report_title",
    "report_matches_date",
    "select_report_notices",
]

_FULL_DATE = re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日")
_YEAR_MONTH = re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月")
_DOTTED_MD = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*[.．]\s*(?P<d>\d{1,2})(?!\d)")
_ISO_DATE = re.compile(r"(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})")
_MONTH_DAY = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日")


def _valid(value: date) -> date | None:
    try:
        return value
    except ValueError:
        return None


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def dates_in_report_title(title: str, default_year: int) -> list[date]:
    """从标题里尽量提取日期；支持 2026年10月27日、2026年10月…（10.27）等写法。"""
    text = str(title or "")
    found: list[date] = []

    for match in _FULL_DATE.finditer(text):
        item = _make_date(int(match["y"]), int(match["m"]), int(match["d"]))
        if item:
            found.append(item)

    # 2026年10月运行日报（10.27）：年份/月在前，具体日藏在后面的 10.27
    for year_month in _YEAR_MONTH.finditer(text):
        tail = text[year_month.end():]
        dotted = _DOTTED_MD.search(tail)
        if not dotted:
            continue
        # 只接受紧跟在年月后面的第一个“10.27”这类写法，避免抓到版本号。
        if dotted.start() > 24:
            continue
        item = _make_date(
            int(year_month["y"]),
            int(dotted["m"]),
            int(dotted["d"]),
        )
        if item:
            found.append(item)

    for match in _ISO_DATE.finditer(text):
        item = _make_date(int(match["y"]), int(match["m"]), int(match["d"]))
        if item:
            found.append(item)

    for match in _MONTH_DAY.finditer(text):
        item = _make_date(default_year, int(match["m"]), int(match["d"]))
        if item:
            found.append(item)

    # 没有完整年月日时，再用 10.27 这种简写。
    if not found:
        for match in _DOTTED_MD.finditer(text):
            item = _make_date(default_year, int(match["m"]), int(match["d"]))
            if item:
                found.append(item)

    # 去重，保持顺序
    result: list[date] = []
    for item in found:
        if item not in result:
            result.append(item)
    return result


def _date_from_text(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _ISO_DATE.search(text)
    if match:
        return _make_date(int(match["y"]), int(match["m"]), int(match["d"]))
    match = _FULL_DATE.search(text)
    if match:
        return _make_date(int(match["y"]), int(match["m"]), int(match["d"]))
    return None


def report_matches_date(title: str, publish_time: str, target: date) -> bool:
    """标题日期优先；标题没有日期时退回发布时间。"""
    dates = dates_in_report_title(title, target.year)
    if dates:
        return target in dates
    published = _date_from_text(publish_time)
    return published == target


def select_report_notices(
    notices: list[dict],
    target: date,
) -> list[dict]:
    """从列表接口返回的公告里挑出目标日期的运行日报。"""
    selected: list[dict] = []
    seen: set[int] = set()
    for notice in notices:
        if not isinstance(notice, dict):
            continue
        notice_id = notice.get("noticeId")
        if notice_id in seen:
            continue
        title = str(notice.get("noticeTitle") or "")
        publish_time = str(notice.get("publishTime") or "")
        if not report_matches_date(title, publish_time, target):
            continue
        if notice_id is not None:
            seen.add(notice_id)
        selected.append(notice)
    return selected
