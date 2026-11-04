"""测试调度器注册/列举/手动触发。"""

from __future__ import annotations

import pytest

from spider_gd.core.scheduler.task_scheduler import TaskScheduler
from spider_gd.interfaces.scheduler import JobSpec


@pytest.mark.unit
async def test_add_and_list_jobs():
    sched = TaskScheduler()

    async def handler(_payload):
        return None

    await sched.add_job(JobSpec(job_id="j1", cron="0 9 * * *", handler_name="h"), handler)
    jobs = await sched.list_jobs()
    assert [j.job_id for j in jobs] == ["j1"]


@pytest.mark.unit
async def test_trigger_now_runs_handler():
    sched = TaskScheduler()
    ran: list[dict] = []

    async def handler(payload):
        ran.append(payload)

    await sched.add_job(
        JobSpec(job_id="j1", cron="0 9 * * *", handler_name="h", payload={"k": "v"}),
        handler,
    )
    ok = await sched.trigger_now("j1")
    assert ok is True
    assert ran == [{"k": "v"}]


@pytest.mark.unit
async def test_trigger_unknown_job_returns_false():
    sched = TaskScheduler()
    assert await sched.trigger_now("missing") is False


@pytest.mark.unit
async def test_start_and_shutdown_lifecycle():
    sched = TaskScheduler()

    async def handler(_payload):
        return None

    await sched.add_job(JobSpec(job_id="j1", cron="0 9 * * *", handler_name="h"), handler)
    await sched.start()
    await sched.shutdown()


@pytest.mark.unit
async def test_disabled_job_not_scheduled_but_listed():
    sched = TaskScheduler()

    async def handler(_payload):
        return None

    await sched.add_job(
        JobSpec(job_id="j1", cron="0 9 * * *", handler_name="h", enabled=False), handler
    )
    jobs = await sched.list_jobs()
    assert [j.job_id for j in jobs] == ["j1"]


@pytest.mark.unit
async def test_remove_job():
    sched = TaskScheduler()

    async def handler(_payload):
        return None

    await sched.add_job(JobSpec(job_id="j1", cron="0 9 * * *", handler_name="h"), handler)
    assert await sched.remove_job("j1") is True
    assert await sched.list_jobs() == []
