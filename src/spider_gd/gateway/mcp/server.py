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


def create_mcp_server(
    service: ControlService,
    *,
    include_workbench_tools: bool = False,
) -> FastMCP:
    """创建 FastMCP 实例并注册查询 tool（可选注册工作台唤醒 tool）。

    Args:
        service: 统一控制服务，提供任务查询与健康检查能力。
        include_workbench_tools: 为 True 时额外注册工作台状态查询与按 SSID 动态唤醒 tool。

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

    if include_workbench_tools:
        _register_workbench_tools(mcp, service)

    logger.info("MCP server 已创建，注册只读 tool：query_tasks、get_task、health_check")
    return mcp


def _register_workbench_tools(mcp: Any, service: Any) -> None:
    """注册 WifiRack 移动工作台 MCP Tool（供 Agent/OpenClaw 或内部统一调用）。"""

    def _get_client():
        from spider_gd.app import build_workbench_client

        ctx = getattr(service, "_ctx", None)
        if ctx is None:
            raise RuntimeError("当前服务上下文不支持工作台调用")
        return build_workbench_client(ctx, require_enabled=False)

    @mcp.tool()
    async def workbench_status() -> dict[str, Any]:
        """查询 WifiRack 移动工作台 6 槽实时状态、电量与动态识别出的 WiFi SSID。"""
        from spider_gd.infrastructure.hardware_lease import run_hardware_blocking

        try:
            client = _get_client()
            data = await run_hardware_blocking(client.get_status)
            return {"ok": True, "enabled": client.enabled, "data": data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool()
    async def workbench_wake_device(
        ssid: str = "",
        station_name: str = "",
        wait: bool = True,
    ) -> dict[str, Any]:
        """按随身 WiFi 的 SSID 或场站名称动态唤醒设备（无需指定槽位号）。

        Args:
            ssid: 目标随身 WiFi 名称，例如 "HUAWEI-YJ01"。
            station_name: 场站名称（可选，未传 ssid 时从场站配置自动查出对应 SSID）。
            wait: 是否等待唤醒上线确认，默认 True。
        """
        from spider_gd.core.station_profile import find_station, parse_station_profiles
        from spider_gd.infrastructure.hardware_lease import (
            hardware_operation,
            run_hardware_blocking,
        )

        target_ssid = ssid.strip()
        target_name = station_name.strip()
        wifi_pass = ""
        ctx = getattr(service, "_ctx", None)
        if ctx is not None and (target_name or not target_ssid):
            try:
                profiles = parse_station_profiles(
                    ctx.state_store.get_setting("stationProfiles") or ""
                )
                matched = find_station(profiles, target_name or target_ssid)
                if matched is not None:
                    target_ssid = target_ssid or matched.wifi_ssid
                    target_name = target_name or matched.name
                    wifi_pass = matched.wifi_password
            except Exception:
                pass
        try:
            client = _get_client()
            with hardware_operation():
                res = await run_hardware_blocking(
                    client.wake_by_ssid,
                    target_ssid,
                    name=target_name,
                    wifi_password=wifi_pass,
                    wait=wait,
                )
            return {
                "ok": res.ok,
                "status": res.status,
                "ssid": res.ssid,
                "slot_id": res.slot_id,
                "rssi": res.rssi,
                "job_id": res.job_id,
                "message": res.message,
                "transport": res.transport,
            }
        except Exception as exc:
            return {"ok": False, "status": "failed", "message": str(exc)}

    @mcp.tool()
    async def workbench_discover_slots() -> dict[str, Any]:
        """触发工作台全槽动态认槽，自动识别每个物理槽位插入的是哪台随身 WiFi。"""
        from spider_gd.infrastructure.hardware_lease import (
            hardware_operation,
            run_hardware_blocking,
        )

        try:
            client = _get_client()
            with hardware_operation():
                data = await run_hardware_blocking(client.discover_slots, wait=False)
            return {"ok": True, "data": data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def create_workbench_mcp_server(service: ControlService) -> FastMCP:
    """创建包含 WifiRack 移动工作台控制 Tool 的 MCP Server。"""
    return create_mcp_server(service, include_workbench_tools=True)
