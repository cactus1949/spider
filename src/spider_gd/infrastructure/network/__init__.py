"""网络自动配置核心（链路2）。

被现场侦察工具（tools/recon_tool）复用，不要重写。
"""

from __future__ import annotations

from spider_gd.infrastructure.network.diagnoser import NetSnapshot, diagnose
from spider_gd.infrastructure.network.dns_resolver import DnsResolver
from spider_gd.infrastructure.network.dual_nic_closure import (
    DualNicClosure,
    DualNicClosureReport,
    DualNicRuleResult,
)
from spider_gd.infrastructure.network.network_validator import NetworkValidator
from spider_gd.infrastructure.network.route_writer import RouteWriter
from spider_gd.infrastructure.network.supervisor import (
    LinkMode,
    NetworkSupervisor,
    SupervisorState,
)
from spider_gd.infrastructure.network.usb_monitor import UsbMonitor
from spider_gd.infrastructure.network.wifi_connector import (
    WifiConnectOptions,
    WifiConnectResult,
    WifiConnector,
    WifiNetwork,
    decode_ssid_name,
)

__all__ = [
    "DnsResolver",
    "DualNicClosure",
    "DualNicClosureReport",
    "DualNicRuleResult",
    "LinkMode",
    "NetSnapshot",
    "NetworkSupervisor",
    "NetworkValidator",
    "RouteWriter",
    "SupervisorState",
    "UsbMonitor",
    "WifiConnectOptions",
    "WifiConnectResult",
    "WifiConnector",
    "WifiNetwork",
    "decode_ssid_name",
    "diagnose",
]
