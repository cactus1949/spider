"""自动补漏规划（含防雪崩限制）。

基于 :func:`detect_gaps` 找出缺口日期，但**不无脑全量回补**：
- 单次补漏 endpoint/日期上限（``max_items_per_run``）：避免一次淹没系统。
- 频率上限（``min_interval_sec``）：两次补漏之间的冷却，防抖。
- 优先级：补漏任务优先级低于正常定时采集（由调用方在调度层体现）。

本模块只做"算出该补哪些"的纯逻辑，便于单测；实际触发由编排器执行。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from loguru import logger

from spider_gd.core.pipeline.data_processor import detect_gaps


@dataclass
class BackfillPolicy:
    """补漏防雪崩策略。"""

    max_items_per_run: int = 5  # 单次最多补几个日期
    min_interval_sec: float = 3600.0  # 两次补漏最小间隔（频率上限）
    priority: int = -1  # 低于正常定时采集（数值越小优先级越低）


@dataclass
class BackfillPlan:
    indicator: str
    dates: list[str]
    skipped_cooldown: bool = False
    total_gaps: int = 0


def plan_backfill(
    *,
    indicator: str,
    present_dates: Sequence[str],
    start: str,
    end: str,
    policy: BackfillPolicy,
    last_run_ts: float | None,
    now_ts: float,
) -> BackfillPlan:
    """计算本次应补漏的日期（已应用防雪崩限制）。

    - 若距上次补漏不足 ``min_interval_sec``：返回空计划（冷却中）。
    - 否则取缺口的前 ``max_items_per_run`` 个（升序，优先补早期缺口）。
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < policy.min_interval_sec:
        logger.debug("补漏冷却中（{}），跳过", indicator)
        return BackfillPlan(indicator=indicator, dates=[], skipped_cooldown=True)

    missing = detect_gaps(present_dates, start, end)
    capped = missing[: policy.max_items_per_run]
    if len(missing) > len(capped):
        logger.info(
            "补漏 {}：缺口 {} 个，本次限额回补 {} 个（防雪崩）",
            indicator, len(missing), len(capped),
        )
    return BackfillPlan(
        indicator=indicator, dates=capped, total_gaps=len(missing)
    )
