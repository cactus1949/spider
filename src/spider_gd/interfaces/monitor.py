"""监控链路接口契约（链路6）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class HealthLevel(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class HealthReport:
    module: str
    level: HealthLevel
    message: str
    timestamp: str
    metadata: dict[str, object] = field(default_factory=dict)


class IHealthChecker(Protocol):
    """统一健康检查。"""

    async def check_all(self) -> list[HealthReport]: ...

    async def get_overall_health(self) -> HealthReport: ...
