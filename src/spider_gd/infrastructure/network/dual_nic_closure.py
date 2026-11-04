"""双网卡闭环：动态解析专网域名、写入精确路由、验证公网仍可用。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from loguru import logger

from spider_gd.infrastructure.network.dns_resolver import DnsResolver
from spider_gd.infrastructure.network.network_validator import NetworkValidator
from spider_gd.infrastructure.network.route_writer import RouteWriter
from spider_gd.infrastructure.network.wifi_connector import decode_ssid_name
from spider_gd.infrastructure.proc import run_capture
from spider_gd.interfaces.network import ValidateReport


class _Resolver(Protocol):
    @property
    def cached_ips(self) -> list[str]: ...
    def resolve(self) -> list[str]: ...


class _RouteWriter(Protocol):
    last_gateway: str
    def configure(self, target_ips: list[str], *, stale_ips: list[str] | None = None) -> bool: ...


class _Validator(Protocol):
    def validate(
        self,
        target_domain: str,
        target_ips: list[str],
        *,
        private_iface: str = "",
        public_probe_host: str = "www.baidu.com",
        public_probe_port: int = 443,
    ) -> ValidateReport: ...


@dataclass
class DualNicRuleResult:
    domain: str
    interface_name: str
    status: str
    expected_ssid: str = ""
    current_ssid: str = ""
    resolved_ips: list[str] = field(default_factory=list)
    stale_ips: list[str] = field(default_factory=list)
    gateway: str = ""
    private_ok: bool = False
    public_ok: bool = False
    routes_ok: bool = False
    details: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.status in {"ok", "degraded"} and self.private_ok and self.routes_ok

    @property
    def public_degraded(self) -> bool:
        return self.private_ok and self.routes_ok and not self.public_ok


@dataclass
class DualNicClosureReport:
    status: str
    applied: int
    results: list[DualNicRuleResult]
    details: list[str]
    message: str = ""


def _safe_cache_name(domain: str, interface_name: str) -> str:
    raw = f"{domain}_{interface_name}".strip().lower()
    return re.sub(r"[^a-z0-9_.-]+", "_", raw) or "private_domain"


def get_connected_ssid(interface_name: str) -> str:
    """读取指定 WiFi 接口当前连接的 SSID（Windows netsh）。"""
    result = run_capture(["netsh", "wlan", "show", "interfaces"], timeout=8)
    current_name = ""
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = [part.strip() for part in line.split(":", 1)]
        key_l = key.lower()
        if key_l in ("name", "名称"):
            current_name = value
        elif key_l == "ssid" and current_name == interface_name:
            return decode_ssid_name(value)
    return ""


class DualNicClosure:
    """运行一次完整双网卡闭环。

    现场 IP 可能随 U-Key/WiFi 拔插变化，因此每次应用规则都重新走指定网卡 DNS
    解析域名，清理旧 /32 路由，写入当前 IP 的 /32 路由，再验证专网和公网。
    """

    def __init__(
        self,
        *,
        cache_dir: Path | str,
        resolver_factory: type[DnsResolver] | None = None,
        route_writer_factory: type[RouteWriter] | None = None,
        validator: _Validator | None = None,
        public_probe_host: str = "www.baidu.com",
        public_probe_port: int = 443,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.resolver_factory = resolver_factory or DnsResolver
        self.route_writer_factory = route_writer_factory or RouteWriter
        self.validator = validator or NetworkValidator()
        self.public_probe_host = public_probe_host
        self.public_probe_port = public_probe_port

    def apply_rules(self, rules: list[dict]) -> DualNicClosureReport:
        enabled = [
            r for r in rules
            if r.get("enabled", True)
            and str(r.get("domain_pattern", "")).strip()
            and str(r.get("interface_name", "")).strip()
        ]
        if not enabled:
            return DualNicClosureReport(
                status="ok",
                applied=0,
                results=[],
                details=["没有启用的专网路由规则"],
                message="没有启用的专网路由规则",
            )

        results: list[DualNicRuleResult] = []
        applied = 0
        for rule in enabled:
            domain = str(rule["domain_pattern"]).strip()
            interface_name = str(rule["interface_name"]).strip()
            expected_ssid = str(rule.get("expected_ssid", "") or rule.get("ssid", "")).strip()
            result = self._apply_one(domain, interface_name, expected_ssid)
            results.append(result)
            if result.status == "ok":
                applied += len(result.resolved_ips)

        details: list[str] = []
        for result in results:
            details.extend(result.details)
            if result.error:
                details.append(f"ERR {result.domain}: {result.error}")

        if results and all(r.passed for r in results):
            status = "degraded" if any(r.public_degraded for r in results) else "ok"
        else:
            status = "error"
        if status == "ok":
            message = "双网卡闭环验证通过"
        elif status == "degraded":
            message = "专网闭环通过，公网不可达；允许采集落本地，告警/上传稍后补偿"
        else:
            message = "专网闭环未通过，请查看解析、路由和专网验证结果"
        return DualNicClosureReport(
            status=status,
            applied=applied,
            results=results,
            details=details,
            message=message,
        )

    def _apply_one(self, domain: str, interface_name: str, expected_ssid: str = "") -> DualNicRuleResult:
        # 规则里可能存着 netsh 的十六进制 SSID，比较前先还原成真实名称。
        expected_ssid = decode_ssid_name(expected_ssid)
        cache_path = self.cache_dir / f"{_safe_cache_name(domain, interface_name)}.yaml"
        result = DualNicRuleResult(
            domain=domain,
            interface_name=interface_name,
            expected_ssid=expected_ssid,
            status="error",
        )
        try:
            if expected_ssid:
                result.current_ssid = get_connected_ssid(interface_name)
                if not result.current_ssid:
                    raise RuntimeError(f"无法确认 {interface_name} 当前连接的 WiFi SSID")
                if result.current_ssid != expected_ssid:
                    raise RuntimeError(
                        f"{interface_name} 当前连接 WiFi 为 {result.current_ssid}，期望 {expected_ssid}"
                    )
                result.details.append(f"OK WiFi SSID: {result.current_ssid}")

            resolver = self.resolver_factory(domain, cache_path, wifi_iface=interface_name)
            result.stale_ips = resolver.cached_ips
            ips = resolver.resolve()
            if not ips:
                raise RuntimeError(f"无法解析 {domain}")
            result.resolved_ips = ips
            result.details.append(f"OK 解析 {domain} via {interface_name}: {', '.join(ips)}")

            writer = self.route_writer_factory(wifi_iface=interface_name)
            if not writer.configure(ips, stale_ips=result.stale_ips):
                raise RuntimeError("路由写入失败")
            result.gateway = getattr(writer, "last_gateway", "")
            gw_text = f" 网关 {result.gateway}" if result.gateway else ""
            result.details.append(f"OK 写入 /32 路由 via {interface_name}{gw_text}")

            report = self.validator.validate(
                domain,
                ips,
                private_iface=interface_name,
                public_probe_host=self.public_probe_host,
                public_probe_port=self.public_probe_port,
            )
            result.private_ok = report.private_ok
            result.public_ok = report.public_ok
            result.routes_ok = report.routes_ok
            if report.private_ok and report.routes_ok:
                result.status = "ok" if report.public_ok else "degraded"
            else:
                result.status = "error"
            result.details.append(
                "OK 验证"
                if result.status == "ok"
                else "WARN 专网验证通过，公网不可达，继续本地采集"
                if result.status == "degraded"
                else (
                    "ERR 验证失败 "
                    f"专网={report.private_ok} 公网={report.public_ok} 路由={report.routes_ok}"
                )
            )
        except Exception as exc:
            result.error = str(exc)
            logger.warning("双网卡闭环失败 domain={} iface={}: {}", domain, interface_name, exc)
        return result
