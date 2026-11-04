"""测试 Agent 真实 handler（注入假 ControlService）。"""

from __future__ import annotations

import pytest

from spider_gd.gateway.agent.agent_adapter import AgentAdapter
from spider_gd.gateway.agent.command_executor import CommandExecutor
from spider_gd.gateway.agent.handlers import build_default_handlers
from spider_gd.gateway.agent.permission_guard import PermissionGuard
from spider_gd.interfaces.agent import AgentResponse


class _FakeService:
    def __init__(self) -> None:
        self.started = False

    async def start_crawl(self, *, target_date=None, task_name=None):
        self.started = True
        return {"task_id": "T9", "status": "completed", "summary": {"success": 2}}

    async def health(self):
        return {"overall": "healthy", "checks": [{"module": "disk"}]}

    def list_tasks(self, limit=5):
        return [{"name": "日常采集", "status": "completed", "created_at": "2026-06-22"}]

    def get_task(self, task_id):
        return {"summary": {"success": 1}}


@pytest.mark.unit
async def test_trigger_crawl_handler_invokes_service():
    svc = _FakeService()
    handlers = build_default_handlers(svc)
    executor = CommandExecutor(handlers=handlers, guard=PermissionGuard("config"))
    adapter = AgentAdapter(executor)
    resp = await adapter.handle_text("立即采集")
    assert resp.success is True
    assert svc.started is True
    assert "T9" in resp.message


@pytest.mark.unit
async def test_health_handler_readonly():
    svc = _FakeService()
    executor = CommandExecutor(
        handlers=build_default_handlers(svc), guard=PermissionGuard("readonly")
    )
    adapter = AgentAdapter(executor)
    resp = await adapter.handle_text("查询状态")
    assert resp.success is True
    assert "healthy" in resp.message


@pytest.mark.unit
async def test_trigger_crawl_blocked_by_readonly_permission():
    svc = _FakeService()
    executor = CommandExecutor(
        handlers=build_default_handlers(svc), guard=PermissionGuard("readonly")
    )
    adapter = AgentAdapter(executor)
    resp = await adapter.handle_text("立即采集")
    assert resp.success is False
    assert svc.started is False


@pytest.mark.unit
async def test_responder_invoked_on_reply():
    svc = _FakeService()
    sent: list[AgentResponse] = []

    async def _responder(r: AgentResponse) -> bool:
        sent.append(r)
        return True

    executor = CommandExecutor(
        handlers=build_default_handlers(svc), guard=PermissionGuard("readonly")
    )
    adapter = AgentAdapter(executor, responder=_responder)
    await adapter.handle_and_reply("查询历史")
    assert len(sent) == 1
    assert sent[0].success is True
