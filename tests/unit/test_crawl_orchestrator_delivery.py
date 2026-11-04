from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from spider_gd.core.crawler.crawl_orchestrator import CrawlOrchestrator
from spider_gd.core.pipeline.local_landing import LocalLanding
from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.interfaces.crawler import CrawlResult, CrawlTask, EndpointConfig, Session


class _FakeKeeper:
    async def fetch(
        self,
        fetcher: object,
        endpoint: EndpointConfig,
        task: CrawlTask,
    ) -> CrawlResult:
        return await fetcher.fetch(Session(bearer_token="tok"), endpoint, task)


class _FakeFetcher:
    async def fetch(
        self,
        _session: Session,
        _endpoint: EndpointConfig,
        task: CrawlTask,
    ) -> CrawlResult:
        return CrawlResult(task_id=task.task_id, success=True, data=b"stable delivery data")


class _PartiallyFailingFetcher:
    async def fetch(
        self,
        _session: Session,
        _endpoint: EndpointConfig,
        task: CrawlTask,
    ) -> CrawlResult:
        if task.data_type == "失败指标":
            return CrawlResult(task_id=task.task_id, success=False, error_msg="network down")
        return CrawlResult(task_id=task.task_id, success=True, data=b"partial stable data")


def _build_orchestrator(
    tmp_path: Path,
    api_fetcher: object | None = None,
    **kwargs: object,
) -> tuple[CrawlOrchestrator, TaskStateStore]:
    store = TaskStateStore(str(tmp_path / "spider.db"))
    landing = LocalLanding(str(tmp_path / "data"))
    orchestrator = CrawlOrchestrator(
        session_keeper=_FakeKeeper(),
        api_fetcher=api_fetcher or _FakeFetcher(),
        state_store=store,
        landing=landing,
        lock_dir=str(tmp_path / "locks"),
        **kwargs,
    )
    return orchestrator, store


@pytest.mark.unit
async def test_delivery_hook_receives_completed_task_zip(tmp_path: Path):
    calls: list[tuple[str, str, str, dict[str, object]]] = []

    async def delivery_hook(
        task_id: str,
        task_dir: str,
        zip_path: str,
        summary: dict[str, object],
    ) -> None:
        calls.append((task_id, task_dir, zip_path, summary))

    orchestrator, store = _build_orchestrator(tmp_path, delivery_hook=delivery_hook)
    try:
        result = await orchestrator.run(
            task_name="稳定发送",
            endpoints=[EndpointConfig(name="日清算概览", url="https://example.test/export")],
            target_date="2026-06-18",
        )
    finally:
        store.close()

    assert result["status"] == "completed"
    assert Path(str(result["zip"])).exists()
    assert len(calls) == 1
    assert calls[0][0] == result["task_id"]
    assert calls[0][2] == result["zip"]
    assert calls[0][3]["success"] == 1


@pytest.mark.unit
async def test_delivery_failure_does_not_change_completed_status(tmp_path: Path):
    alerts: list[tuple[str, str]] = []

    async def delivery_hook(
        _task_id: str,
        _task_dir: str,
        _zip_path: str,
        _summary: dict[str, object],
    ) -> None:
        raise RuntimeError("wecom down")

    async def alert_hook(title: str, message: str) -> None:
        alerts.append((title, message))

    orchestrator, store = _build_orchestrator(
        tmp_path,
        delivery_hook=delivery_hook,
        alert_hook=alert_hook,
    )
    try:
        result = await orchestrator.run(
            task_name="稳定发送",
            endpoints=[EndpointConfig(name="日清算概览", url="https://example.test/export")],
            target_date="2026-06-18",
        )
    finally:
        store.close()

    assert result["status"] == "completed"
    assert Path(str(result["zip"])).exists()
    assert alerts == [("采集任务产物发送失败", f"{result['task_id']}: wecom down")]


@pytest.mark.unit
async def test_delivery_hook_receives_partial_task_zip(tmp_path: Path):
    calls: list[tuple[str, str, str, dict[str, object]]] = []

    async def delivery_hook(
        task_id: str,
        task_dir: str,
        zip_path: str,
        summary: dict[str, object],
    ) -> None:
        calls.append((task_id, task_dir, zip_path, summary))

    orchestrator, store = _build_orchestrator(
        tmp_path,
        api_fetcher=_PartiallyFailingFetcher(),
        delivery_hook=delivery_hook,
    )
    try:
        result = await orchestrator.run(
            task_name="部分成功稳定发送",
            endpoints=[
                EndpointConfig(name="成功指标", url="https://example.test/success"),
                EndpointConfig(name="失败指标", url="https://example.test/fail"),
            ],
            target_date="2026-06-18",
        )
    finally:
        store.close()

    zip_path = Path(str(result["zip"]))
    assert result["status"] == "partial"
    assert zip_path.exists()
    assert len(calls) == 1
    assert calls[0][0] == result["task_id"]
    assert calls[0][2] == result["zip"]
    assert calls[0][3]["total"] == 2
    assert calls[0][3]["success"] == 1
    assert calls[0][3]["failed"] == 1
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("成功指标" in name for name in names)


@pytest.mark.unit
async def test_cancelled_browser_run_does_not_fetch_or_deliver(tmp_path):
    class NeverFetch:
        async def fetch(self, *args):
            pytest.fail('取消后的任务不能执行 API 请求')

    orchestrator, store = _build_orchestrator(
        tmp_path, api_fetcher=NeverFetch(), cancel_requested=lambda: True,
    )
    try:
        result = await orchestrator.run(
            task_name='cancelled', endpoints=[EndpointConfig(name='test', url='http://example.test')],
            target_date='2026-10-27',
        )
        assert result['status'] == 'cancelled'
        assert store.get_items(result['task_id'])[0].status == 'pending'
    finally:
        store.close()
