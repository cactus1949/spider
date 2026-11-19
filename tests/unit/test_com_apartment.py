"""WMI 的 0x800401E4 是 COM 套间，不是 WQL。导入 wmi 前必须先初始化。"""

from __future__ import annotations

import builtins
import sys

import pytest

from spider_gd.infrastructure.com_apartment import (
    MK_E_SYNTAX,
    RPC_E_CHANGED_MODE,
    WBEM_E_INVALID_QUERY,
    com_error_kind,
    ensure_com_apartment,
    hresult_of,
)
from spider_gd.infrastructure.monitor.health_checker import (
    _load_ukey_devices,
    _query_wmi_devices,
)
from spider_gd.infrastructure.network.usb_monitor import _wmi_list_usb_ids


def _ukey(vid_pid: str) -> dict[str, object]:
    return {
        "vid_pid": vid_pid,
        "desc": "token",
        "name": "feitian",
        "class": "USB",
        "error_code": 0,
        "device_id": r"USB\VID_096E&PID_0317\A",
    }


@pytest.mark.unit
def test_logged_hresult_is_moniker_syntax_not_wql():
    logged = RuntimeError(-2147221020, "无效的语法", None, None)
    assert hresult_of(logged) == MK_E_SYNTAX
    assert MK_E_SYNTAX & 0xFFFFFFFF == 0x800401E4
    assert com_error_kind(logged) == "uninitialized-com"
    assert WBEM_E_INVALID_QUERY & 0xFFFFFFFF == 0x80041017
    assert com_error_kind(RuntimeError(WBEM_E_INVALID_QUERY, "查询无效", None, None)) == "wql"


@pytest.mark.unit
def test_failed_com_init_is_retried(monkeypatch):
    import spider_gd.infrastructure.com_apartment as com

    com._STATE.ready = False
    calls = {"n": 0}

    class _Pythoncom:
        @staticmethod
        def CoInitialize() -> None:
            calls["n"] += 1
            raise RuntimeError((-1, "初始化失败", None, None))

    monkeypatch.setitem(sys.modules, "pythoncom", _Pythoncom)
    with pytest.raises(RuntimeError):
        ensure_com_apartment()
    with pytest.raises(RuntimeError):
        ensure_com_apartment()
    assert calls["n"] == 2
    assert com._STATE.ready is False


@pytest.mark.unit
def test_changed_mode_counts_as_ready_and_is_not_repeated(monkeypatch):
    import spider_gd.infrastructure.com_apartment as com

    com._STATE.ready = False
    calls = {"n": 0}

    class _Pythoncom:
        @staticmethod
        def CoInitialize() -> None:
            calls["n"] += 1
            raise RuntimeError(RPC_E_CHANGED_MODE, "套间已存在", None, None)

    monkeypatch.setitem(sys.modules, "pythoncom", _Pythoncom)
    ensure_com_apartment()
    ensure_com_apartment()
    assert calls["n"] == 1
    assert com._STATE.ready is True
    com._STATE.ready = False


@pytest.mark.unit
def test_wmi_import_happens_after_com_init(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        "spider_gd.infrastructure.com_apartment.ensure_com_apartment",
        lambda: order.append("com"),
    )
    real_import = builtins.__import__

    def tracking(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "wmi":
            order.append("import")
            raise ImportError("blocked")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", tracking)
    monkeypatch.delitem(sys.modules, "wmi", raising=False)
    with pytest.raises(ImportError, match="blocked"):
        _query_wmi_devices()
    with pytest.raises(ImportError, match="blocked"):
        _wmi_list_usb_ids()
    assert order == ["com", "import", "com", "import"]


@pytest.mark.unit
def test_uninitialized_com_still_uses_powershell(monkeypatch):
    from loguru import logger

    monkeypatch.setattr(
        "spider_gd.infrastructure.monitor.health_checker._query_wmi_devices",
        lambda: (_ for _ in ()).throw(RuntimeError(-2147221020, "无效的语法", None, None)),
    )
    monkeypatch.setattr(
        "spider_gd.infrastructure.monitor.health_checker._query_powershell_devices",
        lambda: [_ukey("096E:0317")],
    )
    monkeypatch.setattr(
        "spider_gd.infrastructure.monitor.health_checker.sys.platform",
        "win32",
    )
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        devices = _load_ukey_devices()
    finally:
        logger.remove(sink)
    assert devices[0]["vid_pid"] == "096E:0317"
    assert any("0x800401E4" in item and "不是 WQL" in item for item in messages)
    assert any("改试 PowerShell" in item for item in messages)
