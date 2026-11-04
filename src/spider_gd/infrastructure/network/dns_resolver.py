"""专网域名解析：必须走 WiFi 网卡 DNS（公网 DNS 解析不了专网域名）。

三级回退：① WiFi DNS 解析 → ② ping 兜底（系统已缓存）→ ③ 历史缓存 IP。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml
from loguru import logger

from spider_gd.infrastructure.proc import ensure_text

_IP_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")


def _unique_ips(ips: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ip in ips:
        if ip and ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


def get_wifi_dns(interface_name: str = "Wi-Fi") -> list[str]:
    """获取 WiFi 网卡被 DHCP 分配的 DNS 服务器地址（按顺序）。"""
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f'(Get-DnsClientServerAddress -InterfaceAlias "{interface_name}" '
            f"-AddressFamily IPv4).ServerAddresses",
        ],
        capture_output=True,
        timeout=5,
    )
    stdout = ensure_text(result.stdout)
    return [ip.strip() for ip in stdout.strip().splitlines() if ip.strip()]


def resolve_via_dns(domain: str, dns_server: str, timeout: int = 10) -> list[str]:
    """通过指定 DNS 服务器调用 nslookup 解析域名（排除 DNS 服务器自身地址）。"""
    result = subprocess.run(
        ["nslookup", domain, dns_server],
        capture_output=True,
        timeout=timeout,
    )
    ips = re.findall(r"Address:\s*(\d+\.\d+\.\d+\.\d+)", ensure_text(result.stdout))
    return _unique_ips([ip for ip in ips if ip != dns_server])


class DnsResolver:
    """专网域名解析器（实时走 WiFi DNS + 三级回退 + 缓存）。"""

    def __init__(self, domain: str, cache_path: Path | str, wifi_iface: str = "Wi-Fi") -> None:
        self.domain = domain
        self.cache_path = Path(cache_path)
        self.wifi_iface = wifi_iface
        self._cache = self._load_cache()

    def _load_cache(self) -> list[str]:
        if self.cache_path.exists():
            data = yaml.safe_load(self.cache_path.read_text(encoding="utf-8")) or {}
            return data.get("cached_ips", [])
        return []

    def _save_cache(self, ips: list[str]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            yaml.safe_dump({"cached_ips": ips}, allow_unicode=True),
            encoding="utf-8",
        )

    @property
    def cached_ips(self) -> list[str]:
        """返回上次成功解析的 IP，用于动态 IP 变化时清理旧路由。"""
        return list(self._cache)

    def resolve(self) -> list[str]:
        # ① WiFi 网卡 DNS 解析
        for dns in get_wifi_dns(self.wifi_iface):
            try:
                ips = resolve_via_dns(self.domain, dns)
            except Exception as exc:
                logger.warning("DNS({}) 解析失败：{}", dns, exc)
                continue
            if ips:
                ips = _unique_ips(ips)
                logger.success("DNS({}) {} -> {}", dns, self.domain, ips)
                self._cache = ips
                self._save_cache(ips)
                return ips

        # ② ping 兜底
        try:
            result = subprocess.run(
                ["ping", "-n", "1", "-w", "2000", self.domain],
                capture_output=True,
                timeout=5,
            )
            m = re.search(r"\[(\d+\.\d+\.\d+\.\d+)\]", ensure_text(result.stdout))
            if m:
                ip = m.group(1)
                logger.warning("DNS 解析失败，ping 兜底得到 IP：{}", ip)
                self._cache = [ip]
                self._save_cache(self._cache)
                return [ip]
        except Exception as exc:
            logger.warning("ping 兜底失败：{}", exc)

        # ③ 历史缓存
        if self._cache:
            logger.warning("全部解析失败，使用历史缓存 {}", self._cache)
            return self._cache

        raise RuntimeError(f"无法解析 {self.domain}（WiFi DNS / ping / 缓存均失败）")
