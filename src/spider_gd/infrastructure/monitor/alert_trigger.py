"""告警触发器：根据健康报告调用告警网关，带去重（同一告警不重复发送）。"""

from __future__ import annotations

from loguru import logger

from spider_gd.interfaces.alert import AlertLevel, IAlertGateway
from spider_gd.interfaces.monitor import HealthLevel, HealthReport

_LEVEL_MAP = {
    HealthLevel.WARN: AlertLevel.WARN,
    HealthLevel.ERROR: AlertLevel.ERROR,
    HealthLevel.CRITICAL: AlertLevel.ERROR,
}


class AlertTrigger:
    def __init__(self, alert_gateway: IAlertGateway) -> None:
        self._gateway = alert_gateway
        self._last_fingerprints: set[str] = set()

    async def process(self, reports: list[HealthReport]) -> int:
        """对非 OK 报告触发告警，去重后返回实际发送数量。"""
        current = {f"{r.module}:{r.level.value}" for r in reports if r.level != HealthLevel.OK}

        sent = 0
        for report in reports:
            if report.level == HealthLevel.OK:
                continue
            fingerprint = f"{report.module}:{report.level.value}"
            if fingerprint in self._last_fingerprints:
                logger.debug("告警去重，跳过：{}", fingerprint)
                continue
            await self._gateway.send_alert(
                _LEVEL_MAP[report.level],
                f"[{report.module}] {report.message}",
                event_type="health",
            )
            sent += 1

        # 已恢复（不再出现的指纹）清除，便于下次复发时重新告警
        self._last_fingerprints = current
        return sent
