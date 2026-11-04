"""交易日历数据整形：把页面日历事件整理成稳定的 CSV 结构。

页面（广东电力市场交易系统 → 中长期交易首页）的日历是 FullCalendar 组件：

* 月视图：每个 ``td[data-date]`` 里放当天的交易事件，事件标题如"周双边协商交易"；
* 周/日视图：事件可能额外带 ``.fc-event-time`` 时间文本；
* 事件状态由自定义样式类 ``tradeBg`` / ``NotBg`` / ``greyBg`` 表示
  （交易中 / 未开始 / 已结束）。

页面本身没有"导出 CSV"按钮，所以工作流用 ``extract_calendar`` 读取 DOM，再由本模块
统一成固定列。列结构一旦固定，后续即使页面增加"时段/时间范围"也只需补值，不会改表头。
"""

from __future__ import annotations

import calendar
import csv
import io
import re
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

__all__ = [
    "CALENDAR_COLUMNS",
    "EMPTY_DAY_STATUS",
    "build_calendar_table",
    "calendar_csv_text",
    "normalize_month",
]

CALENDAR_COLUMNS: tuple[str, ...] = (
    "日期",
    "星期",
    "时段",
    "交易名称",
    "交易时间范围",
    "申报时间",
    "类型",
    "状态",
    "视图",
    "月份",
    "采集时间",
)

EMPTY_DAY_STATUS = "无交易安排"
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
_PERIOD_RE = re.compile(r"^(上午|下午)\s*[:：]?\s*(.*)$")
_STATUS_ALIASES = {
    "tradeBg": "交易中",
    "NotBg": "未开始",
    "greyBg": "已结束",
    "grayBg": "已结束",
}


def normalize_month(value: str | None) -> str:
    """把 ``2026-3`` / ``2026年3月`` / ``2026-03`` 归一成 ``2026-03``。"""
    text = str(value or "").strip()
    match = _MONTH_RE.match(text)
    if match:
        month = int(match.group(2))
        if 1 <= month <= 12:
            return f"{int(match.group(1)):04d}-{month:02d}"
    match = re.search(r"(\d{4})\D+(\d{1,2})", text)
    if match:
        month = int(match.group(2))
        if 1 <= month <= 12:
            return f"{int(match.group(1)):04d}-{month:02d}"
    return ""


def _month_days(month: str) -> list[str]:
    year, mon = (int(part) for part in month.split("-", 1))
    last_day = calendar.monthrange(year, mon)[1]
    return [f"{year:04d}-{mon:02d}-{day:02d}" for day in range(1, last_day + 1)]


def _weekday_zh(iso_date: str) -> str:
    try:
        return _WEEKDAYS[date.fromisoformat(iso_date).weekday()]
    except ValueError:
        return ""


def _split_period(period: str, time_range: str, apply_time: str = "") -> tuple[str, str]:
    """拆出上午/下午。

    优先用显式时段；其次看申报时间（周视图的 ``申报时间``），最后看交易时间范围
    里是否直接写了“上午/下午”。申报时间举例：
    ``10-26 14:00 ~ 10-26 15:30`` → 下午。
    """
    period = str(period or "").strip()
    time_range = str(time_range or "").strip()
    apply_time = str(apply_time or "").strip()
    if period:
        return period, time_range

    match = _PERIOD_RE.match(apply_time)
    if match:
        return match.group(1), time_range
    match = _PERIOD_RE.match(time_range)
    if match:
        return match.group(1), match.group(2).strip()
    match = re.search(r"(\d{1,2}):\d{2}", apply_time)
    if match:
        hour = int(match.group(1))
        return ("上午" if hour < 12 else "下午"), time_range
    return "", time_range


