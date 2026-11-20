"""保存任务时，调度没变就不要把全部定时任务拆掉再注册一遍。"""

from __future__ import annotations

import sys

import pytest

from spider_gd.app import (
    _TASK_SCHEDULE_SIGNATURES,
    _task_schedule_signature,
    register_jobs,
    reload_task_jobs_if_changed,
)
from spider_gd.config.settings import AppSettings


def test_signature_ignores_order_and_sees_time_changes():
    morning = [
        {"id": "b", "name": "乙", "repeat": "daily", "time": "08:00"},
        {"id": "a", "name": "甲", "repeat": "daily", "time": "09:30"},
    ]
    same = list(reversed(morning))
    changed = [
        {"id": "a", "name": "甲", "repeat": "daily", "time": "09:31"},
        {"id": "b", "name": "乙", "repeat": "daily", "time": "08:00"},
    ]

    assert _task_schedule_signature(morning) == _task_schedule_signature(same)
    assert _task_schedule_signature(morning) != _task_schedule_signature(changed)


class _Store:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def list_task_configs(self) -> list[dict]:
        return self.rows


class _Job:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id


class _Scheduler:
    def __init__(self) -> None:
        self.jobs = [_Job("task_a"), _Job("cleanup_exec_logs")]
        self.removed: list[str] = []
        self.added: list[str] = []

    async def add_job(self, spec, _handler) -> str:
        self.added.append(spec.job_id)
        return spec.job_id

    async def list_jobs(self) -> list[_Job]:
        return list(self.jobs)

    async def remove_job(self, job_id: str) -> bool:
        self.removed.append(job_id)
        self.jobs = [job for job in self.jobs if job.job_id != job_id]
        return True


class _Ctx:
    def __init__(self, rows: list[dict]) -> None:
        self.state_store = _Store(rows)
        self.scheduler = _Scheduler()


@pytest.mark.unit
async def test_repeated_saves_reload_scheduler_once(monkeypatch):
    rows = [
        {"id": "a", "name": "信息披露", "enabled": True, "repeat": "daily", "time": "08:00"},
        {"id": "b", "name": "交易日历", "enabled": True, "repeat": "daily", "time": "08:00"},
    ]
    ctx = _Ctx(rows)
    calls = {"n": 0}

    async def fake_register(context, **_kwargs):
        calls["n"] += 1
        from spider_gd import app as app_module

        app_module._remember_task_schedule(context, app_module._load_task_configs(context))
        return ["task_a", "task_b"]

    monkeypatch.setattr("spider_gd.app.register_jobs", fake_register)
    _TASK_SCHEDULE_SIGNATURES.clear()

    assert await reload_task_jobs_if_changed(ctx) is True
    assert await reload_task_jobs_if_changed(ctx) is False
    assert await reload_task_jobs_if_changed(ctx) is False
    assert calls["n"] == 1
    assert ctx.scheduler.removed == ["task_a"]

    rows[0]["time"] = "16:30"
    assert await reload_task_jobs_if_changed(ctx) is True
    assert calls["n"] == 2


@pytest.mark.parametrize("frozen", [False, True])
async def test_beta_never_registers_imported_enabled_automatic_jobs(monkeypatch, frozen):
    from spider_gd import build_stamp

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(build_stamp, "RELEASE_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "stable" if frozen else "beta")
    rows = [{"id": "a", "name": "采集", "enabled": True,
             "repeat": "daily", "time": "08:00"}]
    ctx = _Ctx(rows)
    ctx.settings = AppSettings()
    ctx.settings.schedule.crawl_cron = ["0 8 * * *"]
    ctx.settings.schedule.backfill_cron = "0 9 * * *"
    ctx.settings.schedule.retention_cron = "0 10 * * *"
    assert await register_jobs(ctx) == [], "测试版不能恢复导入配置中的自动任务"
    assert ctx.scheduler.added == []
    assert rows[0]["enabled"] is True, "运行门禁不能改写用户导入的数据"


@pytest.mark.parametrize("frozen", [False, True])
async def test_stable_registers_enabled_automatic_jobs(monkeypatch, frozen):
    from spider_gd import build_stamp

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(build_stamp, "RELEASE_CHANNEL", "stable")
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta" if frozen else "stable")
    ctx = _Ctx([{
        "id": "a", "name": "采集", "enabled": True,
        "repeat": "daily", "time": "08:00",
    }])
    ctx.settings = AppSettings()
    ctx.settings.schedule.crawl_cron = ["0 8 * * *"]
    ctx.settings.schedule.backfill_cron = "0 9 * * *"
    ctx.settings.schedule.retention_cron = "0 10 * * *"
    expected = [
        "task_a", "crawl_daily_0", "backfill", "retention", "cleanup_exec_logs",
        "delivery_outbox_retry", "alert_outbox_retry",
    ]
    assert await register_jobs(ctx) == expected
    assert ctx.scheduler.added == expected
