"""子进程输出捕获与解码（Windows 控制台工具专用）。

背景
----
``dev-backend.cmd`` 用 ``set "PYTHONUTF8=1"`` 启动后端（Python UTF-8 模式）。
此时 ``subprocess.run(..., text=True)`` 会用 **UTF-8** 去解码子进程输出，而
``netsh`` / ``powershell`` / ``wmic`` / ``ping`` / ``nslookup`` 这些 Windows 控制台
程序在中文系统上输出的是 **cp936(GBK)** 字节。解码失败发生在 subprocess 内部的
读线程里，异常被吞掉，``result.stdout`` 直接变成 ``None``，调用方只能看到
“命令未返回输出”这类假故障。

现象对照：``PYTHONUTF8=1`` + 无控制台 + ``text=True`` 调用 netsh 时，
``returncode=0``、``stdout=None``、``stderr=''``；同一进程改用二进制捕获后
``stdout=b'\\xcf\\xb5\\xcd\\xb3...'``（“系统上有 1 个接口”）完全正常。

做法
----
捕获时用二进制模式，再按 BOM → **控制台代码页** → UTF-8 → 替换字符的顺序
解码，保证 ``stdout`` / ``stderr`` 永远是 ``str``（不会是 ``None``）。

为什么控制台代码页要排在 UTF-8 前面：GBK 字节不保证“解不出 UTF-8”。例如
“状态”的 GBK 字节 ``d7 b4 cc ac`` 恰好是合法 UTF-8，会解成 ``״̬``；
先试 UTF-8 就会把中文 WiFi 名显示成乱码。反过来，真正的 UTF-8 字节用 cp936
解通常会失败（或出现典型乱码），因此以 Windows 控制台实际代码页为准最稳。
"""

from __future__ import annotations

import codecs
import subprocess
import sys
from collections.abc import Sequence

__all__ = [
    "decode_console_bytes",
    "ensure_text",
    "run_capture",
]

#: 带 BOM 的输出：先按 BOM 判断，避免误用代码页解码。
_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _windows_console_codepage() -> int:
    """读取 Windows 控制台输出代码页；失败时回退到系统 OEM 代码页。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes  # 仅 Windows 需要，避免影响其他平台

        kernel32 = ctypes.windll.kernel32
        codepage = int(kernel32.GetConsoleOutputCP() or 0)
        if codepage:
            return codepage
        return int(kernel32.GetOEMCP() or 0)
    except Exception:
        return 0


def _console_encodings() -> tuple[str, ...]:
    """Windows 控制台程序实际使用的代码页（实际代码页在前，OEM/ANSI 兜底）。"""
    if sys.platform != "win32":
        return ()
    encodings: list[str] = []
    codepage = _windows_console_codepage()
    if codepage:
        encodings.append(f"cp{codepage}")
    # ``oem`` 对应 GetOEMCP()，``mbcs`` 对应 ANSI 代码页；中文系统通常都是 cp936。
    encodings.extend(("oem", "mbcs"))
    return tuple(dict.fromkeys(encodings))


def _default_encodings() -> tuple[str, ...]:
    # Windows 控制台程序（netsh / powershell / wmic）按控制台代码页输出；
    # UTF-8 只作为兜底，避免 GBK 字节被“合法地”解成乱码。
    return (*_console_encodings(), "utf-8")


def _fallback_encoding() -> str:
    encodings = _console_encodings()
    return encodings[0] if encodings else "utf-8"


def decode_console_bytes(
    data: bytes | None,
    encodings: Sequence[str] | None = None,
) -> str:
    """把子进程的原始字节解码成文本，任何情况下都不抛异常。"""
    if not data:
        return ""
    for bom, encoding in _BOM_ENCODINGS:
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")
    for encoding in encodings or _default_encodings():
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(_fallback_encoding(), errors="replace")


def ensure_text(value: str | bytes | None) -> str:
    """把 subprocess 的 stdout/stderr 归一成 ``str``；``None`` 归一成空串。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return decode_console_bytes(value)
    return value


def run_capture(
    cmd: Sequence[str],
    *,
    timeout: float = 20,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """执行命令并返回文本化的 ``CompletedProcess``（stdout/stderr 永不为 None）。

    其余关键字参数透传给 :func:`subprocess.run`（如 ``cwd`` / ``env`` / ``check``）。
    注意不要显式再传 ``capture_output`` / ``text``，由本函数统一负责。
    """
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)  # type: ignore[call-overload]
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        ensure_text(result.stdout),
        ensure_text(result.stderr),
    )
