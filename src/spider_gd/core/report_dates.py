"""识别运行日报标题里的日期，并按目标日期筛选公告。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

__all__ = [
    "dates_in_report_title",
    "is_report_workflow",
    "report_file_has_year",
    "report_fill_value",
    "report_matches_date",
    "report_search_query",
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


def is_report_workflow(name: str) -> bool:
    """这个工作流是不是运行日报。

    Args:
        name: 工作流名或任务里的指标名。

    Returns:
        bool: 名称里含有「运行日报」时为 True。

    Examples:
        >>> is_report_workflow("运行日报")
        True
        >>> is_report_workflow("交易日历")
        False
    """
    return "运行日报" in str(name or "")


def report_search_query(target: date) -> str:
    """运行日报查询词，带年份，避免只填「11.13」点到上一年。

    Args:
        target: 要查的业务日期。

    Returns:
        str: 例如 ``2026年11月运行日报（11.13）``。

    Examples:
        >>> report_search_query(date(2026, 11, 13))
        '2026年11月运行日报（11.13）'
        >>> report_search_query(date(2026, 11, 5))
        '2026年11月运行日报（11.05）'
    """
    return (
        f"{target.year}年{target.month}月运行日报"
        f"（{target.month}.{target.day:02d}）"
    )


def report_fill_value(value: str, target: date) -> str | None:
    """运行日报的日期填空换成带年份的查询词。

    不是日期占位符、也不是目标日的 ``月.日`` 简写时返回 None，调用方原样填写。

    Args:
        value: 步骤里的填空。
        target: 这一轮的业务日期。

    Returns:
        str | None: 查询词。不该改时为 None。

    Examples:
        >>> report_fill_value("{target}", date(2026, 11, 13))
        '2026年11月运行日报（11.13）'
        >>> report_fill_value("11.13", date(2026, 11, 13))
        '2026年11月运行日报（11.13）'
        >>> report_fill_value("其他", date(2026, 11, 13)) is None
        True
    """
    text = str(value or "").strip()
    short = f"{target.month}.{target.day:02d}"
    if text in {"{target}", "target", "today", short}:
        return report_search_query(target)
    if text == "yesterday":
        return report_search_query(target - timedelta(days=1))
    matched = re.fullmatch(r"-(\d+)d", text)
    if matched:
        return report_search_query(target - timedelta(days=int(matched.group(1))))
    return None


def report_file_has_year(filename: str, payload: bytes, year: int) -> bool:
    """原文件名或 PDF 标题里是否有目标年份。

    Args:
        filename: 站点给出的原始文件名。
        payload: 文件内容。文件名没有年份时再看 PDF 标题。
        year: 目标年份。

    Returns:
        bool: 对得上时为 True。

    Examples:
        >>> report_file_has_year("2026年11月运行日报（11.13）.pdf", b"", 2026)
        True
        >>> report_file_has_year("11.13.pdf", b"/Title (2025 report)", 2026)
        False
    """
    if str(year) in Path(str(filename or "")).name:
        return True
    return str(year) in _pdf_title(payload or b"")


def _pdf_title(payload: bytes) -> str:
    """读取 PDF 信息字典里的标题。读不到时为空字符串。"""
    match = re.search(br"/Title\s*\((.*?)\)", payload, re.S)
    if match:
        raw = match.group(1)
        return raw.decode("utf-8", "replace").replace("\\(", "(").replace("\\)", ")")
    hex_title = re.search(br"/Title\s*<([0-9A-Fa-f\s]+)>", payload)
    if not hex_title:
        return ""
    digits = re.sub(br"\s+", b"", hex_title.group(1))
    if len(digits) < 4 or len(digits) % 2:
        return ""
    try:
        blob = bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return ""
    if blob.startswith(b"\xfe\xff"):
        return blob[2:].decode("utf-16-be", "replace")
    return blob.decode("utf-8", "replace")


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
