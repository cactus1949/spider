"""网络诊断：U-Key 插入前后路由/适配器/DNS 快照对比，自动推断断网根因。

核心原则：不假设底层机制，仅按前后 diff 推断并给出修复动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from spider_gd.infrastructure.network._powershell import run_ps_json
from spider_gd.interfaces.network import DiagnoseReport


@dataclass
class NetSnapshot:
    """某一时刻的网络快照（默认路由 / 适配器 / DNS）。"""

    timestamp: str
    default_routes: list[dict[str, Any]] = field(default_factory=list)
    adapters: list[dict[str, Any]] = field(default_factory=list)
    dns_servers: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def take(cls) -> NetSnapshot:
        return cls(
            timestamp=datetime.now().isoformat(),
            default_routes=run_ps_json(
                'Get-NetRoute | Where-Object {$_.DestinationPrefix -eq "0.0.0.0/0"} '
                "| Select-Object DestinationPrefix,NextHop,InterfaceAlias,RouteMetric,ifIndex"
            ),
            adapters=run_ps_json(
                "Get-NetAdapter | Select-Object Name,Status,InterfaceMetric,ifIndex"
            ),
            dns_servers=run_ps_json(
                "Get-DnsClientServerAddress -AddressFamily IPv4 "
                '| Select-Object InterfaceAlias,@{n="Servers";e={$_.ServerAddresses -join ","}}'
            ),
        )


def diagnose(
    before: NetSnapshot,
    after: NetSnapshot,
    wifi_iface: str = "Wi-Fi",
) -> DiagnoseReport:
    """对比前后快照，推断断网根因并给出修复动作。"""
    report = DiagnoseReport(conclusion="")

    before_keys = {(r.get("InterfaceAlias"), r.get("NextHop")) for r in before.default_routes}
    new_default = [
        r
        for r in after.default_routes
        if (r.get("InterfaceAlias"), r.get("NextHop")) not in before_keys
    ]
    wifi_new_default = [r for r in new_default if r.get("InterfaceAlias") == wifi_iface]

    if wifi_new_default:
        report.findings.append(f"WiFi 接口新增默认路由：{wifi_new_default}")
        report.conclusion = "WiFi 默认网关抢占"
        report.fix_actions.append("删除 WiFi 默认路由")
    else:
        before_map = {r["InterfaceAlias"]: r["RouteMetric"] for r in before.default_routes}
        after_map = {r["InterfaceAlias"]: r["RouteMetric"] for r in after.default_routes}
        for iface, m_after in after_map.items():
            m_before = before_map.get(iface)
            if m_before is not None and m_after > m_before:
                report.findings.append(f"{iface} 默认路由 metric {m_before} → {m_after}")
                report.conclusion = "metric 被 U-Key 抢占"
                report.fix_actions.append(f"还原 {iface} metric 至 {m_before}")
                break
        if not report.conclusion:
            report.conclusion = "未检测到明显抢占，仅需追加专网 /32 路由"

    report.fix_actions.append("添加专网 /32 精确路由（统一执行）")
    logger.info("诊断结论：{}", report.conclusion)
    return report
