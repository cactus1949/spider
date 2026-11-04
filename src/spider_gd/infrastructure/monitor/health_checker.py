"""统一健康检查：聚合各链路检查项，输出整体健康。

实现 :class:`spider_gd.interfaces.monitor.IHealthChecker`。
检查项以 ``(name, async fn -> HealthReport)`` 形式注入，便于扩展与测试。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime

from loguru import logger

from spider_gd.interfaces.monitor import HealthLevel, HealthReport

CheckFn = Callable[[], Awaitable[HealthReport]]

_LEVEL_ORDER = {
    HealthLevel.OK: 0,
    HealthLevel.WARN: 1,
    HealthLevel.ERROR: 2,
    HealthLevel.CRITICAL: 3,
}


def _now_iso() -> str:
    return datetime.now().isoformat()


def disk_space_check(path: str = ".", min_free_gb: float = 1.0) -> CheckFn:
    """构造磁盘空间检查项。"""

    async def _check() -> HealthReport:
        free_gb = shutil.disk_usage(path).free / (1024**3)
        level = HealthLevel.OK if free_gb >= min_free_gb else HealthLevel.CRITICAL
        return HealthReport(
            module="disk",
            level=level,
            message=f"剩余磁盘 {free_gb:.1f}GB",
            timestamp=_now_iso(),
            metadata={"free_gb": round(free_gb, 2)},
        )

    return _check


def ukey_check(vid_pid: str = "auto") -> CheckFn:
    """构造 U-Key 接入状态检查项。

    Args:
        vid_pid: U-Key 的 VID:PID，如 ``"1234:5678"``；``"auto"`` 表示自动识别
                 （扫描 WMI 中的 SmartCard / 安全密钥类设备）。
    """

    # 常见的 U-Key / 智能卡设备特征
    _UKEY_PATTERNS = [
        "smartcard", "smart card", "ccid", "usb key", "token",
        "飞天", "握奇", "华大", "海泰", "中孚", "吉大", "恒宝",
        "feitian", "watchdata", "huada", "haitai",
        "hid",  # HID 标准设备（多数 U-Key 采用此接口）
    ]
    # 已知 U-Key 厂商 VID（自动识别无需配置 vid_pid）
    _UKEY_VIDS = {
        "096E",  # Feitian Technologies
        "2CE3",  # Watchdata
        "1A86",  # QinHeng (CH552/CH554)
        "24DC",  # ARDS
    }

    async def _check() -> HealthReport:
        try:
            import wmi
            conn = wmi.WMI()
            all_usb: list[dict] = []
            matched: list[dict] = []

            for ent in conn.Win32_PnPEntity(ConfigManagerErrorCode=0):
                did = (ent.DeviceID or "").upper()
                if "VID_" not in did or "PID_" not in did:
                    continue
                vid = did.split("VID_")[1][:4]
                pid = did.split("PID_")[1][:4]
                device_vp = f"{vid}:{pid}"
                desc = (ent.Description or "").lower()
                name = (ent.Name or "").lower()
                pnp = (ent.PNPClass or "").lower()
                combined = f"{desc} {name} {pnp}"

                info = {"vid_pid": device_vp, "desc": ent.Description or "",
                        "name": ent.Name or "", "class": ent.PNPClass or ""}
                all_usb.append(info)

                # 指定 VID:PID 精确匹配
                if vid_pid and vid_pid != "auto" and device_vp == vid_pid.upper():
                    matched.append(info)
                    continue

                # auto 模式：匹配 U-Key 特征
                if vid_pid == "auto":
                    # 1) 已知 U-Key 厂商 VID
                    if vid in _UKEY_VIDS:
                        matched.append(info)
                        continue
                    # 2) 设备描述/名称/类名匹配关键词
                    if any(p in combined for p in _UKEY_PATTERNS):
                        matched.append(info)
                        continue
                    # 3) 也检查 PNPClass
                    if "smartcardreader" in pnp:
                        matched.append(info)
                        continue
                    # 4) DeviceID 以 HID\ 开头（HID 接口 U-Key），排除常见外设
                    if did.startswith("HID\\"):
                        # 排除键盘/鼠标/触控板/手柄
                        _peripheral = {"KEYBOARD", "MOUSE", "TOUCHPAD", "GAMEPAD", "JOYSTICK"}
                        if not any(p in combined for p in _peripheral):
                            matched.append(info)
                            continue

            # 有匹配的设备
            if matched:
                device_str = ", ".join(m["vid_pid"] for m in matched)
                return HealthReport(
                    module="ukey",
                    level=HealthLevel.OK,
                    message=f"U-Key 已连接（{device_str}）",
                    timestamp=_now_iso(),
                    metadata={"devices": matched},
                )

            # 没找到匹配
            if vid_pid and vid_pid != "auto":
                # 指定了 VID:PID 但没找到
                return HealthReport(
                    module="ukey",
                    level=HealthLevel.CRITICAL,
                    message=f"未检测到 U-Key（VID:PID={vid_pid}）",
                    timestamp=_now_iso(),
                )
            else:
                # auto 模式：有 USB 设备但都不是 U-Key
                if all_usb:
                    devices_str = ", ".join(d["vid_pid"] for d in all_usb[:5])
                    return HealthReport(
                        module="ukey",
                        level=HealthLevel.WARN,
                        message=f"未检测到 U-Key（当前 USB 设备：{devices_str}）",
                        timestamp=_now_iso(),
                    )
                else:
                    return HealthReport(
                        module="ukey",
                        level=HealthLevel.CRITICAL,
                        message="未检测到任何 USB 设备",
                        timestamp=_now_iso(),
                    )

        except ImportError:
            return HealthReport(
                module="ukey",
                level=HealthLevel.WARN,
                message="U-Key 检测不可用（非 Windows 或 WMI 未安装）",
                timestamp=_now_iso(),
            )
        except Exception as exc:
            logger.warning("U-Key 健康检查异常：{}", exc)
            return HealthReport(
                module="ukey",
                level=HealthLevel.WARN,
                message=f"U-Key 检测异常：{exc}",
                timestamp=_now_iso(),
            )

    return _check


def network_check(target_url: str = "https://pm.gd.csg.cn", timeout: float = 5.0,
                  module_name: str = "network") -> CheckFn:
    """构造网络连通性检查项。

    Args:
        target_url: 目标检测 URL（默认广东电力交易平台）
        timeout: 请求超时秒数
    """

    async def _check() -> HealthReport:
        try:
            import httpx
            import logging
            # 降低 httpx 日志级别，避免 412 等非 200 响应的 INFO 噪音
            logging.getLogger("httpx").setLevel(logging.WARNING)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
                resp = await cli.get(target_url)
            # 能连上即算 OK（包括 412/403 等，说明网络通）
            return HealthReport(
                module=module_name,
                level=HealthLevel.OK,
                message=f"网络正常（{target_url} HTTP {resp.status_code}）",
                timestamp=_now_iso(),
            )
        except Exception as exc:
            msg = str(exc) or repr(exc)
            if not msg:
                msg = type(exc).__name__
            if "timeout" in msg.lower() or "timed out" in msg.lower():
                return HealthReport(
                    module=module_name,
                    level=HealthLevel.CRITICAL,
                    message=f"网络超时（{target_url}）",
                    timestamp=_now_iso(),
                )
            elif "name or service not known" in msg.lower() or "getaddrinfo" in msg.lower():
                return HealthReport(
                    module=module_name,
                    level=HealthLevel.CRITICAL,
                    message=f"DNS 解析失败（{target_url}）",
                    timestamp=_now_iso(),
                )
            else:
                return HealthReport(
                    module=module_name,
                    level=HealthLevel.ERROR,
                    message=f"网络异常：{msg[:60]}",
                    timestamp=_now_iso(),
                )

    return _check


class HealthChecker:
    def __init__(self) -> None:
        self._checks: list[tuple[str, CheckFn]] = []

    def add_check(self, name: str, fn: CheckFn) -> None:
        self._checks.append((name, fn))

    @staticmethod
    def overall_from(reports: list[HealthReport]) -> HealthReport:
        """从已有检查结果中计算综合状态。"""
        if not reports:
            return HealthReport("system", HealthLevel.OK, "无检查项", _now_iso())
        worst = max(reports, key=lambda r: _LEVEL_ORDER[r.level])
        return HealthReport(
            module="system",
            level=worst.level,
            message=f"检测到 {len(reports)} 项，最高告警级别为 {worst.level.value}: {worst.message[:60]}",
            timestamp=_now_iso(),
        )

    async def check_all(self) -> list[HealthReport]:
        """并发执行所有健康检查。"""
        async def _safe(fn: CheckFn, name: str) -> HealthReport:
            try:
                return await fn()
            except Exception as exc:
                logger.exception("健康检查 {} 异常：{}", name, exc)
                return HealthReport(
                    module=name,
                    level=HealthLevel.ERROR,
                    message=f"检查异常：{exc}",
                    timestamp=_now_iso(),
                )

        tasks = [_safe(fn, name) for name, fn in self._checks]
        return list(await asyncio.gather(*tasks))

    async def get_overall_health(self) -> HealthReport:
        reports = await self.check_all()
        if not reports:
            return HealthReport("system", HealthLevel.OK, "无检查项", _now_iso())
        worst = max(reports, key=lambda r: _LEVEL_ORDER[r.level])
        return HealthReport(
            module="system",
            level=worst.level,
            message=f"整体 {worst.level.value}（{len(reports)} 项检查）",
            timestamp=_now_iso(),
            metadata={"checks": [r.module for r in reports]},
        )
