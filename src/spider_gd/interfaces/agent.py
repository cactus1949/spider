"""远程 Agent 接口契约（链路5，可拆卸）—— 双向控制。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class AgentCommand:
    raw_text: str
    action: str
    params: dict[str, object] = field(default_factory=dict)
    permission_level: str = "readonly"  # readonly / config / system


@dataclass
class AgentResponse:
    success: bool
    message: str
    attachments: list[str] = field(default_factory=list)


class IAgentAdapter(Protocol):
    async def handle_command(self, command: AgentCommand) -> AgentResponse: ...

    async def send_response(self, response: AgentResponse) -> bool: ...
