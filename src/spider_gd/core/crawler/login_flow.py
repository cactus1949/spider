"""登录流程编排（链路1）。

职责边界：**仅负责登录认证**，产出有效 :class:`Session`；不调用 ``api_fetcher``。
通过 :class:`LoginPageOps` 抽象与具体浏览器解耦，便于注入假对象做单元测试。

实现 :class:`spider_gd.interfaces.crawler.ILoginFlow`。
"""

from __future__ import annotations

import asyncio
import random
from typing import Protocol

from loguru import logger

from spider_gd.core.crawler.exceptions import CaptchaError
from spider_gd.interfaces.crawler import ICaptchaSolver, ISessionProvider, Session


class LoginPageOps(Protocol):
    """登录页操作原语（由 playwright_runner 实现）。"""

    async def open(self) -> None: ...

    async def fill_credentials(self, username: str, password: str) -> None: ...

    async def get_captcha_image(self) -> bytes: ...

    async def refresh_captcha(self) -> None: ...

    async def fill_captcha(self, code: str) -> None: ...

    async def submit(self) -> None: ...

    async def is_logged_in(self) -> bool: ...


class LoginFlow:
    def __init__(
        self,
        page_ops: LoginPageOps,
        captcha_solver: ICaptchaSolver,
        session_provider: ISessionProvider,
        username: str,
        password: str,
        *,
        max_attempts: int = 5,
        captcha_retries_per_attempt: int = 3,
        pre_click_count: int = 4,
    ) -> None:
        """``pre_click_count``：登录前先随机点击验证码 N 次，换掉「难识别」的码。"""
        self._page = page_ops
        self._captcha = captcha_solver
        self._session_provider = session_provider
        self._username = username
        self._password = password
        self._max_attempts = max_attempts
        self._captcha_retries = captcha_retries_per_attempt
        self._pre_click_count = pre_click_count

    async def execute(self) -> Session:
        await self._page.open()

        # 如果已有有效会话（持久化浏览器上下文），可能已处于登录态
        if await self._page.is_logged_in():
            logger.info("检测到已有有效登录会话，跳过登录流程")
            return await self._session_provider.get_session()

        # 填写凭据（仅一次，验证码错误不需要重新填）
        try:
            await self._page.fill_credentials(self._username, self._password)
        except Exception as exc:
            logger.warning("填写登录表单失败（可能已重定向）：{}", exc)
            if await self._page.is_logged_in():
                logger.info("虽填写失败但已处于登录态，返回会话")
                return await self._session_provider.get_session()
            raise

        for attempt in range(1, self._max_attempts + 1):
            if attempt > 1:
                delay = random.uniform(2.0, 5.0)
                logger.info("登录第 {} 次尝试，等待 {:.1f}s", attempt, delay)
                await asyncio.sleep(delay)

            # 内层：验证码识别 → 提交 → 校验
            code: str | None = None
            for captcha_try in range(1, self._captcha_retries + 1):
                if captcha_try > 1:
                    # 识别失败 → 刷新验证码
                    logger.info("  刷新验证码（第 {} 次）", captcha_try)
                    try:
                        await self._page.refresh_captcha()
                        await asyncio.sleep(0.8)
                    except Exception as exc:
                        logger.warning("  刷新验证码失败: {}", exc)

                # pre-click：首次进页面时，随机点 N 次换图，降低命中「难识别」码的概率
                if captcha_try == 1 and self._pre_click_count > 0:
                    logger.debug("  pre-click: 随机刷新 {} 次验证码", self._pre_click_count)
                    for _ in range(self._pre_click_count):
                        try:
                            await self._page.refresh_captcha()
                            await asyncio.sleep(random.uniform(0.3, 0.7))
                        except Exception as exc:
                            logger.warning("  pre-click 失败: {}", exc)
                            break

                try:
                    image = await self._page.get_captcha_image()
                    code = self._captcha.recognize(image)
                    logger.debug("  验证码识别: {} (内层 {}/{})", code, captcha_try, self._captcha_retries)
                    break  # 识别成功，跳出内层
                except CaptchaError as exc:
                    logger.warning("  第 {} 次验证码识别失败: {}", captcha_try, exc)
                    if captcha_try >= self._captcha_retries:
                        logger.error("  验证码识别 {} 次全部失败，本尝试放弃", self._captcha_retries)
                        code = None

            if code is None:
                logger.warning("第 {} 次尝试无有效验证码，进入下一轮", attempt)
                continue

            await self._page.fill_captcha(code)
            await self._page.submit()

            if await self._page.is_logged_in():
                logger.success("登录成功（第 {} 次尝试）", attempt)
                return await self._session_provider.get_session()

            logger.warning("第 {} 次登录未通过（验证码错误或已过期）", attempt)

        # 所有尝试耗尽
        logger.error("登录失败：{} 次尝试均未通过，需要人工介入", self._max_attempts)
        raise CaptchaError(
            f"登录失败：{self._max_attempts} 次尝试均未通过验证码验证，请检查账号或手动登录"
        )
