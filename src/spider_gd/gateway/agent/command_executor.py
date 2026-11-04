"""指令解析与执行：自然语言 → 操作，权限校验后调用 readonly 处理器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loguru import logger

from spider_gd.gateway.agent.permission_guard import PermissionGuard
from spider_gd.interfaces.agent import AgentCommand, AgentResponse

# 关键词 → (action, 权限级别)。顺序敏感：先匹配高危。
_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("升级", "git pull", "重启", "卸载"), "system_op", "system"),
    (("改", "配置", "修改时间", "调整"), "update_config", "config"),
    (("立即爬取", "触发", "采集"), "trigger_crawl", "config"),
    (("状态", "健康", "检查"), "health_check", "readonly"),
    (("最近", "结果", "台账", "查询", "历史"), "query_result", "readonly"),
]

Handler = Callable[[dict[str, object]], Awaitable[str]]


def parse_command(raw_text: str) -> AgentCommand:
    """把自然语言指令解析为结构化 :class:`AgentCommand`。"""
    for keywords, action, level in _RULES:
        if any(kw in raw_text for kw in keywords):
            return AgentCommand(raw_text=raw_text, action=action, permission_level=level)
    return AgentCommand(raw_text=raw_text, action="unknown", permission_level="readonly")


class CommandExecutor:
    def __init__(
        self,
        handlers: dict[str, Handler] | None = None,
        guard: PermissionGuard | None = None,
    ) -> None:
        self._handlers = handlers or {}
        self._guard = guard or PermissionGuard("readonly")

    async def execute(self, command: AgentCommand) -> AgentResponse:
        logger.info("Agent 指令：{} → action={}", command.raw_text, command.action)

        if not self._guard.check(command.permission_level):
            return AgentResponse(
                success=False,
                message=f"权限不足：操作 {command.action} 需要 {command.permission_level} 级权限",
            )

        handler = self._handlers.get(command.action)
        if handler is None:
            return AgentResponse(success=False, message=f"暂不支持的操作：{command.action}")

        try:
            result = await handler(command.params)
        except Exception as exc:
            logger.exception("Agent 指令执行失败：{}", exc)
            return AgentResponse(success=False, message=f"执行失败：{exc}")
        return AgentResponse(success=True, message=result)
