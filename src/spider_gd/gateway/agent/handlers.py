"""Agent 指令的真实 handler 实现（复用 ControlService）。

把 :func:`parse_command` 解析出的 action 映射到实际业务：
- ``trigger_crawl`` → 触发采集
- ``health_check``  → 健康检查
- ``query_result``  → 最近任务/台账查询

每个 handler 返回字符串（供 AgentResponse.message 回传）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spider_gd.gateway.control_service import ControlService

Handler = Callable[[dict[str, object]], Awaitable[str]]


def build_default_handlers(service: ControlService) -> dict[str, Handler]:
    async def _trigger_crawl(params: dict[str, object]) -> str:
        target_date = params.get("target_date")
        result = await service.start_crawl(
            target_date=str(target_date) if target_date else None
        )
        return (
            f"采集完成：task={result.get('task_id')} 状态={result.get('status')} "
            f"摘要={result.get('summary')}"
        )

    async def _health_check(_params: dict[str, object]) -> str:
        status = await service.health()
        return f"系统健康：{status.get('overall')}（{len(status.get('checks', []))} 项检查）"

    async def _query_result(params: dict[str, object]) -> str:
        task_id = params.get("task_id")
        if task_id:
            detail = service.get_task(str(task_id))
            return f"任务 {task_id}：{detail.get('summary')}"
        tasks = service.list_tasks(limit=5)
        if not tasks:
            return "暂无采集任务记录"
        lines = [f"- {t['name']} {t['status']} ({t['created_at']})" for t in tasks]
        return "最近任务：\n" + "\n".join(lines)

    return {
        "trigger_crawl": _trigger_crawl,
        "health_check": _health_check,
        "query_result": _query_result,
    }
