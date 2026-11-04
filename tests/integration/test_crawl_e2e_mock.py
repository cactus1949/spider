"""集成：采集编排器对 mock_site 跑通端到端全链路。

覆盖三个关键分支（阶段 1 验收）：
- 412 重登：SessionKeeper 捕获 TokenExpiredError → refresh 重登 → 重试成功。
- 部分失败：多 endpoint 一成一败 → 任务状态 partial，逐 item 独立状态。
- 断点续采：以同 task_id 重跑，仅处理上次 pending/failed 的 item。

登录走 httpx 直连 mock（验证码任意），避免在 CI 拉起真实浏览器；
取数走真实 ApiFetcher + 真实 mock 导出接口，确保链路真实。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from spider_gd.core.crawler.api_fetcher import ApiFetcher
from spider_gd.core.crawler.crawl_orchestrator import CrawlOrchestrator
from spider_gd.core.crawler.exceptions import TokenExpiredError
from spider_gd.core.crawler.session_keeper import SessionKeeper
from spider_gd.core.pipeline.data_processor import DataQualityError
from spider_gd.core.pipeline.local_landing import LocalLanding
from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.interfaces.crawler import CrawlResult, CrawlTask, EndpointConfig, Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MOCK_DIR = _REPO_ROOT / "mock_site"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mock_server() -> str:
    """在后台线程启动 mock_site，返回 base_url。"""
    import uvicorn

    if str(_MOCK_DIR) not in sys.path:
        sys.path.insert(0, str(_MOCK_DIR))
    import main as mock_main  # mock_site/main.py

    port = _free_port()
    config = uvicorn.Config(mock_main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("mock_site 启动超时")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


async def _do_login(base_url: str, client: httpx.AsyncClient) -> Session:
    """直连 mock /login，返回带有效 token 的 Session（cookie 由 client jar 持有）。"""
    await client.post(
        f"{base_url}/login",
        data={"username": "admin", "password": "admin123", "captcha": "1234"},
    )
    token = client.cookies.get("X-Auth-Token") or ""
    return Session(bearer_token=token, cookies={}, hdkgccbl="", page_url=base_url)


class _FakeLogin:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._base = base_url
        self._client = client

    async def execute(self) -> Session:
        return await _do_login(self._base, self._client)


class _FakeProvider:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._base = base_url
        self._client = client

    async def get_session(self) -> Session:
        return await _do_login(self._base, self._client)

    def is_expired(self, status_code: int) -> bool:
        return status_code == 412

    async def refresh(self) -> Session:
        return await _do_login(self._base, self._client)


class _ControllableFetcher:
    """包裹真实 ApiFetcher，按指标名注入受控失败/一次性 412，并记录调用。"""

    def __init__(self, inner: ApiFetcher) -> None:
        self._inner = inner
        self.calls: list[str] = []
        self.fail_indicators: set[str] = set()
        self.expire_once: set[str] = set()
        self._expired_fired: set[str] = set()

    async def fetch(
        self, session: Session, endpoint: EndpointConfig, task: CrawlTask
    ) -> CrawlResult:
        self.calls.append(endpoint.name)
        if endpoint.name in self.expire_once and endpoint.name not in self._expired_fired:
            self._expired_fired.add(endpoint.name)
            raise TokenExpiredError("强制 412（测试）")
        if endpoint.name in self.fail_indicators:
            return CrawlResult(task_id=task.task_id, success=False, error_msg="强制失败（测试）")
        return await self._inner.fetch(session, endpoint, task)


def _endpoints(base_url: str) -> list[EndpointConfig]:
    return [
        EndpointConfig(name="日清算概览", url=f"{base_url}/api/export", hdkgccbl_source="none"),
        EndpointConfig(
            name="月度结算",
            url=f"{base_url}/api/export_monthly",
            params={"year": "2026"},
            hdkgccbl_source="none",
        ),
    ]


def _build(tmp_path: Path, base_url: str, client: httpx.AsyncClient, processor=None):
    store = TaskStateStore(str(tmp_path / "spider.db"))
    landing = LocalLanding(str(tmp_path / "data"))
    keeper = SessionKeeper(_FakeLogin(base_url, client), _FakeProvider(base_url, client))
    fetcher = _ControllableFetcher(ApiFetcher(client=client))
    orch = CrawlOrchestrator(
        session_keeper=keeper,
        api_fetcher=fetcher,
        state_store=store,
        landing=landing,
        lock_dir=str(tmp_path / "locks"),
        processor=processor,
    )
    return orch, fetcher, store


@pytest.mark.integration
async def test_happy_path_all_success(mock_server: str, tmp_path: Path):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        orch, _fetcher, store = _build(tmp_path, mock_server, client)
        try:
            result = await orch.run(
                task_name="日清算采集",
                endpoints=_endpoints(mock_server),
                target_date="2026-06-18",
            )
        finally:
            store.close()

    assert result["status"] == "completed"
    assert result["summary"]["success"] == 2
    assert Path(result["ledger"]).exists()
    assert Path(result["zip"]).exists()


@pytest.mark.integration
async def test_412_triggers_relogin_then_succeeds(mock_server: str, tmp_path: Path):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        orch, fetcher, store = _build(tmp_path, mock_server, client)
        fetcher.expire_once = {"日清算概览"}
        try:
            result = await orch.run(
                task_name="日清算采集",
                endpoints=_endpoints(mock_server),
                target_date="2026-06-18",
            )
        finally:
            store.close()

    assert result["status"] == "completed"
    assert result["summary"]["success"] == 2
    # 日清算概览被调用两次：首次 412，重登后重试成功
    assert fetcher.calls.count("日清算概览") == 2


@pytest.mark.integration
async def test_partial_failure_marks_partial(mock_server: str, tmp_path: Path):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        orch, fetcher, store = _build(tmp_path, mock_server, client)
        fetcher.fail_indicators = {"月度结算"}
        try:
            result = await orch.run(
                task_name="日清算采集",
                endpoints=_endpoints(mock_server),
                target_date="2026-06-18",
            )
            items = {i.indicator_name: i.status for i in store.get_items(str(result["task_id"]))}
        finally:
            store.close()

    assert result["status"] == "partial"
    assert items["日清算概览"] == "success"
    assert items["月度结算"] == "failed"


@pytest.mark.integration
async def test_resume_only_retries_pending(mock_server: str, tmp_path: Path):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        orch, fetcher, store = _build(tmp_path, mock_server, client)
        fetcher.fail_indicators = {"月度结算"}
        try:
            first = await orch.run(
                task_name="日清算采集",
                endpoints=_endpoints(mock_server),
                target_date="2026-06-18",
            )
            task_id = str(first["task_id"])
            assert first["status"] == "partial"

            # 修复故障后以同 task_id 续采：只应处理上次失败的「月度结算」
            fetcher.fail_indicators = set()
            fetcher.calls.clear()
            second = await orch.run(
                task_name="日清算采集",
                endpoints=_endpoints(mock_server),
                target_date="2026-06-18",
                task_id=task_id,
            )
        finally:
            store.close()

    assert second["task_id"] == task_id
    assert second["status"] == "completed"
    assert second["summary"]["success"] == 2
    # 续采仅重试上次失败项，不重采已成功的「日清算概览」
    assert fetcher.calls == ["月度结算"]


def _failing_processor(indicators: set[str]):
    """返回对指定 indicator 抛 DataQualityError 的 processor。"""
    def _process(data: bytes, indicator: str) -> bytes:
        if indicator in indicators:
            raise DataQualityError(f"模拟数据质量校验失败：{indicator}")
        return data
    return _process


@pytest.mark.integration
async def test_processor_error_does_not_abort_task(mock_server: str, tmp_path: Path):
    """processor 抛 DataQualityError 应只影响单个 item，不中断整个任务。"""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        processor = _failing_processor({"月度结算"})
        orch, fetcher, store = _build(tmp_path, mock_server, client, processor=processor)
        try:
            result = await orch.run(
                task_name="日清算采集",
                endpoints=_endpoints(mock_server),
                target_date="2026-06-18",
            )
            items = {i.indicator_name: i.status for i in store.get_items(str(result["task_id"]))}
        finally:
            store.close()

    assert result["status"] == "partial"
    assert items["日清算概览"] == "success"
    assert items["月度结算"] == "failed"
    # 两个 endpoint 都应被执行（processor 异常不应中断后续 endpoint）
    assert "日清算概览" in fetcher.calls
    assert "月度结算" in fetcher.calls


@pytest.mark.integration
async def test_all_failed_skips_compress(mock_server: str, tmp_path: Path):
    """全失败任务不应执行压缩。"""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        orch, fetcher, store = _build(tmp_path, mock_server, client)
        fetcher.fail_indicators = {"日清算概览", "月度结算"}
        try:
            result = await orch.run(
                task_name="日清算采集",
                endpoints=_endpoints(mock_server),
                target_date="2026-06-18",
            )
        finally:
            store.close()

    assert result["status"] == "failed"
    assert result["zip"] is None
