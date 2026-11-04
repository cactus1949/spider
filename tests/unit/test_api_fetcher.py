"""测试通用 API 采集引擎（respx mock httpx）。"""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from spider_gd.core.crawler.api_fetcher import ApiFetcher
from spider_gd.core.crawler.exceptions import (
    AuthFailedError,
    SiteUnreachableError,
    TokenExpiredError,
)
from spider_gd.interfaces.crawler import CrawlTask, EndpointConfig, Session

_URL = "https://pm.gd.csg.cn/export"
_EXCEL_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _endpoint() -> EndpointConfig:
    return EndpointConfig(name="daily", url=_URL, method="POST")


def _session() -> Session:
    return Session(bearer_token="tok", cookies={"SESSION_ID": "s1"}, hdkgccbl="abc123")


def _task() -> CrawlTask:
    return CrawlTask(task_id="T1", date="20260618", data_type="daily")


@pytest.mark.unit
@respx.mock
async def test_fetch_success_returns_excel_bytes():
    route = respx.route(method="POST", url__regex=rf"{re.escape(_URL)}.*").mock(
        return_value=httpx.Response(200, headers={"Content-Type": _EXCEL_CT}, content=b"PKxlsx")
    )
    result = await ApiFetcher(impersonate=None).fetch(_session(), _endpoint(), _task())
    assert result.success is True
    assert result.data == b"PKxlsx"
    # HdKGccbL 应拼到 URL
    assert "HdKGccbL=abc123" in str(route.calls[0].request.url)
    # Authorization / Cookie 头
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert "SESSION_ID=s1" in route.calls[0].request.headers["Cookie"]


@pytest.mark.unit
@respx.mock
async def test_fetch_412_raises_token_expired():
    respx.route(method="POST", url__regex=rf"{re.escape(_URL)}.*").mock(
        return_value=httpx.Response(412, json={"msg": "expired"})
    )
    with pytest.raises(TokenExpiredError):
        await ApiFetcher(impersonate=None).fetch(_session(), _endpoint(), _task())


@pytest.mark.unit
@respx.mock
async def test_fetch_401_raises_auth_failed():
    respx.route(method="POST", url__regex=rf"{re.escape(_URL)}.*").mock(
        return_value=httpx.Response(401, json={"msg": "no"})
    )
    with pytest.raises(AuthFailedError):
        await ApiFetcher(impersonate=None).fetch(_session(), _endpoint(), _task())


@pytest.mark.unit
@respx.mock
async def test_fetch_non_excel_returns_failure():
    respx.route(method="POST", url__regex=rf"{re.escape(_URL)}.*").mock(
        return_value=httpx.Response(200, json={"msg": "无数据"})
    )
    result = await ApiFetcher(impersonate=None).fetch(_session(), _endpoint(), _task())
    assert result.success is False
    assert "无数据" in (result.error_msg or "")


@pytest.mark.unit
@respx.mock
async def test_fetch_retries_on_network_error_then_succeeds():
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, headers={"Content-Type": _EXCEL_CT}, content=b"ok")

    respx.route(method="POST", url__regex=rf"{re.escape(_URL)}.*").mock(side_effect=responder)

    async def _no_sleep(_d):
        return None

    fetcher = ApiFetcher(max_retries=3, sleep_fn=_no_sleep, impersonate=None)
    result = await fetcher.fetch(_session(), _endpoint(), _task())
    assert result.success is True
    assert calls["n"] == 2


@pytest.mark.unit
@respx.mock
async def test_fetch_all_retries_connection_error_raises_site_unreachable():
    """连接错误耗尽重试后应抛出 SiteUnreachableError（而非静默失败）。"""
    respx.route(method="POST", url__regex=rf"{re.escape(_URL)}.*").mock(
        side_effect=httpx.ConnectError("连接被拒绝")
    )

    async def _no_sleep(_d):
        return None

    fetcher = ApiFetcher(max_retries=2, sleep_fn=_no_sleep, impersonate=None)
    with pytest.raises(SiteUnreachableError, match="目标站点不可达"):
        await fetcher.fetch(_session(), _endpoint(), _task())
