"""Agent 适配层：接收指令 → 解析执行 → 返回结果（双向）。

实现 :class:`spider_gd.interfaces.agent.IAgentAdapter`。
回传通道通过 ``responder`` 注入（钉钉 Stream / OpenClaw / Webhook 均可），
未注入时退化为仅记录日志。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loguru import logger

from spider_gd.gateway.agent.command_executor import CommandExecutor, parse_command
from spider_gd.interfaces.agent import AgentCommand, AgentResponse

Responder = Callable[[AgentResponse], Awaitable[bool]]


class AgentAdapter:
    def __init__(self, executor: CommandExecutor, *, responder: Responder | None = None) -> None:
        self._executor = executor
        self._responder = responder

    async def handle_command(self, command: AgentCommand) -> AgentResponse:
        return await self._executor.execute(command)

    async def handle_text(self, raw_text: str) -> AgentResponse:
        """便捷入口：直接处理自然语言文本。"""
        return await self.handle_command(parse_command(raw_text))

    async def handle_and_reply(self, raw_text: str) -> AgentResponse:
        """完整双向回路：解析执行后通过 responder 回传结果。"""
        response = await self.handle_text(raw_text)
        await self.send_response(response)
        return response

    async def send_response(self, response: AgentResponse) -> bool:
        if self._responder is not None:
            try:
                return await self._responder(response)
            except Exception as exc:
                logger.warning("Agent 回传失败：{}", exc)
                return False
        logger.info("Agent 响应（无回传通道）：success={} {}", response.success, response.message)
        return True
