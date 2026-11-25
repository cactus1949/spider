"""当前进程的构建身份。健康检查和诊断包都读这里，避免只看端口。"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def _kernel32():
    """单独的 OS 边界，便于单元测试只替换系统调用本身。"""
    return ctypes.WinDLL("kernel32", use_last_error=True)


def runtime_executable() -> Path:
    """返回当前进程真实的可执行映像路径。

    Windows venv 的 ``Scripts\\python.exe`` 只是转发启动器，实际监听进程是
    基础解释器，``sys.executable`` 会错指到启动器。这里读本进程的模块文件名
    （GetModuleFileNameW(NULL)），失败或截断时抛异常而不是编造路径。
    非 Windows 保持解析后的 ``sys.executable``；冻结构建天然就是真实 exe。

    Raises:
        OSError: Windows 上无法取得完整、非空的进程映像路径。
    """
    if os.name != "nt":
        return Path(sys.executable).resolve()
    get_module_file_name = _kernel32().GetModuleFileNameW
    get_module_file_name.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_module_file_name.restype = ctypes.c_uint32
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    length = get_module_file_name(None, buffer, size)
    if length == 0:
        # get_last_error 仅在 Windows 的 ctypes 上存在；其他平台只用于单元测试。
        last_error = getattr(ctypes, "get_last_error", lambda: 0)()
        raise OSError(last_error, "GetModuleFileNameW 无法返回进程映像路径")
    if length >= size or not buffer.value:
        raise OSError("GetModuleFileNameW 返回的进程映像路径被截断或为空")
    return Path(buffer.value)


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
    from spider_gd.release_channel import runtime_release

    release = runtime_release()
    executable = runtime_executable()
    from spider_gd.config.user_home import user_home

    data_home = (user_home() or Path.cwd()).resolve()
    return {
        "spider_gd": str(release["version"] or package_version or ""),
        "channel": release["channel"],
        "build_id": stamp,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": str(executable),
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "data_home": str(data_home),
    }
