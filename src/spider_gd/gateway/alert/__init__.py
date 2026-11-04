"""告警网关（链路4）—— 单向推送。"""

from __future__ import annotations

from spider_gd.gateway.alert.alert_gateway import AlertGateway, NullAlertGateway
from spider_gd.gateway.alert.webhook_sender import build_dingtalk_signed_url, send_dingtalk

__all__ = [
    "AlertGateway",
    "NullAlertGateway",
    "build_dingtalk_signed_url",
    "send_dingtalk",
]
