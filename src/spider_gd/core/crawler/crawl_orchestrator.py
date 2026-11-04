"""采集任务编排器（决策一：状态机，非线性管道）。

把已实现的原子组件串成一次端到端采集，以 ``task_items`` 表为状态机核心：
LoginFlow → ApiFetcher（经 SessionKeeper 处理 412）→ DataProcessor → LocalLanding → 状态库。

设计要点：
- **item 粒度状态机**：一个任务含多个 endpoint，每个 endpoint 对应一条
  ``(task_id, indicator, target_date)`` item，独立 pending→success/failed 流转。
- **断点续采**：传入已存在的 ``task_id`` 时，只取 ``get_pending_items()`` 续采，
  不重采已成功项；崩溃重启后以同 task_id 重跑即可。
- **部分失败**：逐 item ``update_item_status``；任务级状态由 ``get_task_summary()``
  聚合（全成 completed / 部分 partial / 全败 failed）。
- **并发安全**：``TaskLock`` 文件锁，同名任务同时仅一个实例。
- **RPA 兜底**：API fetch 失败时回退到注入的 ``rpa_fetcher``（阶段 2 接入，可为 None）。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from loguru import logger

from spider_gd.core.crawler.exceptions import (
    AuthFailedError,
    CaptchaError,
    CrawlerError,
    SiteUnreachableError,
)
from spider_gd.core.crawler.session_keeper import ReloginExhaustedError, SessionKeeper
from spider_gd.core.crawler.task_lock import TaskLock
from spider_gd.interfaces.crawler import CrawlResult, CrawlTask, EndpointConfig, IApiFetcher
from spider_gd.interfaces.storage import (
    ILocalLanding,
    ITaskStateStore,
    TaskItemRecord,
    TaskRecord,
)

# 数据处理器签名：原始 bytes → 落地 bytes（阶段 1 为恒等；阶段 2 接入 DataProcessor）
Processor = Callable[[bytes, str], bytes]
# RPA 兜底签名：endpoint + 任务 → CrawlResult（阶段 2 接入）
RpaFetcher = Callable[[EndpointConfig, CrawlTask], Awaitable[CrawlResult]]
# 任务产物发送签名：task_id + task_dir + zip_path + summary
DeliveryHook = Callable[[str, str, str, dict[str, object]], Awaitable[None]]


def _to_api_date(target_date: str) -> str:
    """item 用 YYYY-MM-DD；API 端点按 endpoint.date_format 自行转换。"""
    return target_date  # 保持原样，由 api_fetcher 按端点配置的 date_format 转换


def _as_int(value: object) -> int:
    """把 summary 里的 object 值安全转 int。"""
    return value if isinstance(value, int) else 0


class CrawlOrchestrator:
    def __init__(
        self,
        *,
        session_keeper: SessionKeeper,
        api_fetcher: IApiFetcher,
        state_store: ITaskStateStore,
        landing: ILocalLanding,
        processor: Processor | None = None,
        rpa_fetcher: RpaFetcher | None = None,
        lock_dir: str | Path = "./data/.locks",
        alert_hook: Callable[[str, str], Awaitable[None]] | None = None,
        delivery_hook: DeliveryHook | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._keeper = session_keeper
        self._fetcher = api_fetcher
        self._store = state_store
        self._landing = landing
        self._processor = processor or (lambda data, _indicator: data)
        self._rpa = rpa_fetcher
        self._lock_dir = lock_dir
        self._alert_hook = alert_hook
        self._delivery_hook = delivery_hook
        self._cancel_requested = cancel_requested or (lambda: False)

    async def run(
        self,
        *,
        task_name: str,
        endpoints: list[EndpointConfig],
        target_date: str,
        task_id: str | None = None,
    ) -> dict[str, object]:
        """执行一次采集任务。

        - ``task_id`` 为空：新建任务，逐 endpoint 建 pending item。
        - ``task_id`` 已存在 item：断点续采，仅处理 pending/failed item。
        """
        with TaskLock(self._lock_dir, task_name):
            return await self._run_locked(
                task_name=task_name,
                endpoints=endpoints,
                target_date=target_date,
                task_id=task_id,
            )

    async def _run_locked(
        self,
        *,
        task_name: str,
        endpoints: list[EndpointConfig],
        target_date: str,
        task_id: str | None,
    ) -> dict[str, object]:
        ep_by_indicator = {ep.name: ep for ep in endpoints}

        resume = bool(task_id) and bool(self._store.get_items(task_id))  # type: ignore[arg-type]
        if resume:
            assert task_id is not None
            task_dir = self._resolve_task_dir(task_id)
            self._store.update_task_status(task_id, "running")
            pending = self._store.get_pending_items(task_id)
            logger.info("断点续采 task={} 待处理 {} 项", task_id, len(pending))
        else:
            exec_time = datetime.now().strftime("%Y%m%d_%H%M%S")
            task_id = task_id or f"T{exec_time}"
            task_dir = self._landing.create_task_dir(task_name, exec_time)
            self._store.create_task(
                TaskRecord(task_id, task_name, "running", task_dir, datetime.now().isoformat())
            )
            for ep in endpoints:
                self._store.create_item(TaskItemRecord(task_id, ep.name, target_date, "pending"))
            pending = self._store.get_pending_items(task_id)
            logger.info("新建采集任务 task={} 含 {} 个 endpoint", task_id, len(pending))

        for i, item in enumerate(pending):
            # 模拟人工操作间隔：每个 item 之间随机延迟 2-8 秒（首个 item 无延迟）
            if i > 0:
                delay = random.uniform(2.0, 8.0)
                logger.debug("item 间延迟 {:.1f}s", delay)
                await asyncio.sleep(delay)

            if self._cancel_requested():
                self._store.update_task_status(task_id, "cancelled")
                return {"task_id": task_id, "status": "cancelled", "reason": "用户取消"}

            endpoint = ep_by_indicator.get(item.indicator_name)
            if endpoint is None:
                logger.warning("跳过未知 endpoint 配置：{}", item.indicator_name)
                self._store.update_item_status(
                    task_id, item.indicator_name, item.target_date, "skipped",
                    error_msg="无匹配 endpoint 配置",
                )
                continue
            await self._process_item(task_id, task_dir, endpoint, item.target_date)

        return await self._finalize(task_id, task_dir)

    async def _process_item(
        self, task_id: str, task_dir: str, endpoint: EndpointConfig, target_date: str
    ) -> None:
        indicator = endpoint.name
        task = CrawlTask(task_id=task_id, date=_to_api_date(target_date), data_type=indicator)

        try:
            result = await self._keeper.fetch(self._fetcher, endpoint, task)

            if not result.success or result.data is None:
                result = await self._try_rpa(endpoint, task) or result

            if result.success and result.data is not None:
                data = self._processor(result.data, indicator)
                file_path = self._landing.save_data_file(task_dir, indicator, target_date, data)
                self._store.update_item_status(
                    task_id, indicator, target_date, "success",
                    file_path=file_path, file_size=len(data),
                )
                logger.success("item 成功 {} {} -> {}", indicator, target_date, file_path)
            else:
                await self._fail(task_id, indicator, target_date, result.error_msg or "采集失败")
        except ReloginExhaustedError as exc:
            await self._fail(task_id, indicator, target_date, f"会话续期失败：{exc}", alert=True)
        except AuthFailedError as exc:
            await self._fail(task_id, indicator, target_date, f"认证失败：{exc}", alert=True)
        except SiteUnreachableError as exc:
            await self._fail(task_id, indicator, target_date, f"站点不可达：{exc}", alert=True)
        except CaptchaError as exc:
            await self._fail(task_id, indicator, target_date, f"登录/验证码失败：{exc}", alert=True)
        except CrawlerError as exc:
            await self._fail(task_id, indicator, target_date, f"采集异常：{exc}")
        except Exception as exc:
            logger.error(
                "item 处理时未预期异常 {} {}：{}: {}",
                indicator, target_date, type(exc).__name__, exc,
            )
            await self._fail(task_id, indicator, target_date, f"未预期异常：{exc}")

    async def _try_rpa(self, endpoint: EndpointConfig, task: CrawlTask) -> CrawlResult | None:
        if self._rpa is None:
            return None
        logger.info("API 失败，回退 RPA 兜底采集：{}", endpoint.name)
        try:
            return await self._rpa(endpoint, task)
        except Exception as exc:  # RPA 兜底失败不应中断整个任务
            logger.warning("RPA 兜底失败：{}", exc)
            return None

    async def _fail(
        self, task_id: str, indicator: str, target_date: str, msg: str, *, alert: bool = False
    ) -> None:
        self._store.update_item_status(task_id, indicator, target_date, "failed", error_msg=msg)
        logger.warning("item 失败 {} {}：{}", indicator, target_date, msg)
        if alert and self._alert_hook is not None:
            await self._alert_hook(f"采集失败：{indicator}", msg)

    async def _finalize(self, task_id: str, task_dir: str) -> dict[str, object]:
        items = self._store.get_items(task_id)
        ledger = self._landing.generate_ledger(task_dir, items)

        summary = self._store.get_task_summary(task_id)
        total = _as_int(summary.get("total"))
        success = _as_int(summary.get("success"))
        if success == total and total > 0:
            status = "completed"
        elif success == 0:
            status = "failed"
        else:
            status = "partial"
        self._store.update_task_status(task_id, status)
        logger.info("任务收尾 {} 状态={} 成功 {}/{}", task_id, status, success, total)

        # 只要本次有成功产物就打包发送；partial 状态保留在 summary 里供收件人判断。
        zip_path: str | None = None
        if success > 0:
            zip_path = self._landing.compress_task(task_dir)
            if zip_path is not None and self._delivery_hook is not None:
                try:
                    await self._delivery_hook(task_id, task_dir, zip_path, summary)
                except Exception as exc:
                    logger.warning(
                        "任务产物发送失败但不影响采集结果：task={} error={}",
                        task_id,
                        exc,
                    )
                    if self._alert_hook is not None:
                        try:
                            await self._alert_hook("采集任务产物发送失败", f"{task_id}: {exc}")
                        except Exception as alert_exc:
                            logger.warning("产物发送失败告警也发送失败：{}", alert_exc)
        else:
            logger.info("任务没有成功产物，跳过压缩：task={} status={}", task_id, status)

        # 告警闭环：单 item 的终止失败已在 _fail 即时告警；此处对任务整体收尾再升级一次。
        if status != "completed" and self._alert_hook is not None:
            failed = _as_int(summary.get("failed"))
            await self._alert_hook(
                f"采集任务{status}：{task_id}",
                f"成功 {success}/{total}，失败 {failed}",
            )

        return {
            "task_id": task_id,
            "task_dir": task_dir,
            "ledger": ledger,
            "zip": zip_path,
            "status": status,
            "summary": summary,
        }

    def _resolve_task_dir(self, task_id: str) -> str:
        """续采时复用原任务目录（取自 tasks.output_dir）。"""
        task = self._store.get_task(task_id)
        if task is not None and task.output_dir:
            Path(task.output_dir).mkdir(parents=True, exist_ok=True)
            return task.output_dir
        # 极端情况（记录缺失）：回退新建目录
        exec_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self._landing.create_task_dir(task_id, exec_time)
