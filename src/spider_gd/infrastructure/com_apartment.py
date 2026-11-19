"""让当前线程先具备 COM 套间，再去加载会连接 WMI 的库。

``wmi`` 在被 import 时就执行 ``GetObject("winmgmts:")``。工作线程没有调用
``CoInitialize`` 时，这个调用返回 ``0x800401E4``（``MK_E_SYNTAX``）。系统把它
显示成「无效的语法」。这是套间没建好，不是 WQL 写错，也不是虚拟机独有。
"""

from __future__ import annotations

import threading

# 0x800401E4 MK_E_SYNTAX。wmi 库把它记成「线程还没初始化 COM」。
MK_E_SYNTAX = -2147221020
# 0x80041017 WBEM_E_INVALID_QUERY。这才是 WQL 语法错误。
WBEM_E_INVALID_QUERY = -2147217385
# 0x80010106 RPC_E_CHANGED_MODE。本线程已经在另一个套间里，COM 可用。
RPC_E_CHANGED_MODE = -2147417850

_STATE = threading.local()


def hresult_of(exc: BaseException) -> int | None:
    """取出 COM 异常里的 HRESULT。

    Args:
        exc: ``pywintypes.com_error`` 或带同样参数布局的异常。

    Returns:
        int | None: 有符号 HRESULT。第一个参数不是整数时为 None。

    Examples:
        >>> hresult_of(RuntimeError(-2147221020, "无效的语法", None, None))
        -2147221020
        >>> hresult_of(RuntimeError("不是 COM")) is None
        True
    """
    args = getattr(exc, "args", ())
    if not args or isinstance(args[0], bool) or not isinstance(args[0], int):
        return None
    return args[0]


def com_error_kind(exc: BaseException) -> str:
    """区分 COM 套间、WQL 和其他失败。

    Args:
        exc: WMI 或 COM 抛出的异常。

    Returns:
        str: ``uninitialized-com``、``wql`` 或 ``other``。

    Examples:
        >>> com_error_kind(RuntimeError(-2147221020, "无效的语法", None, None))
        'uninitialized-com'
        >>> com_error_kind(RuntimeError(-2147217385, "查询无效", None, None))
        'wql'
    """
    code = hresult_of(exc)
    if code is None:
        return "other"
    unsigned = code & 0xFFFFFFFF
    if unsigned == MK_E_SYNTAX & 0xFFFFFFFF:
        return "uninitialized-com"
    if unsigned == WBEM_E_INVALID_QUERY & 0xFFFFFFFF:
        return "wql"
    return "other"


def ensure_com_apartment() -> None:
    """在当前线程初始化 COM。同一线程只做一次。

    失败不记成已完成，下一次健康检查还会再试。线程已经在另一个套间里时，
    COM 本身可用，记成完成。

    Returns:
        None: 套间可用时返回。

    Raises:
        ImportError: 没有 ``pythoncom``。
        Exception: ``CoInitialize`` 失败，且不是「套间已存在」。

    Examples:
        >>> callable(ensure_com_apartment)
        True
    """
    if getattr(_STATE, "ready", False):
        return
    import pythoncom

    try:
        pythoncom.CoInitialize()
    except Exception as exc:
        if hresult_of(exc) != RPC_E_CHANGED_MODE:
            raise
    _STATE.ready = True
