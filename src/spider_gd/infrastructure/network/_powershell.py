"""PowerShell 调用封装 —— 网络模块统一的子进程入口（单一 Mock 点）。"""

from __future__ import annotations

import json
from typing import Any

from spider_gd.infrastructure.proc import run_capture


def run_ps(command: str, timeout: int = 15) -> str:
    """执行 PowerShell 命令，返回 stdout（已 strip）。"""
    result = run_capture(
        ["powershell", "-NoProfile", "-Command", command],
        timeout=timeout,
    )
    return result.stdout.strip()


def run_ps_json(command: str, timeout: int = 15) -> list[dict[str, Any]]:
    """执行 PowerShell 命令并解析 ``ConvertTo-Json`` 输出为 list[dict]。"""
    out = run_ps(f"{command} | ConvertTo-Json -Compress", timeout=timeout)
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]
