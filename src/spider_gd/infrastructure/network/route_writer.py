"""路由表配置（PowerShell 封装 + 管理员权限检查）。

实现 :class:`spider_gd.interfaces.network.IRouteManager`。
所有 subprocess 调用集中在本模块，便于 Mock 与命令拼接断言。
"""

from __future__ import annotations

import ctypes
import subprocess

from loguru import logger

from spider_gd.infrastructure.proc import run_capture


def is_admin() -> bool:
    """是否以管理员身份运行。"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined, unused-ignore]
    except Exception:
        return False


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return run_capture(args, timeout=timeout)


class RouteWriter:
    """双网卡路由配置。"""

    def __init__(self, wifi_iface: str = "Wi-Fi", wan_iface: str = "以太网") -> None:
        self.wifi_iface = wifi_iface
        self.wan_iface = wan_iface
        self.last_gateway = ""

    def is_admin(self) -> bool:
        return is_admin()

    def _get_wifi_gateway(self) -> str:
        result = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f'(Get-NetIPConfiguration -InterfaceAlias "{self.wifi_iface}")'
                ".IPv4DefaultGateway.NextHop",
            ]
        )
        gw = result.stdout.strip()
        if not gw:
            raise RuntimeError("WiFi 网关获取失败")
        return gw

    def _remove_host_routes(self, ips: list[str]) -> None:
        for ip in ips:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                f'Get-NetRoute -DestinationPrefix "{ip}/32" '
                "-ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false",
            ]
            _run(cmd)
            logger.info("删除旧专网路由 {}/32（如存在）", ip)

    def configure(self, target_ips: list[str], *, stale_ips: list[str] | None = None) -> bool:
        """删除 WiFi 默认路由 + 清理旧 /32 + 写入当前专网 IP 的 /32 精确路由。"""
        if not self.is_admin():
            raise PermissionError("路由配置需要管理员权限")

        gw = self._get_wifi_gateway()
        self.last_gateway = gw
        logger.info("WiFi 网关 = {}", gw)

        # 删除 WiFi 默认路由（防止抢占外网）
        del_cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f'Get-NetRoute -InterfaceAlias "{self.wifi_iface}" '
            '-DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue '
            "| Remove-NetRoute -Confirm:$false",
        ]
        _run(del_cmd)
        logger.info("已删除 WiFi 默认路由（如存在）")

        cleanup_ips = sorted({*(stale_ips or []), *target_ips})
        if cleanup_ips:
            self._remove_host_routes(cleanup_ips)

        for ip in target_ips:
            add_cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                f'New-NetRoute -DestinationPrefix "{ip}/32" '
                f'-InterfaceAlias "{self.wifi_iface}" -NextHop {gw} '
                "-RouteMetric 1 -PolicyStore ActiveStore",
            ]
            result = _run(add_cmd)
            if result.returncode != 0:
                logger.error("添加专网路由失败 {}/32: {}", ip, result.stderr)
                return False
            logger.success("添加专网路由 {}/32 via {}", ip, gw)
        return True

    def cleanup(self, target_ips: list[str]) -> bool:
        """删除本工具添加的所有专网 /32 路由。"""
        for ip in target_ips:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                f'Get-NetRoute -DestinationPrefix "{ip}/32" '
                "-ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false",
            ]
            _run(cmd)
            logger.info("清理专网路由 {}/32", ip)
        return True
