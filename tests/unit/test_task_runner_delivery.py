from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from spider_gd.app import AppContext, _task_runner_job
from spider_gd.config.settings import AppSettings, DeliverySettings
from spider_gd.infrastructure.storage.database import TaskStateStore


@pytest.mark.unit
@respx.mock
async def test_scheduled_task_delivery_failure_enters_outbox(tmp_path, mocker):
    class FakeTaskRunner:
        def __init__(self, _cfg, *, download_dir, **_kwargs):
            self.download_dir = Path(download_dir)

        async def run(self, on_event=None, **_kwargs):
            (self.download_dir / "result.xlsx").write_bytes(b"result")
            if on_event:
                on_event({"event": "task_done", "name": "稳定发送任务"})

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    respx.post(webhook).mock(return_value=httpx.Response(500, json={"errcode": 1}))
    mocker.patch.dict(
        sys.modules,
        {"task_runner": SimpleNamespace(TaskRunner=FakeTaskRunner)},
    )

    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    settings.storage.data_dir = str(tmp_path / "data")
    ctx = AppContext(
        settings=settings,
        state_store=store,
        landing=None,  # type: ignore[arg-type]
        scheduler=None,  # type: ignore[arg-type]
        alert=None,  # type: ignore[arg-type]
        agent=None,
        endpoints=[],
    )
    try:
        store.save_task_config({
            "id": "job-delivery",
            "name": "稳定发送任务",
            "login": "",
            "indicators": ["spot"],
            "repeat": "daily",
            "time": "08:00",
            "enabled": True,
        })

        handler = _task_runner_job(ctx, "job-delivery")
        await handler({})

        logs = store.list_execution_logs()
        pending = store.list_delivery_outbox()
        assert logs[0].status == "success"
        assert "稳定发送状态: pending" in logs[0].detail_log
        assert "可转发: 尚未送达" in logs[0].detail_log
        assert "稳定发送回执:" in logs[0].detail_log
        assert len(pending) == 1
        assert pending[0]["task_id"] == logs[0].exec_id
        assert Path(pending[0]["zip_path"]).exists()
        assert pending[0]["summary"]["source"] == "task_runner"
    finally:
        store.close()


@pytest.mark.unit
async def test_scheduled_task_delivery_env_file_error_still_enters_outbox(tmp_path, mocker):
    class FakeTaskRunner:
        def __init__(self, _cfg, *, download_dir, **_kwargs):
            self.download_dir = Path(download_dir)

        async def run(self, on_event=None, **_kwargs):
            (self.download_dir / "result.xlsx").write_bytes(b"result")
            if on_event:
                on_event({"event": "task_done", "name": "稳定发送任务"})

    mocker.patch.dict(
        sys.modules,
        {"task_runner": SimpleNamespace(TaskRunner=FakeTaskRunner)},
    )

    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            env_file=str(tmp_path / "missing-delivery.env"),
        )
    )
    settings.storage.data_dir = str(tmp_path / "data")
    ctx = AppContext(
        settings=settings,
        state_store=store,
        landing=None,  # type: ignore[arg-type]
        scheduler=None,  # type: ignore[arg-type]
        alert=None,  # type: ignore[arg-type]
        agent=None,
        endpoints=[],
    )
    try:
        store.save_task_config({
            "id": "job-delivery-env-error",
            "name": "稳定发送任务",
            "login": "",
            "indicators": ["spot"],
            "repeat": "daily",
            "time": "08:00",
            "enabled": True,
        })

        handler = _task_runner_job(ctx, "job-delivery-env-error")
        await handler({})

        logs = store.list_execution_logs()
        pending = store.list_delivery_outbox()
        assert logs[0].status == "success"
        assert len(pending) == 1
        assert pending[0]["task_id"] == logs[0].exec_id
        assert "env 文件不存在" in pending[0]["last_error"]
        assert "稳定发送状态: pending" in logs[0].detail_log
        assert "env 文件不存在" in logs[0].detail_log
        assert Path(pending[0]["zip_path"]).exists()
    finally:
        store.close()


@pytest.mark.unit
async def test_scheduled_task_failure_without_downloads_sends_alert(tmp_path, mocker):
    class FakeTaskRunner:
        def __init__(self, _cfg, *, download_dir, **_kwargs):
            self.download_dir = Path(download_dir)

        async def run(self, on_event=None, **_kwargs):
            if on_event:
                on_event({"event": "task_error", "error": "Page.goto: net::ERR_ABORTED"})

    class FakeAlert:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def send_alert(self, level, message, event_type="info"):
            self.calls.append({
                "level": level,
                "message": message,
                "event_type": event_type,
            })
            return SimpleNamespace(sent=True)

    mocker.patch.dict(
        sys.modules,
        {"task_runner": SimpleNamespace(TaskRunner=FakeTaskRunner)},
    )

    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings()
    settings.storage.data_dir = str(tmp_path / "data")
    alert = FakeAlert()
    ctx = AppContext(
        settings=settings,
        state_store=store,
        landing=None,  # type: ignore[arg-type]
        scheduler=None,  # type: ignore[arg-type]
        alert=alert,  # type: ignore[arg-type]
        agent=None,
        endpoints=[],
    )
    try:
        store.save_task_config({
            "id": "job-alert-no-download",
            "name": "失败无产物任务",
            "login": "",
            "indicators": ["spot"],
            "repeat": "daily",
            "time": "08:00",
            "enabled": True,
        })

        handler = _task_runner_job(ctx, "job-alert-no-download")
        await handler({})

        logs = store.list_execution_logs()
        assert logs[0].status == "failed"
        assert alert.calls
        assert alert.calls[0]["event_type"] == "task_failed"
        assert "Page.goto: net::ERR_ABORTED" in str(alert.calls[0]["message"])
    finally:
        store.close()


@pytest.mark.unit
@respx.mock
async def test_scheduled_task_delivers_downloads_even_when_task_partially_fails(
    tmp_path,
    mocker,
):
    class FakeTaskRunner:
        def __init__(self, _cfg, *, download_dir, **_kwargs):
            self.download_dir = Path(download_dir)

        async def run(self, on_event=None, **_kwargs):
            (self.download_dir / "partial.xlsx").write_bytes(b"partial")
            if on_event:
                on_event({
                    "event": "task_error",
                    "error": "第二个指标失败",
                })

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    route = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    mocker.patch.dict(
        sys.modules,
        {"task_runner": SimpleNamespace(TaskRunner=FakeTaskRunner)},
    )

    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    settings.storage.data_dir = str(tmp_path / "data")
    ctx = AppContext(
        settings=settings,
        state_store=store,
        landing=None,  # type: ignore[arg-type]
        scheduler=None,  # type: ignore[arg-type]
        alert=None,  # type: ignore[arg-type]
        agent=None,
        endpoints=[],
    )
    try:
        store.save_task_config({
            "id": "job-partial-delivery",
            "name": "部分失败发送任务",
            "login": "",
            "indicators": ["spot", "node"],
            "repeat": "daily",
            "time": "08:00",
            "enabled": True,
        })

        handler = _task_runner_job(ctx, "job-partial-delivery")
        await handler({})

        logs = store.list_execution_logs()
        sent = store.list_delivery_outbox(status="sent")
        assert logs[0].status == "failed"
        assert "第二个指标失败" in logs[0].reason
        assert len(sent) == 1
        assert sent[0]["task_id"] == logs[0].exec_id
        assert sent[0]["summary"]["task_status"] == "failed"
        assert sent[0]["summary"]["failed"] == 1
        assert route.called
    finally:
        store.close()
