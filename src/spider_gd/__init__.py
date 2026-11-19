"""广东电力交易数据智能采集系统。"""

from __future__ import annotations

import sys
from pathlib import Path


def parse_version_text(text: str) -> str:
    """取出 VERSION 文件里的版本号。

    Args:
        text: 文件原文，允许首尾空白。

    Returns:
        str: 去掉空白后的版本号。

    Raises:
        ValueError: 内容是空的。

    Examples:
        >>> parse_version_text(" 0.2.8\\n")
        '0.2.8'
    """
    value = text.strip()
    if not value:
        raise ValueError("VERSION 是空的")
    return value


def read_version() -> str:
    """从仓库根目录或冻结包里读取版本号。

    Returns:
        str: ``VERSION`` 文件中的版本。找不到文件时为 ``0.0.0``。

    Examples:
        >>> read_version().count(".") == 2 or read_version() == "0.0.0"
        True
    """
    candidates = [Path(__file__).resolve().parents[2] / "VERSION"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(getattr(sys, "_MEIPASS", "")) / "VERSION")
    for path in candidates:
        if path.is_file():
            return parse_version_text(path.read_text(encoding="utf-8"))
    return "0.0.0"


__version__ = read_version()
