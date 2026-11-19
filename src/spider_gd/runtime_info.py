"""当前进程的构建身份。健康检查和诊断包都读这里，避免只看端口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def runtime_identity() -> dict[str, object]:
    """返回正在跑的这份后端自己的版本、路径和进程号。

    Returns:
        dict[str, object]: ``executable`` 是实际解释器或冻结 exe 的路径。

    Examples:
        >>> runtime_identity()["pid"] > 0
        True
    """
    try:
        from spider_gd import __version__ as package_version
    except Exception:
        package_version = ""
    try:
        from spider_gd.build_stamp import BUILD_ID

        stamp = str(BUILD_ID or "")
    except Exception:
        stamp = ""
    executable = Path(sys.executable).resolve()
    return {
        "spider_gd": str(package_version or ""),
        "build_id": stamp,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": str(executable),
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
    }
