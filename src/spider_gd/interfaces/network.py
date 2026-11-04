"""网络链路接口契约（链路2）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class DeviceInfo:
    vid_pid: str
    name: str = ""


@dataclass
class DiagnoseReport:
    conclusion: str
    findings: list[str] = field(default_factory=list)
    fix_actions: list[str] = field(default_factory=list)


@dataclass
class ValidateReport:
    public_ok: bool
    private_ok: bool
    latency_ms: int = -1
    routes_ok: bool = False

    @property
    def passed(self) -> bool:
        return self.public_ok and self.private_ok and self.routes_ok


class IUsbMonitor(Protocol):
    """U-Key USB 设备插拔监控。"""

    def watch(
        self,
        on_inserted: Callable[[str], None],
        on_removed: Callable[[str], None],
    ) -> None: ...

    def stop(self) -> None: ...

    def list_devices(self) -> set[str]: ...


class IRouteManager(Protocol):
    """双网卡路由配置（PowerShell 封装）。"""

    def configure(self, target_ips: list[str]) -> bool: ...

    def cleanup(self, target_ips: list[str]) -> bool: ...

    def is_admin(self) -> bool: ...


class INetworkValidator(Protocol):
    """双网络连通性验证。"""

    def validate(self, target_domain: str, target_ips: list[str]) -> ValidateReport: ...
