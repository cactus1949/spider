"""测试 Agent 适配层（文本入口、响应回传、可拆卸）。"""

from __future__ import annotations

import pytest

from spider_gd.gateway.agent.agent_adapter import AgentAdapter
from spider_gd.gateway.agent.command_executor import CommandExecutor
from spider_gd.gateway.agent.permission_guard import PermissionGuard
from spider_gd.interfaces.agent import AgentResponse


@pytest.mark.unit
async def test_handle_text_routes_readonly_query():
    async def health(_params):
        return "全部正常"

    adapter = AgentAdapter(CommandExecutor(handlers={"health_check": health}))
    resp = await adapter.handle_text("检查系统状态")
    assert resp.success is True
    assert resp.message == "全部正常"


@pytest.mark.unit
async def test_handle_text_denies_config_at_readonly():
    adapter = AgentAdapter(CommandExecutor(guard=PermissionGuard("readonly")))
    resp = await adapter.handle_text("把爬取时间改为9点")
    assert resp.success is False
    assert "权限不足" in resp.message


@pytest.mark.unit
async def test_send_response_returns_true():
    adapter = AgentAdapter(CommandExecutor())
    assert await adapter.send_response(AgentResponse(success=True, message="ok")) is True
