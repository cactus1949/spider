"""指标失败后的重试、停任务和续跑判断。

可重试的失败先回到工作台再试。试完仍失败只记在这个指标上，继续下一个。
登录、证书、配置错误和同一轮里的第二次 HTTP 400 会停掉整次任务。
HTTP 400 是瑞数拦截：整次任务最多回工作台重试一次，避免连续刷新被封。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any


class FailureKind(str, Enum):
    """一条失败原因该怎么处理。"""

    RETRYABLE = "retryable"
    LOCAL = "local"
    GATEWAY_400 = "gateway_400"
    FATAL = "fatal"


DEFAULT_WORKBENCH_URL = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
DEFAULT_INDICATOR_RETRY = 3

_SECRET_PARTS = ("password", "token", "secret", "webhook", "sendkey", "send_key", "pin")


def classify_failure(reason: str) -> FailureKind:
    """按失败原因决定重试、跳过本指标，还是停掉整次任务。

    Args:
        reason (str): 工作流或登录步骤留下的失败说明。

    Returns:
        FailureKind: ``retryable`` 可在次数内重试；``local`` 不重试但继续下一个指标；
        ``gateway_400`` 需要整次任务最多一次的回工作台；``fatal`` 停止整个任务。

    Examples:
        >>> classify_failure("等待元素超时：.export") is FailureKind.RETRYABLE
        True
        >>> classify_failure("数字证书连接失败") is FailureKind.FATAL
        True
        >>> classify_failure("网关形态 HTTP 400") is FailureKind.GATEWAY_400
        True
        >>> classify_failure("指标不存在: spot") is FailureKind.FATAL
        True
    """
    text = str(reason or "")
    lowered = text.lower()
    if any(word in text for word in ("用户取消", "用户中止", "已取消")):
        return FailureKind.FATAL
    if "400" in text and any(word in text for word in ("HTTP", "网关", "瑞数")):
        return FailureKind.GATEWAY_400
    if "http 400" in lowered or "status 400" in lowered or "status=400" in lowered:
        return FailureKind.GATEWAY_400
    fatal_marks = (
        "证书", "U-Key", "UKey", "ukey", "PIN", "pin错误",
        "登录失败", "登录工作流", "未配置登录",
        "敏感参数未配置", "指标不存在", "配置错误", "配置不存在",
        "YAML", "存储结构", "存储路径",
    )
    if any(mark in text for mark in fatal_marks):
        return FailureKind.FATAL
    retry_marks = (
        "超时", "timeout", "Timeout",
        "等待元素", "未确认业务标签", "业务页面被关闭", "业务标签",
        "导航", "net::ERR_", "ERR_",
        "在途", "请求未确认", "导出前暂停",
        "下载",
        "回到工作台",
    )
    if any(mark in text for mark in retry_marks):
        return FailureKind.RETRYABLE
    return FailureKind.LOCAL


def slot_keys_for_item(item: dict) -> list[str]:
    """把一次计划条目展开成「指标 + 日期」键，供续跑跳过已成功的组合。

    Args:
        item (dict): ``plan_workflow_runs`` 的一条，含 ``indicator``、``mode``、``dates``。

    Returns:
        list[str]: ``指标\\t日期``。按月、按周会带前缀，避免和按日撞键。

    Examples:
        >>> from datetime import date
        >>> item = {"indicator": "spot", "mode": "replay", "dates": [date(2026, 11, 1)]}
        >>> slot_keys_for_item(item)
        ['spot\\t2026-11-01']
    """
    indicator = str(item.get("indicator") or "")
    mode = str(item.get("mode") or "")
    dates = list(item.get("dates") or [])

    def day_text(value: Any) -> str:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    if mode == "month":
        token = str(item.get("query_month") or "")
        if not token and dates:
            token = dates[-1].strftime("%Y-%m")
        return [f"{indicator}\tmonth:{token}"]
    if mode == "week":
        tokens = [day_text(day) for day in dates] or [""]
        return [f"{indicator}\tweek:{token}" for token in tokens]
    if mode == "repeat":
        tokens = [day_text(day) for day in dates]
        for values in (item.get("output_dates") or {}).values():
            for day in values:
                text = day_text(day)
                if text not in tokens:
                    tokens.append(text)
        return [f"{indicator}\t{token}" for token in (tokens or [""])]
    if dates:
        return [f"{indicator}\t{day_text(dates[0])}"]
    return [f"{indicator}\t"]


def public_plan(cfg: dict) -> dict:
    """去掉任务配置里的密钥，留下可以存进执行记录的续跑快照。

    Args:
        cfg (dict): 交给 TaskRunner 的任务配置。

    Returns:
        dict: 可 JSON 序列化、不含口令和 Token 的配置。

    Examples:
        >>> public_plan({"name": "日常", "cert_password": "secret", "indicators": ["a"]})
        {'name': '日常', 'indicators': ['a']}
    """
    kept: dict[str, Any] = {}
    for key, value in cfg.items():
        if callable(value):
            continue
        if str(key).startswith("_") and key != "_control_name":
            continue
        lowered = str(key).lower()
        if any(part in lowered for part in _SECRET_PARTS):
            continue
        kept[str(key)] = value
    return kept


def remember_run_plan(store: Any, exec_id: str, cfg: dict) -> None:
    """把这次任务的续跑快照写进执行记录。

    Args:
        store: 带 ``save_resume_plan`` 的状态库。空则忽略。
        exec_id (str): 本次执行 id。
        cfg (dict): 任务配置。

    Returns:
        None: 写入失败只记日志，不打断任务。

    Examples:
        >>> remember_run_plan(None, "", {}) is None
        True
    """
    if not exec_id or store is None or not hasattr(store, "save_resume_plan"):
        return
    try:
        store.save_resume_plan(exec_id, public_plan(cfg))
    except Exception as exc:
        from loguru import logger

        logger.warning("保存续跑计划失败: {}", exc)


def copy_resume_slots(store: Any, source_exec_id: str, dest_exec_id: str) -> None:
    """把上一次的指标结果抄到新执行上，这样中断后还能接着跳过已成功项。

    Args:
        store: 带 ``list_indicator_slots`` 和 ``mark_indicator_slot`` 的状态库。
        source_exec_id (str): 被续跑的执行 id。
        dest_exec_id (str): 这一次新的执行 id。

    Returns:
        None: 没有状态库或两次是同一条记录时不做任何事。

    Examples:
        >>> copy_resume_slots(None, "a", "b") is None
        True
    """
    if store is None or not source_exec_id or source_exec_id == dest_exec_id:
        return
    if not hasattr(store, "list_indicator_slots") or not hasattr(store, "mark_indicator_slot"):
        return
    for row in store.list_indicator_slots(source_exec_id):
        store.mark_indicator_slot(
            dest_exec_id,
            str(row.get("indicator") or ""),
            str(row.get("slot_key") or ""),
            str(row.get("status") or ""),
        )


def dumps_plan(plan: dict) -> str:
    """把续跑快照收成 JSON。

    Args:
        plan (dict): ``public_plan`` 的结果。

    Returns:
        str: UTF-8 JSON 文本。

    Examples:
        >>> json.loads(dumps_plan({"name": "日常"}))["name"]
        '日常'
    """
    return json.dumps(plan, ensure_ascii=False)
