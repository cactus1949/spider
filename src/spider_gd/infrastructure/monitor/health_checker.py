"""统一健康检查：聚合各链路检查项，输出整体健康。

实现 :class:`spider_gd.interfaces.monitor.IHealthChecker`。
检查项以 ``(name, async fn -> HealthReport)`` 形式注入，便于扩展与测试。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from loguru import logger

from spider_gd.interfaces.monitor import HealthLevel, HealthReport

CheckFn = Callable[[], Awaitable[HealthReport]]

# U-Key 枚举单独占一个线程：WMI/COM 挂起时不能占满默认线程池，也不能卡住事件循环。
_UKEY_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ukey-wmi")
_COM_STATE = threading.local()
_UKEY_UNAVAILABLE = "U-Key 检测不可用（非 Windows 或 WMI 未安装）"
_UKEY_QUERY_TIMEOUT = 8.0
_UKEY_POWERSHELL_TIMEOUT = 6.0

# 常见的 U-Key / 智能卡设备特征。匹配逻辑与历史行为保持一致。
_UKEY_PATTERNS = [
    "smartcard", "smart card", "ccid", "usb key", "token",
    "飞天", "握奇", "华大", "海泰", "中孚", "吉大", "恒宝",
    "feitian", "watchdata", "huada", "haitai",
    "hid",
]
_UKEY_VIDS = {
    "096E",  # Feitian Technologies
    "2CE3",  # Watchdata
    "1A86",  # QinHeng (CH552/CH554)
    "24DC",  # ARDS
}
_PS_CIM_SCRIPT = (
    "Get-CimInstance -ClassName Win32_PnPEntity | "
    "Where-Object { $_.DeviceID -match 'VID_' } | "
    "Select-Object Name, Description, DeviceID, PNPClass, ConfigManagerErrorCode | "
    "ConvertTo-Json -Compress -Depth 3"
)
_PS_PNP_SCRIPT = (
    "Get-PnpDevice -PresentOnly | "
    "Where-Object { $_.InstanceId -match 'VID_' } | "
    "Select-Object FriendlyName, InstanceId, Class, Status, Problem | "
    "ConvertTo-Json -Compress -Depth 3"
)

DeviceQuery = Callable[[], list[dict[str, object]]]

_LEVEL_ORDER = {
    HealthLevel.OK: 0,
    HealthLevel.WARN: 1,
    HealthLevel.ERROR: 2,
    HealthLevel.CRITICAL: 3,
}


def _now_iso() -> str:
    return datetime.now().isoformat()


def disk_space_check(path: str = ".", min_free_gb: float = 1.0) -> CheckFn:
    """构造磁盘空间检查项。"""

    async def _check() -> HealthReport:
        free_gb = shutil.disk_usage(path).free / (1024**3)
        level = HealthLevel.OK if free_gb >= min_free_gb else HealthLevel.CRITICAL
        return HealthReport(
            module="disk",
            level=level,
            message=f"剩余磁盘 {free_gb:.1f}GB",
            timestamp=_now_iso(),
            metadata={"free_gb": round(free_gb, 2)},
        )

    return _check


def _error_code(value: object) -> int:
    """把驱动错误码收成整数。

    Args:
        value: WMI 的 ``ConfigManagerErrorCode`` 或 PowerShell 的 ``Problem``。
            ``None``、空串视为 0（驱动正常）。

    Returns:
        int: 非负错误码。无法解析时为 0。

    Examples:
        >>> _error_code("28")
        28
        >>> _error_code(None)
        0
        >>> _error_code("bad")
        0
    """
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pnp_record(
    device_id: object,
    description: object,
    name: object,
    pnp_class: object,
    error_code: object,
) -> dict[str, object] | None:
    """从一条即插即用记录取出 VID:PID。

    Args:
        device_id: 实例编号，需同时含 ``VID_`` 与 ``PID_``。
        description: 设备描述。
        name: 设备名称。
        pnp_class: 设备类。
        error_code: 驱动管理错误码。0 表示正常。

    Returns:
        dict[str, object] | None: 含 ``vid_pid`` 与 ``error_code``；不是 USB 设备时为 None。

    Examples:
        >>> row = _pnp_record(r"USB\\VID_096E&PID_0317\\A", "token", "UKey", "USB", 28)
        >>> row["vid_pid"] == "096E:0317" and row["error_code"] == 28
        True
        >>> _pnp_record("ROOT/DISK", "", "", "", 0) is None
        True
    """
    did = str(device_id or "").upper()
    if "VID_" not in did or "PID_" not in did:
        return None
    vid = did.split("VID_")[1][:4]
    pid = did.split("PID_")[1][:4]
    if len(vid) < 4 or len(pid) < 4:
        return None
    return {
        "vid_pid": f"{vid}:{pid}",
        "desc": str(description or ""),
        "name": str(name or ""),
        "class": str(pnp_class or ""),
        "error_code": _error_code(error_code),
        "device_id": did,
    }


def _matches_ukey(info: dict[str, object], vid_pid: str) -> bool:
    """判断这条设备是不是要找的 U-Key。

    Args:
        info: :func:`_pnp_record` 的结果。
        vid_pid: ``"auto"`` 或形如 ``"096E:0317"`` 的编号。

    Returns:
        bool: 命中时为 True。驱动错误码不参与过滤。

    Examples:
        >>> sample = {
        ...     "vid_pid": "096E:0317", "desc": "token", "name": "feitian",
        ...     "class": "USB", "error_code": 28, "device_id": r"USB\\VID_096E&PID_0317\\A",
        ... }
        >>> _matches_ukey(sample, "auto")
        True
    """
    device_vp = str(info.get("vid_pid") or "")
    if vid_pid and vid_pid != "auto":
        return device_vp == vid_pid.upper()
    if vid_pid != "auto":
        return False
    vid = device_vp.split(":", 1)[0]
    if vid in _UKEY_VIDS:
        return True
    desc = str(info.get("desc") or "").lower()
    name = str(info.get("name") or "").lower()
    pnp = str(info.get("class") or "").lower()
    combined = f"{desc} {name} {pnp}"
    if any(pattern in combined for pattern in _UKEY_PATTERNS):
        return True
    if "smartcardreader" in pnp:
        return True
    did = str(info.get("device_id") or "")
    if did.startswith("HID\\"):
        peripheral = {"KEYBOARD", "MOUSE", "TOUCHPAD", "GAMEPAD", "JOYSTICK"}
        if not any(word in combined for word in peripheral):
            return True
    return False


def _driver_error_message(devices: list[dict[str, object]]) -> str:
    """写成「已插入但驱动异常」的说明。

    Args:
        devices: 已匹配、且 ``error_code`` 非 0 的设备。

    Returns:
        str: 含 ``plugged in but driver error (code N)`` 的中文说明。

    Examples:
        >>> "code 28" in _driver_error_message([{"vid_pid": "096E:0317", "error_code": 28}])
        True
    """
    parts = [
        f"{item.get('vid_pid')} plugged in but driver error (code {item.get('error_code')})"
        for item in devices
    ]
    return "U-Key 已插入但驱动异常：" + "，".join(parts)


def classify_ukey_devices(devices: list[dict[str, object]], vid_pid: str = "auto") -> HealthReport:
    """根据已经枚举好的设备给出 U-Key 健康结论。

    驱动错误码不是 0 的匹配项不再丢弃，会记成已插入但驱动异常。

    Args:
        devices: :func:`_pnp_record` 形式的设备列表。
        vid_pid: ``"auto"`` 或指定的 ``VID:PID``。

    Returns:
        HealthReport: 模块名固定为 ``ukey``。

    Examples:
        >>> broken = {
        ...     "vid_pid": "096E:0317", "desc": "token", "name": "feitian",
        ...     "class": "USB", "error_code": 28, "device_id": r"USB\\VID_096E&PID_0317\\A",
        ... }
        >>> report = classify_ukey_devices([broken])
        >>> report.level.value == "warn" and "code 28" in report.message
        True
    """
    matched = [item for item in devices if _matches_ukey(item, vid_pid)]
    healthy = [item for item in matched if _error_code(item.get("error_code")) == 0]
    broken = [item for item in matched if _error_code(item.get("error_code")) != 0]
    if broken and not healthy:
        return HealthReport(
            module="ukey",
            level=HealthLevel.WARN,
            message=_driver_error_message(broken),
            timestamp=_now_iso(),
            metadata={"devices": matched},
        )
    if healthy:
        device_str = ", ".join(str(item["vid_pid"]) for item in healthy)
        message = f"U-Key 已连接（{device_str}）"
        if broken:
            message += "；" + _driver_error_message(broken)
        return HealthReport(
            module="ukey",
            level=HealthLevel.OK,
            message=message,
            timestamp=_now_iso(),
            metadata={"devices": matched},
        )
    if vid_pid and vid_pid != "auto":
        return HealthReport(
            module="ukey",
            level=HealthLevel.CRITICAL,
            message=f"未检测到 U-Key（VID:PID={vid_pid}）",
            timestamp=_now_iso(),
        )
    if devices:
        devices_str = ", ".join(str(item["vid_pid"]) for item in devices[:5])
        return HealthReport(
            module="ukey",
            level=HealthLevel.WARN,
            message=f"未检测到 U-Key（当前 USB 设备：{devices_str}）",
            timestamp=_now_iso(),
        )
    return HealthReport(
        module="ukey",
        level=HealthLevel.CRITICAL,
        message="未检测到任何 USB 设备",
        timestamp=_now_iso(),
    )


def _ensure_com() -> None:
    """在当前线程初始化 COM，供 WMI 查询使用。

    线程池线程会被复用，同一线程只初始化一次，避免反复配对
    ``CoUninitialize`` 把还在用的 COM 对象拆掉。

    Returns:
        None: 初始化失败时记日志并继续，由后续 WMI 调用暴露真正的错误。

    Raises:
        ImportError: 没有 ``pythoncom``（pywin32 未安装）。

    Examples:
        >>> callable(_ensure_com)
        True
    """
    if getattr(_COM_STATE, "ready", False):
        return
    import pythoncom

    try:
        pythoncom.CoInitialize()
    except Exception as exc:
        logger.warning("U-Key 检查初始化 COM 失败：{}", exc)
    _COM_STATE.ready = True


def _query_wmi_devices() -> list[dict[str, object]]:
    """用 WMI 列出带 VID/PID 的即插即用设备。

    不过滤 ``ConfigManagerErrorCode``，驱动异常的钥匙也会返回。

    Returns:
        list[dict[str, object]]: 规范化后的设备。

    Raises:
        ImportError: 当前环境没有 ``wmi`` 或 ``pythoncom``。

    Examples:
        >>> callable(_query_wmi_devices)
        True
    """
    import wmi

    _ensure_com()
    conn = wmi.WMI()
    wql = (
        "SELECT DeviceID, Description, Name, PNPClass, ConfigManagerErrorCode "
        "FROM Win32_PnPEntity WHERE DeviceID LIKE '%VID_%'"
    )
    devices: list[dict[str, object]] = []
    for ent in conn.query(wql):
        info = _pnp_record(
            getattr(ent, "DeviceID", "") or "",
            getattr(ent, "Description", "") or "",
            getattr(ent, "Name", "") or "",
            getattr(ent, "PNPClass", "") or "",
            getattr(ent, "ConfigManagerErrorCode", 0),
        )
        if info is not None:
            devices.append(info)
    return devices


def _parse_powershell_pnp(raw: str) -> list[dict[str, object]]:
    """解析轻量 ``Get-PnpDevice`` JSON。

    Args:
        raw: ``ConvertTo-Json`` 文本。单条时是对象，多条时是数组。

    Returns:
        list[dict[str, object]]: 规范化设备。空文本得到空列表。

    Raises:
        ValueError: JSON 无法解析。

    Examples:
        >>> raw = '{"InstanceId":"USB\\\\\\\\VID_096E&PID_0317\\\\\\\\A","Problem":28}'
        >>> _parse_powershell_pnp(raw)[0]["error_code"]
        28
    """
    text = (raw or "").strip()
    if not text:
        return []
    data = json.loads(text)
    rows = data if isinstance(data, list) else [data]
    devices: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        problem = row.get("Problem")
        if problem is None:
            problem = row.get("ConfigManagerErrorCode")
        info = _pnp_record(
            row.get("InstanceId") or row.get("DeviceID"),
            row.get("FriendlyName") or row.get("Description"),
            row.get("FriendlyName") or row.get("Name"),
            row.get("Class") or row.get("PNPClass"),
            problem,
        )
        if info is not None:
            devices.append(info)
    return devices


def _run_powershell_pnp(script: str) -> list[dict[str, object]]:
    """执行一条 PowerShell 枚举并把 JSON 收成设备列表。

    Args:
        script: 单条命令。不要逐台查询父设备。

    Returns:
        list[dict[str, object]]: 规范化设备。没有输出时为空列表。

    Raises:
        RuntimeError: 命令失败、超时，或输出不是 JSON。

    Examples:
        >>> callable(_run_powershell_pnp)
        True
    """
    import subprocess

    from spider_gd.infrastructure.proc import run_capture

    try:
        result = run_capture(
            ["powershell", "-NoProfile", "-Command", script],
            timeout=_UKEY_POWERSHELL_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"PowerShell 枚举超时（{_UKEY_POWERSHELL_TIMEOUT:.0f} 秒）") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"PowerShell 退出码 {result.returncode}")
    try:
        return _parse_powershell_pnp(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PowerShell 设备列表不是 JSON：{exc}") from exc


def _query_powershell_devices() -> list[dict[str, object]]:
    """WMI 模块缺失时，用 PowerShell 枚举代替。

    先试 ``Get-CimInstance Win32_PnPEntity``（带驱动错误码），
    失败再试 ``Get-PnpDevice``。两条都不逐台查父设备。

    Returns:
        list[dict[str, object]]: 规范化设备。

    Raises:
        RuntimeError: 两种枚举都失败。

    Examples:
        >>> callable(_query_powershell_devices)
        True
    """
    errors: list[str] = []
    for label, script in (
        ("Get-CimInstance", _PS_CIM_SCRIPT),
        ("Get-PnpDevice", _PS_PNP_SCRIPT),
    ):
        try:
            return _run_powershell_pnp(script)
        except Exception as exc:
            logger.warning("U-Key {} 枚举失败：{}", label, exc)
            errors.append(f"{label}: {exc}")
    raise RuntimeError("；".join(errors) or "PowerShell 未能列出设备")


class _UkeyUnavailableError(RuntimeError):
    """WMI 和备用枚举都不可用。

    Examples:
        >>> isinstance(_UkeyUnavailableError("x"), RuntimeError)
        True
    """


def _load_ukey_devices() -> list[dict[str, object]]:
    """优先 WMI；模块缺失时在 Windows 上改走 PowerShell。

    Returns:
        list[dict[str, object]]: 规范化设备。

    Raises:
        _UkeyUnavailableError: 非 Windows，或 PowerShell 也失败。
        Exception: WMI 已安装但查询本身失败。

    Examples:
        >>> callable(_load_ukey_devices)
        True
    """
    try:
        return _query_wmi_devices()
    except ImportError as exc:
        logger.warning(
            "U-Key 无法导入 WMI：{}（解释器 {}）",
            exc,
            sys.executable,
        )
        if sys.platform != "win32":
            raise _UkeyUnavailableError(f"{_UKEY_UNAVAILABLE}（{exc}）") from exc
        try:
            return _query_powershell_devices()
        except Exception as fallback_exc:
            logger.warning("U-Key PowerShell 枚举失败：{}", fallback_exc)
            raise _UkeyUnavailableError(
                f"{_UKEY_UNAVAILABLE}（{exc}；PowerShell：{fallback_exc}）"
            ) from fallback_exc


def _ukey_unavailable(message: str) -> HealthReport:
    """构造「检测不可用」的告警项。

    Args:
        message: 给界面看的原因。

    Returns:
        HealthReport: 级别为 WARN。

    Examples:
        >>> _ukey_unavailable("缺少 WMI").level.value
        'warn'
    """
    return HealthReport(
        module="ukey",
        level=HealthLevel.WARN,
        message=message,
        timestamp=_now_iso(),
    )


def ukey_check(
    vid_pid: str = "auto",
    *,
    query: DeviceQuery | None = None,
    timeout: float = _UKEY_QUERY_TIMEOUT,
) -> CheckFn:
    """构造 U-Key 接入状态检查项。

    查询放在专用线程里，并带超时，避免设备枚举挂起时拖死 ``/api/health``。
    调用线程会先初始化 COM。WMI 模块缺失时改用 PowerShell。

    Args:
        vid_pid: U-Key 的 VID:PID，如 ``"1234:5678"``；``"auto"`` 表示按厂商和名称识别。
        query: 测试注入的枚举函数。默认走 WMI / PowerShell。
        timeout: 枚举超时，单位秒。到点后健康检查返回告警，不等待线程结束。

    Returns:
        CheckFn: 无参异步函数，返回 U-Key 健康报告。

    Examples:
        >>> callable(ukey_check("auto"))
        True
    """

    async def _check() -> HealthReport:
        loader = query or _load_ukey_devices
        try:
            devices = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(_UKEY_EXECUTOR, loader),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("U-Key 设备枚举超时（{} 秒）", timeout)
            return _ukey_unavailable(f"U-Key 检测超时（{timeout:.0f} 秒）")
        except _UkeyUnavailableError as exc:
            return _ukey_unavailable(str(exc))
        except Exception as exc:
            logger.warning("U-Key 健康检查异常：{}", exc)
            if isinstance(exc, ImportError):
                text = _UKEY_UNAVAILABLE
            else:
                text = str(exc).strip() or type(exc).__name__
                if not text.startswith("U-Key"):
                    text = f"U-Key 检测异常：{text}"
            return _ukey_unavailable(text)
        return classify_ukey_devices(list(devices or []), vid_pid)

    return _check


def network_check(target_url: str = "https://pm.gd.csg.cn", timeout: float = 5.0,
                  module_name: str = "network") -> CheckFn:
    """构造网络连通性检查项。

    Args:
        target_url: 目标检测 URL（默认广东电力交易平台）
        timeout: 请求超时秒数
    """

    async def _check() -> HealthReport:
        try:
            import httpx
            import logging
            # 降低 httpx 日志级别，避免 412 等非 200 响应的 INFO 噪音
            logging.getLogger("httpx").setLevel(logging.WARNING)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
                resp = await cli.get(target_url)
            # 能连上即算 OK（包括 412/403 等，说明网络通）
            return HealthReport(
                module=module_name,
                level=HealthLevel.OK,
                message=f"网络正常（{target_url} HTTP {resp.status_code}）",
                timestamp=_now_iso(),
            )
        except Exception as exc:
            msg = str(exc) or repr(exc)
            if not msg:
                msg = type(exc).__name__
            if "timeout" in msg.lower() or "timed out" in msg.lower():
                return HealthReport(
                    module=module_name,
                    level=HealthLevel.CRITICAL,
                    message=f"网络超时（{target_url}）",
                    timestamp=_now_iso(),
                )
            elif "name or service not known" in msg.lower() or "getaddrinfo" in msg.lower():
                return HealthReport(
                    module=module_name,
                    level=HealthLevel.CRITICAL,
                    message=f"DNS 解析失败（{target_url}）",
                    timestamp=_now_iso(),
                )
            else:
                return HealthReport(
                    module=module_name,
                    level=HealthLevel.ERROR,
                    message=f"网络异常：{msg[:60]}",
                    timestamp=_now_iso(),
                )

    return _check


class HealthChecker:
    def __init__(self) -> None:
        self._checks: list[tuple[str, CheckFn]] = []

    def add_check(self, name: str, fn: CheckFn) -> None:
        self._checks.append((name, fn))

    @staticmethod
    def overall_from(reports: list[HealthReport]) -> HealthReport:
        """从已有检查结果中计算综合状态。"""
        if not reports:
            return HealthReport("system", HealthLevel.OK, "无检查项", _now_iso())
        worst = max(reports, key=lambda r: _LEVEL_ORDER[r.level])
        return HealthReport(
            module="system",
            level=worst.level,
            message=f"检测到 {len(reports)} 项，最高告警级别为 {worst.level.value}: {worst.message[:60]}",
            timestamp=_now_iso(),
        )

    async def check_all(self) -> list[HealthReport]:
        """并发执行所有健康检查。"""
        async def _safe(fn: CheckFn, name: str) -> HealthReport:
            try:
                return await fn()
            except Exception as exc:
                logger.exception("健康检查 {} 异常：{}", name, exc)
                return HealthReport(
                    module=name,
                    level=HealthLevel.ERROR,
                    message=f"检查异常：{exc}",
                    timestamp=_now_iso(),
                )

        tasks = [_safe(fn, name) for name, fn in self._checks]
        return list(await asyncio.gather(*tasks))

    async def get_overall_health(self) -> HealthReport:
        reports = await self.check_all()
        if not reports:
            return HealthReport("system", HealthLevel.OK, "无检查项", _now_iso())
        worst = max(reports, key=lambda r: _LEVEL_ORDER[r.level])
        return HealthReport(
            module="system",
            level=worst.level,
            message=f"整体 {worst.level.value}（{len(reports)} 项检查）",
            timestamp=_now_iso(),
            metadata={"checks": [r.module for r in reports]},
        )
