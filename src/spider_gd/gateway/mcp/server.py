"""MCP server 工厂：基于 FastMCP 暴露只读查询 tool。

通过 streamable-http 传输协议供 OpenClaw 等 MCP 客户端调用采集系统的查询能力。
所有 tool 均为只读，不触发任何采集/写入操作，可安全暴露给外部 Agent。

集成方式：调用方先 ``mcp = create_mcp_server(service)``，再
``mcp_app = mcp.streamable_http_app()`` 获取 ASGI 子应用并 ``app.mount("/mcp", mcp_app)``。

.. note::
    ``streamable_http_app()`` 返回的 Starlette 子应用自带 lifespan
    （``lifespan=lambda app: self.session_manager.run()``），但 Starlette
    的 ``Mount`` 不会向子应用传播 lifespan 事件。因此挂载方**必须**在
    父应用 lifespan 中显式进入 ``mcp.session_manager.run()`` 上下文，
    否则 session manager 的 task group 不会初始化，请求时会抛
    ``RuntimeError: Task group is not initialized``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from spider_gd.gateway.control_service import ControlService


def create_mcp_server(service: ControlService) -> FastMCP:
    """创建 FastMCP 实例并注册只读查询 tool。

    Args:
        service: 统一控制服务，提供任务查询与健康检查能力。

    Returns:
        配置好的 :class:`FastMCP` 实例。调用方需自行调用
        ``streamable_http_app()`` 获取 ASGI 子应用，并在父应用 lifespan 中
        进入 ``mcp.session_manager.run()`` 上下文以初始化 task group。
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "spider-gd",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
    )

    @mcp.tool()
    def query_tasks(limit: int = 20) -> dict[str, Any]:
        """查询最近的采集任务列表。

        Args:
            limit: 返回任务数量上限，默认 20。
        """
        tasks = service.list_tasks(limit=limit)
        return {"tasks": tasks, "count": len(tasks)}

    @mcp.tool()
    def get_task(task_id: str) -> dict[str, Any]:
        """查询单个采集任务的详细信息，包括各指标项的采集状态。

        Args:
            task_id: 任务 ID，例如 "T20260624_090000"。
        """
        try:
            result = service.get_task(task_id)
        except KeyError:
            return {"error": "任务不存在", "task_id": task_id}
        except Exception:
            logger.exception("查询任务详情失败 task={}", task_id)
            return {"error": "查询失败", "task_id": task_id}
        if not result:
            return {"error": "任务不存在", "task_id": task_id}
        return result

    @mcp.tool()
    async def health_check() -> dict[str, Any]:
        """检查采集系统各模块的健康状态。"""
        return await service.health()

    logger.info("MCP server 已创建，注册只读 tool：query_tasks、get_task、health_check")
    return mcp
