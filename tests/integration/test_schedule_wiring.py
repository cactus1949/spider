"""集成：调度任务注册与手动触发（注入假 handler，不启动浏览器）。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from spider_gd.app import assemble, register_jobs
from spider_gd.config.settings import load_config


@pytest.mark.integration
@respx.mock
async def test_register_jobs_creates_expected_jobs(tmp_path: Path):
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = ":memory:"
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    calls: list[str] = []

    async def _crawl(_payload):
        calls.append("crawl")

    async def _backfill(_payload):
        calls.append("backfill")

    async def _retention(_payload):
        calls.append("retention")

    try:
        job_ids = await register_jobs(
            ctx,
            crawl_handler=_crawl,
            backfill_handler=_backfill,
            retention_handler=_retention,
        )
        # cleanup_exec_logs、delivery_outbox_retry 和 alert_outbox_retry 始终注册。
        # delivery/alert 初始未启用或未配置时也要注册，设置页后续开启后无需重启。
        assert "cleanup_exec_logs" in job_ids
        assert "delivery_outbox_retry" in job_ids
        assert "alert_outbox_retry" in job_ids

        # 手动触发验证 handler 可调用
        await ctx.scheduler.trigger_now("cleanup_exec_logs")
        await ctx.scheduler.trigger_now("delivery_outbox_retry")
        await ctx.scheduler.trigger_now("alert_outbox_retry")

        webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=runtime"
        route = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
        zip_path = tmp_path / "result.zip"
        zip_path.write_bytes(b"123456")
        ctx.state_store.save_settings({
            "deliveryEnabled": "true",
            "deliveryWecomWebhook": webhook,
            "deliverySendFile": "false",
        })
        ctx.state_store.enqueue_delivery_outbox(
            task_id="RUNTIME_ON",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="waiting",
        )

        await ctx.scheduler.trigger_now("delivery_outbox_retry")

        assert route.called
        sent = ctx.state_store.list_delivery_outbox(status="sent")
        assert sent[0]["task_id"] == "RUNTIME_ON"

        recoverable_zip = tmp_path / "recoverable.zip"
        recoverable_zip.write_bytes(b"retry me")
        ctx.state_store.enqueue_delivery_outbox(
            task_id="RECOVERABLE_FAILED",
            task_dir=str(tmp_path),
            zip_path=str(recoverable_zip),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="network down",
        )
        ctx.state_store.mark_delivery_outbox_failed(
            "RECOVERABLE_FAILED",
            "network down",
            status="failed",
        )

        await ctx.scheduler.trigger_now("delivery_outbox_retry")

        recovered = ctx.state_store.get_delivery_outbox("RECOVERABLE_FAILED")
        assert recovered is not None
        assert recovered["status"] == "sent"
    finally:
        ctx.state_store.close()
