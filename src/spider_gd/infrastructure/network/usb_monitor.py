"""U-Key USB 设备插拔监控（WMI 轮询，可 Mock）。

通过 VID:PID 匹配 U-Key；WMI 不可用时（如非 Windows / 测试）由注入的
``list_ids_fn`` 替代，保证可测试。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger


def _wmi_list_usb_ids() -> set[str]:
    """通过 WMI 列出当前连接的所有 USB 设备 ID（VID:PID 格式）。"""
    import wmi  # 延迟导入，非 Windows 环境不影响其余模块

    conn = wmi.WMI()
    ids: set[str] = set()
    for ent in conn.Win32_PnPEntity(ConfigManagerErrorCode=0):
        did = ent.DeviceID or ""
        if "VID_" in did and "PID_" in did:
            vid = did.split("VID_")[1][:4]
            pid = did.split("PID_")[1][:4]
            ids.add(f"{vid}:{pid}")
    return ids


class UsbMonitor:
    """实现 :class:`spider_gd.interfaces.network.IUsbMonitor`。"""

    def __init__(
        self,
        vid_pid: str = "auto",
        poll_interval: int = 3,
        list_ids_fn: Callable[[], set[str]] | None = None,
    ) -> None:
        self._vid_pid = vid_pid.upper() if vid_pid != "auto" else None
        self._poll_interval = poll_interval
        self._list_ids = list_ids_fn or _wmi_list_usb_ids
        self._last_devices: set[str] = set()
        self._running = False

    def list_devices(self) -> set[str]:
        return self._list_ids()

    def _is_ukey(self, vid_pid: str) -> bool:
        if self._vid_pid is None:
            return True  # auto 模式，下游进一步过滤
        return vid_pid == self._vid_pid

    def poll_once(self) -> tuple[set[str], set[str]]:
        """单次轮询，返回 (新增, 移除) 的 U-Key 设备集合。"""
        current = self._list_ids()
        added = {d for d in current - self._last_devices if self._is_ukey(d)}
        removed = {d for d in self._last_devices - current if self._is_ukey(d)}
        self._last_devices = current
        return added, removed

    def watch(
        self,
        on_inserted: Callable[[str], None],
        on_removed: Callable[[str], None],
    ) -> None:
        self._running = True
        self._last_devices = self._list_ids()
        logger.info("USB 监听启动，初始设备数={}", len(self._last_devices))
        while self._running:
            time.sleep(self._poll_interval)
            try:
                added, removed = self.poll_once()
                for d in added:
                    logger.success("U-Key 插入：{}", d)
                    on_inserted(d)
                for d in removed:
                    logger.warning("U-Key 拔出：{}", d)
                    on_removed(d)
            except Exception as exc:
                logger.exception("USB 轮询异常：{}", exc)

    def stop(self) -> None:
        self._running = False
