"""任务调度器：APScheduler(AsyncIOScheduler) 封装。

实现 :class:`spider_gd.interfaces.scheduler.IScheduler`。
- cron 定时（CronTrigger）+ 手动立即触发（trigger_now 直接 await handler）。
- 维护 job_id → (spec, handler) 注册表，便于 list / trigger。
"""

from __future__ import annotations

from loguru import logger

from spider_gd.interfaces.scheduler import JobHandler, JobSpec


class TaskScheduler:
    def __init__(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler()
        self._registry: dict[str, tuple[JobSpec, JobHandler]] = {}

    async def add_job(self, spec: JobSpec, handler: JobHandler) -> str:
        from apscheduler.triggers.cron import CronTrigger

        self._registry[spec.job_id] = (spec, handler)
        if spec.enabled:
            self._scheduler.add_job(
                handler,
                trigger=CronTrigger.from_crontab(spec.cron),
                args=[spec.payload],
                id=spec.job_id,
                replace_existing=True,
            )
        logger.info("注册任务 {} cron={} enabled={}", spec.job_id, spec.cron, spec.enabled)
        return spec.job_id

    async def remove_job(self, job_id: str) -> bool:
        if job_id not in self._registry:
            return False
        self._registry.pop(job_id)
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        logger.info("移除任务 {}", job_id)
        return True

    async def list_jobs(self) -> list[JobSpec]:
        return [spec for spec, _ in self._registry.values()]

    async def trigger_now(self, job_id: str) -> bool:
        entry = self._registry.get(job_id)
        if entry is None:
            logger.warning("手动触发失败：未知任务 {}", job_id)
            return False
        spec, handler = entry
        logger.info("手动触发任务 {}", job_id)
        await handler(spec.payload)
        return True

    async def start(self) -> None:
        self._scheduler.start()
        logger.info("调度器已启动")

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("调度器已关闭")
