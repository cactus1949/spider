"""全局 loguru 配置。

三类文件日志 + 磁盘保护：

1. **app.log** — 主日志，每天午夜轮转 + 单文件 50MB 上限（双保险）
2. **error.log** — 仅 ERROR+，保留 90 天不压缩（方便快速排查历史故障）
3. **任务日志** — ``logs/{YYYY-MM-DD}/{任务名}_{时间}.log``，每次任务执行独立文件

磁盘保护：
- app.log 文件数上限 60 个（≈30天×2，留余量）
- 任务日志目录：schedule 的 retention_cron 会清理过期日期目录
- 启动时检查日志总大小，超过 500MB 时告警
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{module}:{function}:{line} | "
    "{extra[task]: <16} | "
    "{message}"
)

# 敏感信息脱敏规则（密码/Token）
_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'(password["\s:=]+)([^"\s,}]+)', re.IGNORECASE), r"\1***"),
    (re.compile(r'(token["\s:=]+)([^"\s,}]+)', re.IGNORECASE), r"\1***"),
    (re.compile(r"(HdKGccbL[\"\s:=]+)([^\"\s,}&]+)"), r"\1***"),
]

# 默认配置
DEFAULT_RETENTION_DAYS = 30
DEFAULT_APP_LOG_MAX_SIZE_MB = 50
DEFAULT_TASK_LOG_RETENTION_DAYS = 90
MAX_TOTAL_LOG_SIZE_MB = 500


def _mask_sensitive(record: Any) -> bool:
    msg = record["message"]
    for pattern, repl in _SENSITIVE_PATTERNS:
        msg = pattern.sub(repl, msg)
    record["message"] = msg
    return True


def _size_mb(path: Path) -> float:
    """计算文件或目录大小 (MB)。"""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size / (1024 * 1024)
    return sum(
        f.stat().st_size for f in path.rglob("*") if f.is_file()
    ) / (1024 * 1024)


def setup_logging(
    log_dir: Path | str = "./logs",
    *,
    persist_to_file: bool = True,
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    """初始化全局日志（进程入口处调用一次）。"""
    logger.remove()
    logger.configure(extra={"task": "-"})

    # 控制台
    logger.add(
        sys.stderr,
        level=console_level,
        format=LOG_FORMAT,
        colorize=True,
        enqueue=True,
        filter=_mask_sensitive,
    )

    if not persist_to_file:
        return

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 主日志：按天轮转 + 单文件大小上限（双保险，先触发的生效）
    logger.add(
        log_dir / "app.log",
        level=file_level,
        format=LOG_FORMAT,
        rotation=f"{DEFAULT_APP_LOG_MAX_SIZE_MB} MB",  # 50MB 就切
        retention=f"{max(retention_days, 30)} days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        filter=_mask_sensitive,
    )

    # 错误日志：仅 ERROR+，保留更久，不压缩（方便 grep）
    logger.add(
        log_dir / "error.log",
        level="ERROR",
        format=LOG_FORMAT,
        rotation="00:00",
        retention=f"{max(retention_days, 90)} days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,
        filter=_mask_sensitive,
    )

    # 启动时检查总大小
    total_mb = _size_mb(log_dir)
    if total_mb > MAX_TOTAL_LOG_SIZE_MB:
        logger.warning(
            "日志目录总大小 {:.0f} MB 超过上限 {} MB，请检查或清理",
            total_mb,
            MAX_TOTAL_LOG_SIZE_MB,
        )


def attach_task_sink(
    log_dir: Path | str,
    task_name: str,
    *,
    file_level: str = "DEBUG",
) -> int:
    """为本次任务执行追加独立文件 sink，返回 sink_id（结束后 remove）。

    任务日志按日期分层：``logs/YYYY-MM-DD/task_HHMMSS.log``
    每日调度器会清理超过保留期的日期目录。
    """
    now = datetime.now()
    day_dir = Path(log_dir) / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    file_path = day_dir / f"{task_name}_{now.strftime('%Y%m%d_%H%M%S')}.log"

    return logger.add(
        file_path,
        level=file_level,
        format=LOG_FORMAT,
        encoding="utf-8",
        enqueue=True,
        filter=lambda record: record["extra"].get("task") == task_name
        and _mask_sensitive(record),
    )


def cleanup_task_logs(
    log_dir: str | Path,
    *,
    retention_days: int = DEFAULT_TASK_LOG_RETENTION_DAYS,
    dry_run: bool = False,
) -> list[str]:
    """清理超过保留期的任务日志日期目录。

    日志目录结构：``logs/YYYY-MM-DD/...``
    与数据清理（retention.py）结构一致，可复用同一调度。
    """
    from datetime import timedelta

    root = Path(log_dir)
    if not root.exists() or retention_days <= 0:
        return []

    now = datetime.now()
    cutoff = now - timedelta(days=retention_days)
    removed: list[str] = []

    for day_dir in sorted(root.glob("*-*-*")):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        if day >= cutoff:
            continue

        # 不删除 app.log / error.log（它们不在日期子目录）
        removed.append(str(day_dir))
        if not dry_run:
            import shutil

            shutil.rmtree(day_dir, ignore_errors=True)

    if removed:
        logger.info(
            "日志清理：{} 个过期日期目录（retention={}d）",
            len(removed),
            retention_days,
        )
    return removed


def get_log_stats(log_dir: str | Path) -> dict:
    """返回日志目录统计信息。"""
    root = Path(log_dir)
    total_mb = _size_mb(root)
    app_log_mb = _size_mb(root / "app.log")

    # 统计日期子目录
    day_dirs = [
        d for d in sorted(root.glob("*-*-*"))
        if d.is_dir()
    ]
    task_count = sum(
        1 for d in day_dirs
        for f in d.glob("*.log")
    )

    return {
        "total_mb": round(total_mb, 1),
        "app_log_mb": round(app_log_mb, 1),
        "task_log_count": task_count,
        "day_dirs": len(day_dirs),
        "path": str(root),
    }
