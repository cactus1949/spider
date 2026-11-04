"""会话守护：集中处理 412/Token 过期的刷新与重登（决策二）。

不把 412 重试硬编码进编排器 try/except，单一职责封装在此：
- 持有当前 :class:`Session`，首次按需登录。
- ``fetch`` 包裹 :class:`IApiFetcher.fetch`，捕获 :class:`TokenExpiredError` →
  轻量 ``refresh()``（失败再 ``LoginFlow.execute()`` 完整重登）→ 重试。
- 最大重登次数上限；连续失败（如 U-Key 被拔）抛 :class:`ReloginExhaustedError`，不无限循环。
- 一把 ``asyncio.Lock`` 防重入：串行模型下避免多请求重复触发重登。
"""

from __future__ import annotations

import asyncio

from loguru import logger

from spider_gd.core.crawler.exceptions import CrawlerError, TokenExpiredError
from spider_gd.interfaces.crawler import (
    CrawlResult,
    CrawlTask,
    EndpointConfig,
    IApiFetcher,
    ILoginFlow,
    ISessionProvider,
    Session,
)


class ReloginExhaustedError(CrawlerError):
    """刷新/重登达到上限仍无法获得有效会话（终止信号，需告警）。"""


class SessionKeeper:
    def __init__(
        self,
        login_flow: ILoginFlow,
        session_provider: ISessionProvider,
        *,
        max_relogin: int = 2,
    ) -> None:
        self._login_flow = login_flow
        self._session_provider = session_provider
        self._max_relogin = max_relogin
        self._session: Session | None = None
        self._lock = asyncio.Lock()

    async def get_session(self) -> Session:
        """返回当前会话；首次调用触发登录。"""
        async with self._lock:
            if self._session is None:
                self._session = await self._login_flow.execute()
            return self._session

    async def _renew(self) -> Session:
        """续期会话：优先轻量 refresh，失败再完整重登。带锁防重入。"""
        async with self._lock:
            try:
                self._session = await self._session_provider.refresh()
                logger.info("会话已通过 refresh 续期")
            except Exception as exc:  # refresh 失败（如凭证已不可读）→ 完整重登
                logger.warning("refresh 失败（{}），转完整重登", exc)
                self._session = await self._login_flow.execute()
            return self._session

    async def fetch(
        self, fetcher: IApiFetcher, endpoint: EndpointConfig, task: CrawlTask
    ) -> CrawlResult:
        """带 412 自动重登的 fetch。重登耗尽抛 :class:`ReloginExhaustedError`。"""
        session = await self.get_session()
        last_err: Exception | None = None
        # 首次 + 最多 max_relogin 次重登后的重试
        for attempt in range(self._max_relogin + 1):
            try:
                return await fetcher.fetch(session, endpoint, task)
            except TokenExpiredError as exc:
                last_err = exc
                if attempt >= self._max_relogin:
                    break
                logger.warning("Token 过期，第 {} 次续期重试", attempt + 1)
                try:
                    session = await self._renew()
                except CrawlerError as exc2:
                    raise ReloginExhaustedError(f"续期失败终止：{exc2}") from exc2
        raise ReloginExhaustedError(f"重登 {self._max_relogin} 次后仍过期：{last_err}")