def _normalize_status(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in _STATUS_ALIASES:
        return _STATUS_ALIASES[text]
    # 兼容 ``calendar-month greyBg`` 这类整串 class。
    for alias, label in _STATUS_ALIASES.items():
        if re.search(rf"(^|\s){re.escape(alias)}(\s|$)", text):
            return label
    return text


def _iter_raw_rows(raw_rows: Iterable[dict[str, Any]]) -> Iterable[dict[str, str]]:
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        iso_date = str(item.get("date") or item.get("日期") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso_date):
            continue
        try:
            date.fromisoformat(iso_date)
        except ValueError:
            continue
        apply_time = str(item.get("apply_time") or item.get("申报时间") or "").strip()
        period, time_range = _split_period(
            str(item.get("period") or item.get("时段") or ""),
            str(item.get("time_range") or item.get("交易时间范围") or ""),
            apply_time,
        )
        yield {
            "date": iso_date,
            "weekday": _weekday_zh(iso_date),
            "period": period,
            "event": str(item.get("event") or item.get("交易名称") or "").strip(),
            "time_range": time_range,
            "apply_time": apply_time,
            "event_type": str(item.get("event_type") or item.get("类型") or "").strip(),
            "status": _normalize_status(str(item.get("status") or item.get("状态") or "")),
        }


def build_calendar_table(
    raw_rows: Sequence[dict[str, Any]],
    *,
    month: str = "",
    view: str = "月",
    include_empty_days: bool = True,
    captured_at: str | None = None,
) -> list[list[str]]:
    """把页面事件整理成 ``[表头, 行...]``。

    - 同一天多个事件 → 多行；同一天无事件 → 一行"无交易安排"（可关闭）；
    - 行按日期升序，同一天保持页面顺序；
    - "时段"和"交易时间范围"在月视图可能为空，列仍保留，便于下游固定表头。
    """
    month = normalize_month(month)
    rows = list(_iter_raw_rows(raw_rows))
    if month:
        rows = [row for row in rows if row["date"].startswith(month)]
    elif rows:
        # 页面没给出月份时，用出现次数最多的月份，避免把跨月网格混进来。
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["date"][:7]] = counts.get(row["date"][:7], 0) + 1
        month = max(counts, key=counts.get)

    if captured_at is None:
        captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        captured_at = str(captured_at).strip()
    view = str(view or "月").strip() or "月"

    grouped: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    if include_empty_days and month:
        for iso_date in _month_days(month):
            grouped[iso_date] = []
    for row in rows:
        # 空事件行只用于告诉后续“这一天有格子”，不能当一条交易事件输出。
        if row["event"]:
            grouped.setdefault(row["date"], []).append(row)
        else:
            grouped.setdefault(row["date"], [])

    table: list[list[str]] = [list(CALENDAR_COLUMNS)]
    for iso_date in sorted(grouped):
        events = grouped[iso_date]
        if not events:
            if include_empty_days:
                table.append([
                    iso_date,
                    _weekday_zh(iso_date),
                    "",
                    "",
                    "",
                    "",
                    "",
                    EMPTY_DAY_STATUS,
                    view,
                    iso_date[:7],
                    captured_at,
                ])
            continue
        for row in events:
            table.append([
                iso_date,
                row["weekday"],
                row["period"],
                row["event"],
                row["time_range"],
                row["apply_time"],
                row["event_type"],
                row["status"],
                view,
                iso_date[:7],
                captured_at,
            ])
    return table


def calendar_csv_text(table: Sequence[Sequence[str]]) -> str:
    """序列化成 Excel 友好的 CSV 文本（调用方用 utf-8-sig 写文件）。"""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerows(table)
    return output.getvalue()


def merge_calendar_rows(
    existing_rows: Iterable[dict[str, Any]],
    new_table: Sequence[Sequence[str]],
) -> list[list[str]]:
    """把新抓到的行合并进已有 CSV 行。

    - 以 ``日期 + 交易名称 + 申报时间 + 交易时间范围`` 为键，同一事件重抓时用新值覆盖；
    - 旧文件是 9 列版本时，缺少的列按空串补齐；
    - 按日期稳定排序，同一天保持原顺序。
    """
    header = list(CALENDAR_COLUMNS)
    merged: OrderedDict[tuple[str, str, str, str], list[str]] = OrderedDict()

    def add(values: dict[str, str]) -> None:
        iso_date = values.get("日期", "").strip()
        if not iso_date:
            return
        key = (
            iso_date,
            values.get("交易名称", ""),
            values.get("申报时间", ""),
            values.get("交易时间范围", ""),
        )
        merged[key] = [values.get(column, "") for column in header]

    for row in existing_rows:
        if isinstance(row, dict):
            add({column: str(row.get(column, "") or "") for column in header})
    for row in list(new_table)[1:]:
        add(dict(zip(header, (str(value) for value in row), strict=False)))

    ordered = sorted(merged.items(), key=lambda item: item[0][0])
    return [header] + [values for _, values in ordered]
