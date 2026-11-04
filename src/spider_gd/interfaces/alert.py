"""告警链路接口契约（链路4）—— 单向推送。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class AlertLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass
class AlertResult:
    sent: bool
    suppressed: bool = False
    error_msg: str | None = None


class IAlertGateway(Protocol):
    """告警网关：路由到配置的通道（钉钉/飞书/邮件）。"""

    async def send_alert(
        self, level: AlertLevel, message: str, event_type: str = "info"
    ) -> AlertResult: ...

    def is_in_silence_window(self) -> bool: ...
