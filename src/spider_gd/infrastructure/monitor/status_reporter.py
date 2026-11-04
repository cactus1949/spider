"""状态汇总：把健康报告转为 /api/health 响应结构。"""

from __future__ import annotations

from typing import Any

from spider_gd.interfaces.monitor import HealthLevel, HealthReport

_OVERALL_MAP = {
    HealthLevel.OK: "healthy",
    HealthLevel.WARN: "degraded",
    HealthLevel.ERROR: "degraded",
    HealthLevel.CRITICAL: "critical",
}


def build_status(overall: HealthReport, checks: list[HealthReport]) -> dict[str, Any]:
    """构造 ``/api/health`` 返回体。"""
    return {
        "overall": _OVERALL_MAP[overall.level],
        "checks": [
            {
                "module": r.module,
                "status": r.level.value,
                "message": r.message,
                "timestamp": r.timestamp,
            }
            for r in checks
        ],
    }
