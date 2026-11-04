"""数据/日志保留期清理：按保留天数删除过期的日期分层目录。

采集数据目录结构为 ``{data_dir}/{YYYY-MM}/{YYYY-MM-DD}/...``。清理以"年月日"目录为单位，
早于保留期的整目录删除。可由调度器周期触发，或磁盘不足时按需触发。
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger


def _parse_day_dir(name: str) -> datetime | None:
    try:
        return datetime.strptime(name, "%Y-%m-%d")
    except ValueError:
        return None


def cleanup_old_data(
    data_dir: str | Path,
    *,
    retention_days: int,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[str]:
    """删除 ``data_dir`` 下早于保留期的「年月日」目录，返回被清理（或将清理）的路径。"""
    root = Path(data_dir)
    if not root.exists() or retention_days <= 0:
        return []
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)

    removed: list[str] = []
    for month_dir in sorted(root.glob("*-*")):
        if not month_dir.is_dir():
            continue
        for day_dir in sorted(month_dir.glob("*-*-*")):
            if not day_dir.is_dir():
                continue
            day = _parse_day_dir(day_dir.name)
            if day is None or day >= cutoff:
                continue
            removed.append(str(day_dir))
            if not dry_run:
                shutil.rmtree(day_dir, ignore_errors=True)
        # 清空的月目录顺手移除
        if not dry_run and month_dir.exists() and not any(month_dir.iterdir()):
            month_dir.rmdir()

    if removed:
        logger.info(
            "保留期清理：{} 个过期日期目录（retention={}d, dry_run={}）",
            len(removed), retention_days, dry_run,
        )
    return removed
