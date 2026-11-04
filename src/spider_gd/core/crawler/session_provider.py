"""会话凭证提供者：以浏览器会话为真相源。

实时从 Playwright 页面读取：
- ``HdKGccbL``：来自页面 URL 的 query 参数（与同事代码一致）。
- Bearer Token：localStorage / sessionStorage 常见键。
- Cookie：当前 context 的 cookies。

实现 :class:`spider_gd.interfaces.crawler.ISessionProvider`。
``page`` 仅需提供 ``url`` 属性、异步 ``evaluate`` 与 ``context.cookies()``，便于测试注入假对象。
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import parse_qs, urlparse

from loguru import logger

from spider_gd.interfaces.crawler import Session

_TOKEN_JS_CANDIDATES: tuple[str, ...] = (
    "localStorage.getItem('token')",
    "localStorage.getItem('access_token')",
    "sessionStorage.getItem('token')",
    "sessionStorage.getItem('access_token')",
    "window.__token__",
)


def parse_hdkgccbl(url: str) -> str:
    """从页面 URL 中提取 HdKGccbL 参数（同时兼容 query 与 hash 段）。"""
    parsed = urlparse(url)
    for segment in (parsed.query, parsed.fragment):
        qs = parse_qs(segment)
        if qs.get("HdKGccbL"):
            return qs["HdKGccbL"][0]
        # hash 路由形如 #/path?HdKGccbL=xxx
        if "HdKGccbL=" in segment:
            return segment.split("HdKGccbL=")[1].split("&")[0].split("#")[0]
    return ""


class BrowserSessionProvider:
    def __init__(
        self,
        page: object,
        *,
        token_js_candidates: Sequence[str] = _TOKEN_JS_CANDIDATES,
        expired_codes: tuple[int, ...] = (412,),
    ) -> None:
        self._page = page
        self._token_js = tuple(token_js_candidates)
        self._expired_codes = expired_codes

    async def _read_token(self) -> str:
        for expr in self._token_js:
            value = await self._page.evaluate(expr)  # type: ignore[attr-defined]
            if value:
                return str(value)
        return ""

    async def _read_cookies(self) -> dict[str, str]:
        cookies = await self._page.context.cookies()  # type: ignore[attr-defined]
        return {c["name"]: c["value"] for c in cookies}

    async def get_session(self) -> Session:
        url = self._page.url  # type: ignore[attr-defined]
        token = await self._read_token()
        cookies = await self._read_cookies()
        hdkgccbl = parse_hdkgccbl(url)
        if not token:
            logger.warning("未从页面读取到 Bearer Token")
        logger.debug("读取会话: token={} hdkgccbl={}", "***" if token else "", hdkgccbl)
        return Session(
            bearer_token=token,
            cookies=cookies,
            hdkgccbl=hdkgccbl,
            page_url=url,
        )

    def is_expired(self, status_code: int) -> bool:
        return status_code in self._expired_codes

    async def refresh(self) -> Session:
        logger.info("刷新会话（重新读取浏览器实时凭证）")
        return await self.get_session()
