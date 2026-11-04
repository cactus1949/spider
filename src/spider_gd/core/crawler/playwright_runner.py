"""Playwright 浏览器执行器与登录页操作实现（适配器，运行时依赖 playwright）。

实现 :class:`spider_gd.core.crawler.login_flow.LoginPageOps`。
playwright 延迟导入，便于无浏览器环境下导入本模块。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from spider_gd.core.crawler.exceptions import (
    SiteUnreachableError,
    is_playwright_goto_error,
)
from spider_gd.core.pipeline.file_versions import save_versioned_bytes

@dataclass
class LoginSelectors:
    username: str = "input[name='username']"
    password: str = "input[type='password']"
    captcha_image: str = "img.captcha-img"
    captcha_input: str = "input[name='captcha']"
    submit: str = "button[type='submit']"
    logged_in_marker: str = ".user"


class PlaywrightRunner:
    """管理浏览器生命周期并提供登录页操作原语。"""

    def __init__(
        self,
        login_url: str,
        *,
        headless: bool = True,
        user_data_dir: str | None = None,
        download_dir: str | None = None,
        selectors: LoginSelectors | None = None,
        on_event=None,
    ) -> None:
        self._login_url = login_url
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._download_dir = download_dir
        self._selectors = selectors or LoginSelectors()
        self._pw: Any = None
        self._context: Any = None
        self.page: Any = None
        self._chrome_proc: subprocess.Popen | None = None
        self._on_event = on_event
        self._session = None
        self._lease = None

    @property
    def cancel_requested(self) -> bool:
        return bool(self._session and self._session._cancel_requested)

    async def start(self) -> None:
        if self._lease is not None:
            return
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        self._session = get_shared_browser()
        lease = self._session.execution("主采集器", self._on_event)
        await lease.__aenter__()
        self._lease = lease
        self._context = self._session.page.context
        self.page = self._session.page

    async def close(self) -> None:
        if self._lease is not None:
            lease, self._lease = self._lease, None
            import sys
            await lease.__aexit__(*sys.exc_info())
        self.page = None
        self._context = None

    async def open(self) -> None:
        if self.page is None:
            await self.start()
        if self._session is not None and await self._session.require_auth(allow_auto=True, page=self.page):
            return
        try:
            await self.page.goto(self._login_url)
        except Exception as exc:
            if is_playwright_goto_error(exc):
                raise SiteUnreachableError(f"登录页不可达：{exc}") from exc
            raise

    async def fill_credentials(self, username: str, password: str) -> None:
        await self.page.fill(self._selectors.username, username)
        await self.page.fill(self._selectors.password, password)

    async def get_captcha_image(self) -> bytes:
        return await self.page.locator(self._selectors.captcha_image).screenshot()

    async def refresh_captcha(self) -> None:
        """点击验证码图片以刷新验证码。"""
        await self.page.locator(self._selectors.captcha_image).click()

    async def fill_captcha(self, code: str) -> None:
        await self.page.fill(self._selectors.captcha_input, code)

    async def submit(self) -> None:
        await self.page.click(self._selectors.submit)

    async def is_logged_in(self) -> bool:
        if self._session is not None:
            if self._session._cancel_requested:
                raise RuntimeError("用户取消主采集器")
            auth = await self._session.check_auth(self.page)
            if auth["state"] in ("challenge", "wrong_account", "unknown"):
                return await self._session.require_auth(page=self.page)
            return auth["state"] == "authenticated"
        return False

    # --- RPA 兜底：前端仿真导出下载 ---

    async def download_via_click(self, page_url: str, export_selector: str) -> bytes:
        """导航到页面、点击导出按钮、捕获下载并读取文件字节（RPA 兜底用）。

        如果配置了 download_dir，额外保存一份到该目录。
        """
        from pathlib import Path

        if self.page is None:
            await self.start()
        try:
            await self.page.goto(page_url)
        except Exception as exc:
            if is_playwright_goto_error(exc):
                raise SiteUnreachableError(
                    f"RPA 目标页不可达：{exc}"
                ) from exc
            raise
        async with self.page.expect_download() as dl_info:
            await self.page.click(export_selector)
        download = await dl_info.value
        path = await download.path()
        data = Path(path).read_bytes()
        # 额外保存到 download_dir（与 WorkflowRunner 行为一致）
        if self._download_dir:
            dest_dir = Path(self._download_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            filename = download.suggested_filename or f"rpa_export_{int(time.time())}.xlsx"
            dest = dest_dir / filename
            save_versioned_bytes(dest, data)
            logger.info("RPA 导出下载完成：{} → {} ({} bytes)", page_url, dest, len(data))
        else:
            logger.info("RPA 导出下载完成：{} ({} bytes)", page_url, len(data))
        return data
