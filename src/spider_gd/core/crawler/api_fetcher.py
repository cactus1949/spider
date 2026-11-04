"""通用 API 采集引擎（配置驱动，不含业务逻辑）。

用有效 :class:`Session` + :class:`EndpointConfig` 取数据：
- ``HdKGccbL`` 按 ``endpoint.hdkgccbl_source`` 从会话拼到 URL query。
- 412 → :class:`TokenExpiredError`；401/403 → :class:`AuthFailedError`（不自重登，通知上游）。
- 网络异常指数退避重试；响应内容嗅探区分 Excel 与错误。

实现 :class:`spider_gd.interfaces.crawler.IApiFetcher`。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from loguru import logger

from spider_gd.core.crawler.exceptions import (
    AuthFailedError,
    SiteUnreachableError,
    TokenExpiredError,
)
from spider_gd.interfaces.crawler import CrawlResult, CrawlTask, EndpointConfig, Session

# --- curl_cffi TLS 指纹伪装支持 ---
# httpx 的 TLS 栈与 Chrome 明显不同（JA3/JA4 指纹），可能被服务器直接拒绝。
# curl_cffi 可以伪装成 Chrome 的 TLS 握手特征，让 API 直连与浏览器内请求无区别。
_CFFI_AVAILABLE = False
_CFFI_SESSION: type[Any] | None = None

try:
    from curl_cffi.requests import AsyncSession as _CffiAsyncSession
    from curl_cffi.requests import RequestsError as _CffiError

    _CFFI_AVAILABLE = True
    _CFFI_SESSION = _CffiAsyncSession
    _NETWORK_ERRORS = (httpx.RequestError, _CffiError)
except ImportError:
    _NETWORK_ERRORS = (httpx.RequestError,)


def _is_connection_error(exc: Exception) -> bool:
    """判断异常是否由「目标主机不可达」导致（而非认证/业务错误）。

    覆盖：httpx 连接错误、httpcore 协议错误、curl_cffi 请求错误，
    以及任何包含连接关键字的异常消息。
    """
    # httpx 连接级异常
    if isinstance(exc, httpx.ConnectError):
        return True
    # httpcore 协议异常（如连接被对端关闭）
    try:
        from httpcore import RemoteProtocolError

        if isinstance(exc, RemoteProtocolError):
            return True
    except ImportError:
        pass
    # curl_cffi 请求异常（检查模块来源）
    if type(exc).__module__.startswith("curl_cffi"):
        return True
    # 兜底：字符串匹配连接相关关键字
    msg = str(exc).lower()
    for kw in ("connection", "refused", "reset", "closed", "unreachable", "dns", "timeout"):
        if kw in msg:
            return True
    return False


_EXCEL_CTYPES = ("excel", "spreadsheet", "octet-stream")


def _default_impersonate() -> str | None:
    """选当前 curl_cffi 支持的 Chrome 指纹。

    旧默认 ``chrome130`` 在 curl_cffi 0.17 已不存在（只到 chrome124，
    下一个是 chrome131），构造 session 时直接抛 ImpersonateError。
    这里按可用版本挑最新的 Chrome；一个都不支持就退回 httpx。
    """
    try:
        from curl_cffi.requests import BrowserType

        targets = {item.value for item in BrowserType}
    except Exception:
        return "chrome152"
    for name in ("chrome152", "chrome136", "chrome131", "chrome124"):
        if name in targets:
            return name
    return None


_DEFAULT_IMPERSONATE = _default_impersonate()


def _attach_hdkgccbl(url: str, hdkgccbl: str) -> str:
    if not hdkgccbl:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}HdKGccbL={hdkgccbl}"


_STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def _build_headers(session: Session, endpoint: EndpointConfig) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": endpoint.content_type,
        "User-Agent": _STEALTH_USER_AGENT,
        "Sec-Ch-Ua": (
            '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"'
        ),
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if session.bearer_token:
        headers["Authorization"] = f"Bearer {session.bearer_token}"
    if session.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
    # Referer：优先用页面 URL，回退到平台首页
    if session.page_url:
        headers["Referer"] = session.page_url
    elif session.hdkgccbl:
        # 从 hdkgccbl 推测是已登录的 portal 页面
        headers["Referer"] = "https://pm.gd.csg.cn/portal/"
    headers.update(endpoint.extra_headers)
    return headers


def _is_excel(content_type: str) -> bool:
    ct = content_type.lower()
    return any(x in ct for x in _EXCEL_CTYPES)


class ApiFetcher:
    """通用 API 采集引擎。

    支持两种 HTTP 后端：
    - ``httpx``（默认）：Python 原生 TLS，需要时设置 ``impersonate=None``
    - ``curl_cffi``（推荐）：伪装 Chrome TLS 指纹，绕过 JA3/JA4 检测

    当 ``impersonate`` 非空且 ``curl_cffi`` 已安装时自动启用 curl_cffi 后端；
    显式传入 ``client``（httpx.AsyncClient）则始终使用 httpx。
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
        verify: bool = False,
        impersonate: str | None = _DEFAULT_IMPERSONATE,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._verify = verify
        self._impersonate = impersonate
        self._sleep = sleep_fn or asyncio.sleep
        self._warned_stale_token: set[str] = set()
        # 仅在未显式注入 httpx client 且 curl_cffi 可用时启用 TLS 伪装
        self._use_cffi = (
            impersonate is not None
            and client is None
            and _CFFI_AVAILABLE
        )
        # 复用 curl_cffi session（避免每次请求新建 TLS 连接，更自然）
        self._cffi_client: Any = None
        if self._use_cffi:
            self._cffi_client = _CFFI_SESSION(
                impersonate=self._impersonate,
                verify=self._verify,
                timeout=60,
            )
            logger.info("TLS 伪装已启用：curl_cffi impersonate={}", impersonate)
        elif impersonate is not None and not _CFFI_AVAILABLE:
            logger.info("curl_cffi 未安装，回退到 httpx（无 TLS 伪装）")

    async def aclose(self) -> None:
        """关闭底层 HTTP 会话（curl_cffi / httpx）。"""
        if self._cffi_client is not None:
            try:
                await self._cffi_client.close()
            except Exception:
                pass
            self._cffi_client = None

    async def fetch(
        self, session: Session, endpoint: EndpointConfig, task: CrawlTask
    ) -> CrawlResult:
        url = endpoint.url
        if endpoint.hdkgccbl_source == "page_url":
            # HdKGccbL 是页面 JS 每次请求现算的签名（query 与同名 header 各一份），
            # 页面 URL 上保存的只是历史值。拿它直连，网关只会回 400（text/html、
            # 6 字节空正文），不是业务错误。这里保留兼容，但把风险写进日志。
            if endpoint.name not in self._warned_stale_token:
                self._warned_stale_token.add(endpoint.name)
                logger.warning(
                    "端点 {} 使用页面 URL 的 HdKGccbL：该值是一次性签名，"
                    "直连会被网关拒绝；正式采集必须在页面上下文里发请求",
                    endpoint.name,
                )
            url = _attach_hdkgccbl(url, session.hdkgccbl)

        headers = _build_headers(session, endpoint)

        # 1. 格式化日期
        date_str = task.date  # 原始格式，如 "2026-06-20"
        if endpoint.date_format == "YYYYMMDD":
            date_str = date_str.replace("-", "")

        # 2. 构建参数
        data = dict(endpoint.params)

        started = time.monotonic()

        if self._use_cffi:
            client = self._cffi_client
            owns_client = False  # session 复用，不在此方法内关闭
        else:
            client = self._client or httpx.AsyncClient(verify=self._verify, timeout=60)
            owns_client = self._client is None
        try:
            return await self._fetch_with_retry(
                client, url, endpoint, data, headers, task, started, date_str
            )
        finally:
            if owns_client:
                if self._use_cffi:
                    await client.close()
                else:
                    await client.aclose()

    async def _fetch_with_retry(
        self,
        client: Any,
        url: str,
        endpoint: EndpointConfig,
        data: dict[str, str],
        headers: dict[str, str],
        task: CrawlTask,
        started: float,
        date_str: str,
    ) -> CrawlResult:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                # 3. 根据 method 决定日期参数位置
                if endpoint.method.upper() == "GET":
                    query_params = {**data, endpoint.date_param: date_str}
                    resp = await client.request("GET", url, params=query_params, headers=headers)
                else:
                    data[endpoint.date_param] = date_str
                    resp = await client.request(endpoint.method, url, data=data, headers=headers)
            except _NETWORK_ERRORS as exc:
                last_exc = exc
                delay = self._base_delay * (2 ** (attempt - 1))
                logger.warning("第 {} 次请求失败：{}，{}s 后重试", attempt, exc, delay)
                await self._sleep(delay)
                continue

            return self._handle_response(resp, task, started)

        duration = int((time.monotonic() - started) * 1000)
        if last_exc is not None and _is_connection_error(last_exc):
            raise SiteUnreachableError(
                f"目标站点不可达（{self._max_retries} 次重试后）：{last_exc}"
            ) from last_exc
        return CrawlResult(
            task_id=task.task_id,
            success=False,
            error_msg=f"网络重试耗尽：{last_exc}",
            duration_ms=duration,
        )

    def _handle_response(
        self, resp: Any, task: CrawlTask, started: float
    ) -> CrawlResult:
        duration = int((time.monotonic() - started) * 1000)

        if resp.status_code in (401, 403):
            raise AuthFailedError(f"认证失败 HTTP {resp.status_code}")
        if resp.status_code == 412:
            raise TokenExpiredError("HdKGccbL/Token 已过期 (412)")

        content_type = resp.headers.get("Content-Type", "")
        if _is_excel(content_type):
            logger.info("采集成功 {} bytes={}", task.data_type, len(resp.content))
            return CrawlResult(
                task_id=task.task_id,
                success=True,
                data=resp.content,
                duration_ms=duration,
            )

        # 非 Excel：解析错误信息
        try:
            err = resp.json()
            msg = str(err.get("msg", err))
        except Exception:
            msg = f"非 Excel 响应（{content_type}）"
        logger.warning("采集无数据/错误：{}", msg)
        return CrawlResult(
            task_id=task.task_id,
            success=False,
            error_msg=msg,
            duration_ms=duration,
        )
