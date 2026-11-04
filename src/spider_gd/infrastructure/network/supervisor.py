"""双网卡连接监督：持续监控 + 自动重连 + 公网失败自动降级到离线模式 D。

单机双网卡场景（专网网卡接平台、公网网卡接告警/Agent）的底层运行机制：
- 周期性 :class:`INetworkValidator.validate` 检查专网/公网/路由。
- 专网不可达：触发重连回调（如重配 /32 路由），连续失败计数。
- 公网不可达：降级为离线模式 D（停用告警/Agent 等联网能力），恢复后回升。

纯逻辑可测：validator、reconnect、now/sleep 均可注入。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from loguru import logger

from spider_gd.interfaces.network import INetworkValidator, ValidateReport


class LinkMode(str, Enum):
    ONLINE = "online"  # 公网可用，全功能
    DEGRADED_OFFLINE = "degraded_offline"  # 公网不可用，降级离线（模式 D）


@dataclass
class SupervisorState:
    mode: LinkMode = LinkMode.ONLINE
    private_fail_streak: int = 0
    public_fail_streak: int = 0
    last_report: ValidateReport | None = None


class NetworkSupervisor:
    def __init__(
        self,
        validator: INetworkValidator,
        *,
        target_domain: str,
        target_ips: list[str] | None = None,
        reconnect: Callable[[], Awaitable[bool]] | None = None,
        on_mode_change: Callable[[LinkMode], Awaitable[None]] | None = None,
        reconnect_after_failures: int = 2,
        public_degrade_after: int = 2,
    ) -> None:
        self._validator = validator
        self._domain = target_domain
        self._ips = target_ips or []
        self._reconnect = reconnect
        self._on_mode_change = on_mode_change
        self._reconnect_after = reconnect_after_failures
        self._public_degrade_after = public_degrade_after
        self.state = SupervisorState()

    async def check_once(self) -> SupervisorState:
        """单次连通性检查 + 必要的重连/降级动作。返回最新状态。"""
        report = self._validator.validate(self._domain, self._ips)
        self.state.last_report = report

        # 专网监控 + 自动重连
        if report.private_ok:
            self.state.private_fail_streak = 0
        else:
            self.state.private_fail_streak += 1
            logger.warning("专网不可达（连续 {} 次）", self.state.private_fail_streak)
            if self._reconnect and self.state.private_fail_streak >= self._reconnect_after:
                logger.info("触发专网自动重连")
                ok = await self._reconnect()
                if ok:
                    self.state.private_fail_streak = 0

        # 公网监控 + 自动降级/回升
        if report.public_ok:
            if self.state.public_fail_streak > 0:
                logger.info("公网恢复（此前连续失败 {} 次）", self.state.public_fail_streak)
            self.state.public_fail_streak = 0
            await self._set_mode(LinkMode.ONLINE)
        else:
            self.state.public_fail_streak += 1
            if self.state.public_fail_streak >= self._public_degrade_after:
                logger.warning(
                    "公网连续不可达 {} 次（阈值 {}），降级离线模式",
                    self.state.public_fail_streak,
                    self._public_degrade_after,
                )
                await self._set_mode(LinkMode.DEGRADED_OFFLINE)
            else:
                logger.info(
                    "公网不可达（连续 {} 次，未达降级阈值 {}）",
                    self.state.public_fail_streak,
                    self._public_degrade_after,
                )

        return self.state

    async def _set_mode(self, mode: LinkMode) -> None:
        if mode != self.state.mode:
            logger.info("链路模式切换：{} → {}", self.state.mode.value, mode.value)
            self.state.mode = mode
            if self._on_mode_change is not None:
                await self._on_mode_change(mode)
