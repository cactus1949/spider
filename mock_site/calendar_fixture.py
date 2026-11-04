"""交易日历 mock 的现场样本。

原始页面 HTML（``交易日历.txt``）已经不在仓库里。这里改用当时从页面抽出的两份 CSV：

* ``交易日历2026-10.csv``：月视图，只有短标题和状态；
* ``交易日历2026-10-周明细.csv``：2026-10-26 那一周的周视图明细。

页面脚本用月视图短标题补全其余日期的标的时间、申报时间；样本里没有的月份再按同一套卡片结构生成。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

__all__ = [
    "calendar_bootstrap",
    "load_captured_week",
    "load_month_view",
]

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_MONTH_CSV = _FIXTURES / "交易日历2026-10.csv"
_WEEK_CSV = _FIXTURES / "交易日历2026-10-周明细.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    """读取带 BOM 的日历 CSV。

    Args:
        path (Path): CSV 路径。第一行必须是表头。

    Returns:
        list[dict[str, str]]: 数据行。缺文件时返回空列表，避免示例在路径异常时抛错。

    Examples:
        >>> isinstance(_read_csv(_WEEK_CSV), list)
        True
    """
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_month_view(path: Path | None = None) -> tuple[dict[str, list[str]], list[str]]:
    """读取月视图短标题，并列出没有任何交易的日期。

    Args:
        path (Path | None): 月视图 CSV。默认使用 2026-10 现场样本。

    Returns:
        tuple[dict[str, list[str]], list[str]]: 前者按日期保留页面顺序的短标题，
        后者是样本中标记为「无交易安排」的日期。

    Examples:
        >>> titles, empty = load_month_view()
        >>> titles["2026-10-01"]
        ['周双边协商交易']
        >>> "2026-10-30" in empty
        True
    """
    rows = _read_csv(path or _MONTH_CSV)
    order: list[str] = []
    titles: dict[str, list[str]] = {}
    for row in rows:
        day = str(row.get("日期") or "").strip()
        if not day:
            continue
        if day not in titles and day not in order:
            order.append(day)
        name = str(row.get("交易名称") or "").strip()
        status = str(row.get("状态") or "").strip()
        if name and status != "无交易安排":
            titles.setdefault(day, []).append(name)
    empty = [day for day in order if day not in titles]
    return titles, empty


def load_captured_week(path: Path | None = None) -> dict[str, list[dict[str, str]]]:
    """读取现场周视图明细。

    Args:
        path (Path | None): 周明细 CSV。默认是 2026-10-26 那一周。

    Returns:
        dict[str, list[dict[str, str]]]: 键为申报日期。每条含
        ``title`` / ``delivery`` / ``apply`` / ``type`` / ``status``。

    Examples:
        >>> load_captured_week()["2026-10-26"][1]["apply"]
        '10-26 14:00 ~ 10-26 15:30'
    """
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in _read_csv(path or _WEEK_CSV):
        day = str(row.get("日期") or "").strip()
        title = str(row.get("交易名称") or "").strip()
        if not day or not title:
            continue
        grouped.setdefault(day, []).append(
            {
                "title": title,
                "delivery": str(row.get("交易时间范围") or "").strip(),
                "apply": str(row.get("申报时间") or "").strip(),
                "type": str(row.get("类型") or "").strip(),
                "status": str(row.get("状态") or "").strip(),
            }
        )
    return grouped


def calendar_bootstrap() -> dict[str, Any]:
    """组装交易日历页直接嵌入的样本。

    Returns:
        dict[str, Any]: ``captured`` 为周明细，``shorts`` 为月视图短标题，
        ``empty`` 为明确没有交易的日期。

    Examples:
        >>> sample = calendar_bootstrap()
        >>> sample["captured"]["2026-10-26"][1]["delivery"]
        '2026-10-28 ~ 2026-10-31'
        >>> "2026-10-31" in sample["empty"]
        True
    """
    shorts, empty = load_month_view()
    return {"captured": load_captured_week(), "shorts": shorts, "empty": empty}
