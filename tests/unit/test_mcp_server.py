"""测试 MCP server 只读 tool（注入假 ControlService，不依赖网络/浏览器）。"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("mcp")

from spider_gd.gateway.mcp.server import create_mcp_server


class _FakeService:
    """模拟 ControlService，仅实现 MCP tool 依赖的方法。"""

    def __init__(
        self,
        *,
        tasks: list[dict[str, Any]] | None = None,
        task_detail: dict[str, Any] | None = None,
        health_status: dict[str, Any] | None = None,
        raise_on_get: bool = False,
    ) -> None:
        self._tasks = tasks or []
        self._task_detail = task_detail or {}
        self._health = health_status or {"overall": "healthy", "checks": []}
        self._raise_on_get = raise_on_get

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._tasks[:limit]

    def get_task(self, task_id: str) -> dict[str, Any]:
        if self._raise_on_get:
            raise KeyError(task_id)
        return self._task_detail

    async def health(self) -> dict[str, Any]:
        return self._health


async def _call_tool(mcp: Any, name: str, **arguments: Any) -> Any:
    """通过 FastMCP 公开 API call_tool 调用 tool，返回结构化结果。"""
    result = await mcp.call_tool(name, arguments)
    # call_tool 返回 (content_list, structured_data) 元组，取结构化数据
    return result[1]


@pytest.mark.unit
async def test_query_tasks_returns_dict() -> None:
    svc = _FakeService(
        tasks=[
            {
                "task_id": "T001",
                "name": "日常采集",
                "status": "completed",
                "created_at": "2026-06-24T09:00:00",
                "finished_at": "2026-06-24T09:05:00",
            },
            {
                "task_id": "T002",
                "name": "补漏_日常采集",
                "status": "running",
                "created_at": "2026-06-24T15:00:00",
                "finished_at": None,
            },
        ]
    )
    mcp = create_mcp_server(svc)
    data = await _call_tool(mcp, "query_tasks", limit=10)
    assert data["count"] == 2
    assert data["tasks"][0]["task_id"] == "T001"
    assert data["tasks"][1]["status"] == "running"


@pytest.mark.unit
async def test_get_task_returns_detail() -> None:
    detail = {
        "summary": {"task_id": "T001", "total": 5, "success": 3, "failed": 1, "pending": 1},
        "items": [{"indicator": "日前统一结算价格", "status": "success"}],
    }
    svc = _FakeService(task_detail=detail)
    mcp = create_mcp_server(svc)
    data = await _call_tool(mcp, "get_task", task_id="T001")
    assert data["summary"]["task_id"] == "T001"
    assert data["summary"]["success"] == 3
    assert len(data["items"]) == 1


@pytest.mark.unit
async def test_get_task_not_found() -> None:
    svc = _FakeService(raise_on_get=True)
    mcp = create_mcp_server(svc)
    data = await _call_tool(mcp, "get_task", task_id="NOPE")
    assert data["error"] == "任务不存在"
    assert data["task_id"] == "NOPE"


@pytest.mark.unit
async def test_health_check_returns_status() -> None:
    svc = _FakeService(
        health_status={
            "overall": "degraded",
            "checks": [{"module": "disk", "message": "空间不足"}],
        }
    )
    mcp = create_mcp_server(svc)
    data = await _call_tool(mcp, "health_check")
    assert data["overall"] == "degraded"
    assert data["checks"][0]["module"] == "disk"


@pytest.mark.unit
async def test_mcp_server_has_tools() -> None:
    """验证 MCP server 注册了 3 个只读 tool。"""
    svc = _FakeService()
    mcp = create_mcp_server(svc)
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == {"query_tasks", "get_task", "health_check"}
