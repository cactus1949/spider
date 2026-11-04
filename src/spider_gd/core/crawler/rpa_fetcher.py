"""RPA 兜底采集引擎（前端仿真，链路1 的 C 引擎之一）。

"接口优先、RPA 兜底"：当 :class:`ApiFetcher` 失败时，编排器回退到本模块，
用浏览器模拟"打开页面 → 点击导出 → 捕获下载"获取同一份数据。

与具体浏览器解耦：依赖 :class:`RpaPageOps`（由 ``PlaywrightRunner.download_via_click`` 满足），
便于注入假对象做单元测试。

页面地址与导出按钮选择器是平台相关的（阶段 2.5 按真实平台补全），
通过 ``endpoint_pages`` 映射（indicator → (page_url, selector)）注入，避免污染 EndpointConfig。
"""

from __future__ import annotations

import time
from typing import Protocol

from loguru import logger

from spider_gd.interfaces.crawler import CrawlResult, CrawlTask, EndpointConfig


class RpaPageOps(Protocol):
    """RPA 页面操作原语（由 playwright_runner 实现）。"""

    async def download_via_click(self, page_url: str, export_selector: str) -> bytes: ...


class RpaFetcher:
    def __init__(
        self,
        page_ops: RpaPageOps,
        endpoint_pages: dict[str, tuple[str, str]],
    ) -> None:
        """``endpoint_pages``: indicator 名 → (页面 URL, 导出按钮 selector)。"""
        self._page = page_ops
        self._pages = endpoint_pages

    async def fetch(self, endpoint: EndpointConfig, task: CrawlTask) -> CrawlResult:
        started = time.monotonic()
        mapping = self._pages.get(endpoint.name)
        if mapping is None:
            msg = f"RPA 无页面映射：{endpoint.name}"
            logger.warning(msg)
            return CrawlResult(task_id=task.task_id, success=False, error_msg=msg)

        page_url, selector = mapping
        try:
            data = await self._page.download_via_click(page_url, selector)
        except Exception as exc:
            duration = int((time.monotonic() - started) * 1000)
            logger.warning("RPA 兜底采集失败 {}：{}", endpoint.name, exc)
            return CrawlResult(
                task_id=task.task_id, success=False, error_msg=f"RPA 失败：{exc}",
                duration_ms=duration,
            )

        duration = int((time.monotonic() - started) * 1000)
        logger.success("RPA 兜底采集成功 {}（{} bytes）", endpoint.name, len(data))
        return CrawlResult(
            task_id=task.task_id, success=True, data=data, duration_ms=duration
        )
