"""测试 RPA 兜底采集（注入假页面操作）。"""

from __future__ import annotations

import pytest

from spider_gd.core.crawler.rpa_fetcher import RpaFetcher
from spider_gd.interfaces.crawler import CrawlTask, EndpointConfig


class _FakePage:
    def __init__(self, *, data: bytes | None = None, exc: Exception | None = None) -> None:
        self._data = data
        self._exc = exc
        self.calls: list[tuple[str, str]] = []

    async def download_via_click(self, page_url: str, export_selector: str) -> bytes:
        self.calls.append((page_url, export_selector))
        if self._exc is not None:
            raise self._exc
        assert self._data is not None
        return self._data


def _task() -> CrawlTask:
    return CrawlTask(task_id="T1", date="20260618", data_type="日清算概览")


def _endpoint() -> EndpointConfig:
    return EndpointConfig(name="日清算概览", url="http://x/api/export")


@pytest.mark.unit
async def test_rpa_success_returns_bytes():
    page = _FakePage(data=b"PK\x03\x04excel")
    rpa = RpaFetcher(page, {"日清算概览": ("http://x/daily", "#export")})
    result = await rpa.fetch(_endpoint(), _task())
    assert result.success is True
    assert result.data == b"PK\x03\x04excel"
    assert page.calls == [("http://x/daily", "#export")]


@pytest.mark.unit
async def test_rpa_no_mapping_fails_gracefully():
    page = _FakePage(data=b"x")
    rpa = RpaFetcher(page, {})  # 无映射
    result = await rpa.fetch(_endpoint(), _task())
    assert result.success is False
    assert "无页面映射" in (result.error_msg or "")


@pytest.mark.unit
async def test_rpa_download_error_returns_failure():
    page = _FakePage(exc=RuntimeError("下载超时"))
    rpa = RpaFetcher(page, {"日清算概览": ("http://x/daily", "#export")})
    result = await rpa.fetch(_endpoint(), _task())
    assert result.success is False
    assert "RPA 失败" in (result.error_msg or "")
