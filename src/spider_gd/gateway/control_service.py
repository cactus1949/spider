"""统一控制服务：采集/健康/任务查询的单一业务入口。

REST API 与 Agent handler 共用本服务，避免逻辑重复（DRY）。
采集触发支持注入 ``crawl_runner`` 以便测试不启动真实浏览器。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from spider_gd.interfaces.alert import AlertLevel

if TYPE_CHECKING:
    from spider_gd.app import AppContext

CrawlRunner = Callable[[str, str], Awaitable[dict[str, Any]]]


def _yesterday() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


class ControlService:
    def __init__(self, ctx: AppContext, *, crawl_runner: CrawlRunner | None = None) -> None:
        self._ctx = ctx
        self._crawl_runner = crawl_runner

    async def start_crawl(
        self, *, target_date: str | None = None, task_name: str | None = None,
        indicators: list[str] | None = None, data_dir: str | None = None,
    ) -> dict[str, Any]:
        if not self._ctx.settings.modules.crawler:
            return {"status": "rejected", "error": "采集模块未启用（modules.crawler=false）"}
        target_date = target_date or _yesterday()
        task_name = task_name or self._ctx.settings.schedule.task_name
        # 按指标过滤端点
        endpoints = self._ctx.endpoints
        if indicators:
            endpoints = [ep for ep in endpoints if ep.name in indicators]
            logger.info("控制服务：触发采集 task={} date={} indicators={} → 匹配 {} 个端点",
                        task_name, target_date, indicators, len(endpoints))
        else:
            logger.info("控制服务：触发采集 task={} date={}（全端点）", task_name, target_date)
        if not endpoints:
            return {"status": "rejected", "error": "没有匹配的端点配置"}
        if self._crawl_runner is not None:
            return await self._crawl_runner(target_date, task_name)

        # 默认路径：构建真实编排器跑一次（需浏览器环境）
        from spider_gd.app import build_orchestrator
        from spider_gd.core.pipeline.local_landing import LocalLanding

        # 如果前端指定了下载目录，使用自定义路径
        landing: LocalLanding | None = None
        if data_dir:
            from pathlib import Path as _P
            today = datetime.now()
            dated = str(_P(data_dir) / today.strftime("%Y-%m") / today.strftime("%Y-%m-%d"))
            _P(dated).mkdir(parents=True, exist_ok=True)
            landing = LocalLanding(dated)
            logger.info("使用自定义下载目录：{}", dated)

        orchestrator, runner = build_orchestrator(self._ctx, landing=landing)
        try:
            return await orchestrator.run(  # type: ignore[attr-defined]
                task_name=task_name, endpoints=endpoints, target_date=target_date
            )
        finally:
            await runner.close()  # type: ignore[attr-defined]

    async def health(self) -> dict[str, Any]:
        from spider_gd.infrastructure.monitor.health_checker import (
            HealthChecker,
            disk_space_check,
            network_check,
            ukey_check,
        )
        from spider_gd.infrastructure.monitor.status_reporter import build_status

        checker = HealthChecker()
        checker.add_check("disk", disk_space_check(self._ctx.settings.storage.data_dir))
        checker.add_check("ukey", ukey_check(self._ctx.settings.ukey.vid_pid))
        # 专网站点：优先用设置中的 siteUrl，回退 platform.domain
        site_url = self._ctx.state_store.get_setting("siteUrl") or ""
        if site_url:
            target = site_url if site_url.startswith("http") else f"https://{site_url}"
        else:
            target = f"https://{self._ctx.settings.platform.domain}"

        checker.add_check("network", network_check(
            target, module_name="network",
        ))
        checker.add_check("wan", network_check(
            "https://www.baidu.com", timeout=3.0, module_name="wan",
        ))
        checks = await checker.check_all()
        overall = checker.overall_from(checks)
        return build_status(overall, checks)

    async def test_alert(self) -> dict[str, Any]:
        """发送一条测试告警，复用当前运行时告警网关。"""
        result = await self._ctx.alert.send_alert(
            AlertLevel.INFO,
            "这是一条测试告警，来自 spider-gd 采集系统",
            event_type="test",
        )
        return {
            "sent": result.sent,
            "suppressed": result.suppressed,
            "error": result.error_msg,
        }

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        tasks = self._ctx.state_store.list_tasks(limit)
        return [
            {
                "id": t.task_id,
                "name": t.name,
                "status": t.status,
                "created_at": t.created_at,
                "finished_at": t.finished_at,
                "output_dir": t.output_dir,
            }
            for t in tasks
        ]

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self._ctx.state_store.get_task(task_id)
        if task is None:
            return {}
        items = self._ctx.state_store.get_items(task_id)
        return {
            "id": task.task_id,
            "name": task.name,
            "status": task.status,
            "created_at": task.created_at,
            "finished_at": task.finished_at,
            "output_dir": task.output_dir,
            "items": [
                {
                    "indicator": i.indicator_name,
                    "date": i.target_date,
                    "status": i.status,
                    "error": i.error_msg,
                }
                for i in items
            ],
        }
