"""U-Key 接入管理：插入检测 → 自登录，有效期记录与续期告警。

对应架构图"安全入口 U-Key/CA 认证"：
- 复用 :class:`UsbMonitor` 检测 U-Key 插拔（VID:PID 匹配，可注入假对象测试）。
- 插入时触发自登录回调（接 LoginFlow），实现"插入即用、自启动自登录"。
- 记录证书有效期，到期前 ``warn_days`` 天发出续期告警，过期发出严重告警。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime

from loguru import logger

from spider_gd.infrastructure.network.usb_monitor import UsbMonitor


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("U-Key 有效期格式非法（应为 YYYY-MM-DD）：{}", s)
        return None


class UKeyManager:
    def __init__(
        self,
        monitor: UsbMonitor,
        *,
        valid_until: str = "",
        warn_days: int = 30,
        alert_hook: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._monitor = monitor
        self._valid_until = _parse_date(valid_until)
        self._warn_days = warn_days
        self._alert_hook = alert_hook

    def is_present(self) -> bool:
        """当前是否插着 U-Key。"""
        return len(self._monitor.list_devices()) > 0

    def days_until_expiry(self, *, now: date | None = None) -> int | None:
        """距 U-Key 到期天数（负数表示已过期）；未配置有效期返回 None。"""
        if self._valid_until is None:
            return None
        now = now or date.today()
        return (self._valid_until - now).days

    async def check_expiry(self, *, now: date | None = None) -> int | None:
        """检查有效期，按需发出续期/过期告警。返回剩余天数。"""
        days = self.days_until_expiry(now=now)
        if days is None:
            return None
        if days < 0:
            logger.error("U-Key 已过期 {} 天", -days)
            await self._alert(f"U-Key 已过期 {-days} 天", "请立即更换 U-Key/CA 证书")
        elif days <= self._warn_days:
            logger.warning("U-Key 将在 {} 天后到期", days)
            await self._alert(f"U-Key 将在 {days} 天后到期", "请尽快办理续期")
        return days

    async def wait_and_login(
        self,
        login: Callable[[], Awaitable[None]],
        *,
        max_polls: int | None = None,
    ) -> bool:
        """轮询等待 U-Key 插入；插入后触发自登录。返回是否成功登录。

        ``max_polls`` 限制轮询次数（测试/单次检查用）；None 表示持续等待。
        """
        polls = 0
        while max_polls is None or polls < max_polls:
            added, _removed = self._monitor.poll_once()
            if added or self.is_present():
                logger.success("检测到 U-Key，触发自登录")
                await login()
                return True
            polls += 1
        logger.info("未检测到 U-Key（轮询 {} 次）", polls)
        return False

    async def _alert(self, title: str, message: str) -> None:
        if self._alert_hook is not None:
            await self._alert_hook(title, message)
