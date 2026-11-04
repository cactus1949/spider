"""远程控制 Agent（链路5，可拆卸）。

当前阶段仅放开 readonly 指令；config / system 默认锁死。
配置禁用（modules.agent=false）时不加载，不影响主流程。
"""

from __future__ import annotations

from spider_gd.gateway.agent.agent_adapter import AgentAdapter
from spider_gd.gateway.agent.command_executor import CommandExecutor, parse_command
from spider_gd.gateway.agent.permission_guard import PermissionGuard

__all__ = ["AgentAdapter", "CommandExecutor", "PermissionGuard", "parse_command"]
