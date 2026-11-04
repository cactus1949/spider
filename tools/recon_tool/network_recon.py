"""网络侦察：复用 spider_gd 网络核心，产出结构化 JSON。

不重写诊断逻辑，直接复用 ``spider_gd.infrastructure.network``。
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from loguru import logger


def collect_system_info() -> dict[str, Any]:
    """采集系统环境信息（纯 Python，无外部依赖）。"""
    return {
        "platform": platform.platform(),
        "python_version": sys.version,
        "machine": platform.machine(),
        "node": platform.node(),
    }


def run_network_recon(
    out_dir: Path | str,
    *,
    target_domain: str = "pm.gd.csg.cn",
    wifi_iface: str = "Wi-Fi",
    baseline_snapshot: Any | None = None,
) -> dict[str, Any]:
    """执行网络侦察（U-Key 已插入后调用），写出 JSON 并返回摘要。

    ``baseline_snapshot`` 为插入前快照（由调用方在插入前 ``NetSnapshot.take()``）。
    """
    from spider_gd.infrastructure.network.diagnoser import NetSnapshot, diagnose

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    before = baseline_snapshot or NetSnapshot.take()
    after = NetSnapshot.take()
    report = diagnose(before, after, wifi_iface=wifi_iface)

    summary = {
        "system_info": collect_system_info(),
        "route_before": asdict(before),
        "route_after": asdict(after),
        "diagnose": asdict(report),
        "target_domain": target_domain,
    }
    for name, payload in summary.items():
        (out / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    logger.success("网络侦察完成，结论：{}", report.conclusion)
    return summary
