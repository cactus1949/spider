"""列出当前插入的 USB 设备，供场站设置里选择。

飞天 UKey 在 Windows 上的设备名通常是通用 HID 名，同一把钥匙还会拆成
两条接口。这里按实例编号把它们都列出来，并标出 VID ``096E``。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from spider_gd.infrastructure.proc import run_capture

_UKEY_VIDS = {"096E"}
_FAST_PROBE_TIMEOUT = 6.0
_FALLBACK_PROBE_TIMEOUT = 6.0
_USB_PROBE_BUDGET = 8.0
_DOWNLOAD_BUDGET = 14.0
_HEX_ID = re.compile(r"(?i)(?:VID_|0x)?([0-9A-F]{4})")
# Windows 给没有序列号的设备编的插口位置，例如 6&256A9945&0&0000。
_LOCATION_SERIAL = re.compile(r"^\d+&[0-9A-F]+&\d+&\d+$", re.IGNORECASE)
_SKIP_NAME = re.compile(
    r"根集线器|主机控制器|Host Controller|Root Hub|集线器|USB Hub|Generic Hub|"
    r"Bluetooth|蓝牙|Camera|摄像头|FaceTime|红外|Ambient Light|"
    r"复合设备|USB Composite Device|USB 复合设备",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UsbDevice:
    """一条当前可见的 USB 设备。

    Args:
        device_id: 系统给出的实例编号。换 USB 口后可能变化。
        name: 系统里的设备名。
        label: 下拉里显示的文字。同名设备会带上区分后缀。
        vid: 厂商标识，四位十六进制；没有时为空。
        pid: 产品标识，四位十六进制；没有时为空。
        ukey_candidate: 厂商属于已知 UKey（目前是飞天 ``096E``）时为 True。
        serial: 设备序列号。空字符串表示系统只给了插口位置。

    Examples:
        >>> UsbDevice("USB\\1", "USB 输入设备", "USB 输入设备", "096E", "0317", True).ukey_candidate
        True
    """

    device_id: str
    name: str
    label: str
    vid: str = ""
    pid: str = ""
    ukey_candidate: bool = False
    serial: str = ""

    def as_dict(self) -> dict[str, str | bool]:
        """转成接口返回的字段。

        Returns:
            dict[str, str | bool]: 设备编号、名称、下拉文字和厂商信息。

        Examples:
            >>> UsbDevice("a", "钥匙", "钥匙", "096E", "0317", True).as_dict()["label"]
            '钥匙'
        """
        return {
            "id": self.device_id,
            "name": self.name,
            "label": self.label,
            "vid": self.vid,
            "pid": self.pid,
            "ukeyCandidate": self.ukey_candidate,
            "serial": self.serial,
        }


def list_usb_devices() -> list[UsbDevice]:
    """列出插在外部 USB 口上的设备。

    内置摄像头、蓝牙和主机控制器不在这里。U 盘、USB 无线网卡和 UKey 会保留。

    Returns:
        list[UsbDevice]: 按名称排序。读不到设备时返回空列表。

    Raises:
        RuntimeError: 系统命令失败。

    Examples:
        >>> isinstance(list_usb_devices, object)
        True
    """
    if sys.platform == "win32":
        return _list_windows()
    if sys.platform == "darwin":
        return _list_macos()
    return []


def parse_pnp_devices(raw: str) -> list[UsbDevice]:
    """解析 Windows ``Get-PnpDevice`` 的 JSON。

    Args:
        raw: ``ConvertTo-Json`` 的输出。只有一条时是对象，多条时是数组。

    Returns:
        list[UsbDevice]: 去重后的设备。空文本返回空列表。

    Examples:
        >>> raw = '[{"FriendlyName":"USB 输入设备","InstanceId":"USB VID_096E&PID_0317 A"}]'
        >>> parse_pnp_devices(raw)[0].ukey_candidate
        True
    """
    text = (raw or "").strip()
    if not text:
        return []
    data = json.loads(text)
    rows = data if isinstance(data, list) else [data]
    devices: list[UsbDevice] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("InstanceId") or row.get("DeviceID") or "").strip()
        name = str(row.get("FriendlyName") or row.get("Name") or "").strip()
        pnp_class = str(row.get("Class") or row.get("PNPClass") or "")
        parent = str(row.get("Parent") or "")
        if not device_id or not name or device_id in seen:
            continue
        if not is_external_usb(name, device_id, pnp_class=pnp_class, parent=parent):
            continue
        seen.add(device_id)
        vid, pid = _vid_pid(device_id)
        serial = usb_serial(device_id)
        devices.append(_device(device_id, name, vid, pid, serial))
    return _with_unique_labels(_collapse_interfaces(devices))


def parse_macos_usb(raw: str) -> list[UsbDevice]:
    """解析 macOS ``system_profiler SPUSBDataType -json``。

    Args:
        raw: 该命令的 JSON 文本。

    Returns:
        list[UsbDevice]: 带厂商编号的设备。总线节点本身不列入。

    Examples:
        >>> sample = (
        ...     '{"SPUSBDataType":[{"_name":"USB Bus","_items":[{'
        ...     '"_name":"USB 输入设备","vendor_id":"0x096e","product_id":"0x0317",'
        ...     '"serial_num":"A1"}]}]}'
        ... )
        >>> parse_macos_usb(sample)[0].vid
        '096E'
    """
    text = (raw or "").strip()
    if not text:
        return []
    data = json.loads(text)
    roots = data.get("SPUSBDataType") if isinstance(data, dict) else None
    found: list[UsbDevice] = []
    _walk_macos(roots if isinstance(roots, list) else [], found)
    return _with_unique_labels(_collapse_interfaces(found))


def _command_text(command: list[str], timeout: float, label: str) -> tuple[bool, str]:
    """执行一条枚举命令。超时和异常都变成文字，不向外抛。

    Args:
        command: 要执行的程序和参数。
        timeout: 这一条命令自己的时限，单位秒。
        label: 写进失败说明的探测名称。

    Returns:
        tuple[bool, str]: 第一项为 True 表示进程正常结束；第二项是原文或失败说明。

    Examples:
        >>> callable(_command_text)
        True
    """
    try:
        result = run_capture(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"超时（{timeout:.0f} 秒）：{label}"
    except Exception as exc:
        return False, f"失败：{label}：{exc}"
    text = (result.stdout or "").strip()
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        merged = text
        if detail:
            merged = f"{merged}\n{detail}".strip()
        return False, merged or f"失败：{label}：退出码 {result.returncode}"
    return True, text or "（没有输出）"


def _powershell(script: str) -> list[str]:
    """组装不弹窗、不逐台查父设备的 PowerShell 命令。

    Args:
        script: 单条 PowerShell 语句。

    Returns:
        list[str]: 传给 ``run_capture`` 的参数。

    Examples:
        >>> _powershell("Get-Date")[:3]
        ['powershell', '-NoProfile', '-NonInteractive']
    """
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def _cim_usb_command() -> list[str]:
    """用 WMI/CIM 只取带 VID 的即插即用设备。

    Returns:
        list[str]: ``Get-CimInstance Win32_PnPEntity`` 命令。

    Examples:
        >>> "Win32_PnPEntity" in " ".join(_cim_usb_command())
        True
    """
    script = (
        "Get-CimInstance -ClassName Win32_PnPEntity "
        "-Filter \"DeviceID LIKE '%VID_%'\" | "
        "Select-Object Name, DeviceID, PNPClass, ConfigManagerErrorCode | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    return _powershell(script)


def _pnp_fallback_command() -> list[str]:
    """慢速兜底：只在更快的探测都失败时才跑 ``Get-PnpDevice``。

    不再逐台调用 ``Get-PnpDeviceProperty``。那一步会把整次诊断拖过 20 秒。

    Returns:
        list[str]: PowerShell 命令。

    Examples:
        >>> "Get-PnpDeviceProperty" not in " ".join(_pnp_fallback_command())
        True
    """
    script = (
        "Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.InstanceId -match 'VID_' } | "
        "Select-Object FriendlyName, InstanceId, Class, Status, Problem | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    return _powershell(script)


def _pnputil_command() -> list[str]:
    """列出当前连着的设备，不启动 PowerShell。

    Returns:
        list[str]: ``pnputil /enum-devices /connected``。

    Examples:
        >>> _pnputil_command()[0]
        'pnputil'
    """
    return ["pnputil", "/enum-devices", "/connected"]


def _discard_probe_future(future: Future) -> None:
    """读掉后台探测的异常，避免解释器提示异常没人取。

    Args:
        future: 已经结束或被取消的探测任务。

    Returns:
        None: 只消费结果，不向外抛。

    Examples:
        >>> done = Future()
        >>> done.set_exception(RuntimeError("x"))
        >>> _discard_probe_future(done)
    """
    try:
        future.exception()
    except BaseException:
        return


def run_named_probes(
    probes: list[tuple[str, Callable[[], tuple[bool, str]]]],
    *,
    budget: float,
) -> list[tuple[str, bool, str]]:
    """并行跑几条探测，到总时限就返回。

    没跑完的记成超时说明，并且不再等它的线程结束。下载接口因此可以先把
    已经拿到的结果和超时说明写进诊断包。调用方自己的函数仍应带超时，
    避免线程一直占着。

    Args:
        probes: ``(名称, 函数)``。函数返回 ``(是否成功, 原文)``。
        budget: 整批最多等待的秒数。到点后函数返回，不等待未完成的线程。

    Returns:
        list[tuple[str, bool, str]]: 按输入顺序排列的名称、是否成功、原文或失败说明。

    Examples:
        >>> rows = run_named_probes([("快", lambda: (True, "ok"))], budget=1)
        >>> rows[0][2]
        'ok'
    """
    if not probes:
        return []
    pool = ThreadPoolExecutor(max_workers=len(probes), thread_name_prefix="ukey-probe")
    futures = {pool.submit(func): name for name, func in probes}
    try:
        done, pending = wait(set(futures), timeout=max(0.0, budget))
        by_name: dict[str, tuple[bool, str]] = {}
        for future in done:
            name = futures[future]
            try:
                by_name[name] = future.result()
            except Exception as exc:
                by_name[name] = (False, f"失败：{name}：{exc}")
        for future in pending:
            name = futures[future]
            future.add_done_callback(_discard_probe_future)
            future.cancel()
            by_name[name] = (False, f"超时（超过总时限 {budget:.0f} 秒）：{name}")
        return [(name, *by_name[name]) for name, _func in probes]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _format_probe_sections(rows: list[tuple[str, bool, str]]) -> str:
    """把多条探测拼成诊断包里的原文。

    Args:
        rows: :func:`run_named_probes` 的结果。

    Returns:
        str: 带分段标题的文本。没有探测时返回说明。

    Examples:
        >>> "CIM" in _format_probe_sections([("CIM", False, "超时")])
        True
    """
    if not rows:
        return "没有执行设备枚举。"
    return "\n\n".join(f"===== {name} =====\n{text}" for name, _ok, text in rows)


def _windows_usb_sections() -> list[tuple[str, bool, str]]:
    """先并行跑 CIM 和 pnputil，都失败才兜底 ``Get-PnpDevice``。

    Returns:
        list[tuple[str, bool, str]]: 每条探测的名称、是否成功、原文。

    Examples:
        >>> callable(_windows_usb_sections)
        True
    """
    def cim() -> tuple[bool, str]:
        return _command_text(_cim_usb_command(), _FAST_PROBE_TIMEOUT, "Get-CimInstance")

    def pnputil() -> tuple[bool, str]:
        return _command_text(_pnputil_command(), _FAST_PROBE_TIMEOUT, "pnputil")

    fast = [("Get-CimInstance", cim), ("pnputil", pnputil)]
    rows = run_named_probes(fast, budget=_USB_PROBE_BUDGET)
    if any(ok for _name, ok, _text in rows):
        return rows
    ok, text = _command_text(_pnp_fallback_command(), _FALLBACK_PROBE_TIMEOUT, "Get-PnpDevice")
    rows.append(("Get-PnpDevice", ok, text))
    return rows


def _devices_from_probe_text(text: str) -> tuple[list[UsbDevice], str]:
    """从一条探测原文里解析设备。失败时返回原因，不抛异常。

    Args:
        text: 命令标准输出。可能是 JSON，也可能是失败说明。

    Returns:
        tuple[list[UsbDevice], str]: 设备列表，以及失败原因。成功时原因为空串。

    Examples:
        >>> devices, error = _devices_from_probe_text("")
        >>> devices == [] and bool(error)
        True
    """
    stripped = (text or "").strip()
    if not stripped or stripped.startswith(("超时", "失败")) or "超过总时限" in stripped:
        return [], stripped or "没有输出"
    try:
        return parse_pnp_devices(stripped), ""
    except json.JSONDecodeError as exc:
        return [], f"不是设备 JSON：{exc}"


def _list_windows() -> list[UsbDevice]:
    """列出当前存在的 USB 设备。

    先用 CIM。CIM 失败才用 ``Get-PnpDevice``，并且不再逐台查父设备。

    Returns:
        list[UsbDevice]: 解析后的设备。命令成功但没有设备时为空列表。

    Raises:
        RuntimeError: 快速探测和兜底都没有拿到可解析的结果。

    Examples:
        >>> callable(_list_windows)
        True
    """
    notes: list[str] = []
    for label, command, timeout in (
        ("Get-CimInstance", _cim_usb_command(), _FAST_PROBE_TIMEOUT),
        ("Get-PnpDevice", _pnp_fallback_command(), _FALLBACK_PROBE_TIMEOUT),
    ):
        ok, text = _command_text(command, timeout, label)
        if not ok:
            notes.append(text)
            continue
        try:
            return parse_pnp_devices(text)
        except json.JSONDecodeError as exc:
            notes.append(f"{label} 不是设备 JSON：{exc}")
    raise RuntimeError("；".join(notes) or "没有枚举到 USB 设备")


def _list_macos() -> list[UsbDevice]:
    """用 system_profiler 列出 USB 设备。

    Returns:
        list[UsbDevice]: 解析后的设备。

    Raises:
        RuntimeError: system_profiler 没有正常返回。

    Examples:
        >>> callable(_list_macos)
        True
    """
    result = run_capture(
        ["system_profiler", "SPUSBDataType", "-json"],
        timeout=25,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
        raise RuntimeError(f"读取 USB 设备失败：{detail}")
    return parse_macos_usb(result.stdout)


def ukey_raw_text() -> dict[str, str]:
    """读取 UKey 检测用到的系统原文。

    设备和证书并行采集。任何一条超时或失败都写成说明，不抛异常。
    证书查询只含主体、颁发者、截止日期和指纹，不含私钥。

    Returns:
        dict[str, str]: ``usb`` 是设备列举原文，``certificates`` 是证书库原文。

    Examples:
        >>> callable(ukey_raw_text)
        True
    """
    try:
        rows = run_named_probes(
            [
                ("usb", _capture_usb_outcome),
                ("certificates", _capture_certificate_outcome),
            ],
            budget=_DOWNLOAD_BUDGET,
        )
    except Exception as exc:
        message = f"读取诊断原文失败：{exc}"
        return {"usb": message, "certificates": message}
    found = {name: text for name, _ok, text in rows}
    return {
        "usb": _clip_probe_text(found.get("usb") or "没有 USB 原文。"),
        "certificates": _clip_probe_text(found.get("certificates") or "没有证书原文。"),
    }


def _clip_probe_text(text: str) -> str:
    """截断过长的探测原文。

    Args:
        text: 命令输出或失败说明。

    Returns:
        str: 最长约 1 MB。

    Examples:
        >>> _clip_probe_text("abc")
        'abc'
    """
    if len(text) > 1_000_000:
        return text[:1_000_000] + "\n...已截断"
    return text


def _capture_usb_outcome() -> tuple[bool, str]:
    """采集 USB 原文，并把失败收成文字。

    Returns:
        tuple[bool, str]: 是否至少有一条探测成功，以及拼好的原文。

    Examples:
        >>> callable(_capture_usb_outcome)
        True
    """
    try:
        if sys.platform == "win32":
            rows = _windows_usb_sections()
            return any(ok for _name, ok, _text in rows), _format_probe_sections(rows)
        if sys.platform == "darwin":
            return _command_text(
                ["system_profiler", "SPUSBDataType", "-json"],
                12,
                "system_profiler",
            )
        return True, "当前系统没有 USB 列举命令。"
    except Exception as exc:
        return False, f"失败：USB：{exc}"


def _capture_usb_text() -> str:
    """执行当前系统的 USB 列举命令并返回原文。

    Windows 上先并行跑 CIM 和 pnputil。两者都失败才兜底 ``Get-PnpDevice``，
    并且不再逐台查父设备。超时和异常都留在原文里。

    Returns:
        str: 探测原文。失败时是说明，不抛异常。

    Examples:
        >>> callable(_capture_usb_text)
        True
    """
    _ok, text = _capture_usb_outcome()
    return _clip_probe_text(text)


def _capture_certificate_outcome() -> tuple[bool, str]:
    """读取证书库原文。失败时返回说明，不抛异常。

    Returns:
        tuple[bool, str]: 是否读到命令结果，以及原文或失败说明。

    Examples:
        >>> ok, text = _capture_certificate_outcome()
        >>> ok is True and "证书" in text
        True
    """
    if sys.platform != "win32":
        return True, "当前系统没有 Windows 证书库，读不到 UKey 证书单位。"
    script = (
        "Get-ChildItem Cert:\\CurrentUser\\My | "
        "Where-Object { $_.NotAfter -gt (Get-Date) } | "
        "ForEach-Object { [PSCustomObject]@{ Subject = $_.Subject; Issuer = $_.Issuer; "
        "NotAfter = $_.NotAfter.ToString('yyyy-MM-dd'); Thumbprint = $_.Thumbprint } } | "
        "ConvertTo-Json -Compress"
    )
    try:
        return _command_text(_powershell(script), _FAST_PROBE_TIMEOUT, "证书库")
    except Exception as exc:
        return False, f"失败：证书库：{exc}"


def _capture_certificate_text() -> str:
    """读取当前用户证书库的公开字段原文。

    Returns:
        str: PowerShell 输出。非 Windows 或查询失败时返回说明，不抛异常。

    Examples:
        >>> isinstance(_capture_certificate_text(), str)
        True
    """
    _ok, text = _capture_certificate_outcome()
    return _clip_probe_text(text or "证书库没有返回内容。")


def _walk_macos(nodes: list[object], found: list[UsbDevice]) -> None:
    """递归收集 macOS USB 树里的设备。

    Args:
        nodes: 当前层节点。
        found: 收集结果，函数会原地追加。

    Returns:
        None: 结果写入 ``found``。

    Examples:
        >>> bucket: list[UsbDevice] = []
        >>> _walk_macos([], bucket)
        >>> bucket
        []
    """
    seen = {item.device_id for item in found}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = str(node.get("_name") or "").strip()
        vid = _hex4(str(node.get("vendor_id") or ""))
        pid = _hex4(str(node.get("product_id") or ""))
        serial = str(node.get("serial_num") or "").strip()
        location = str(node.get("location_id") or "").strip()
        device_id = serial or location or f"{vid}:{pid}:{name}"
        instance = f"USB\\VID_{vid}&PID_{pid}\\{serial or location}"
        external = bool(name and vid and is_external_usb(name, instance))
        if external and device_id not in seen:
            seen.add(device_id)
            found.append(_device(device_id, name, vid, pid, serial))
        children = node.get("_items")
        if isinstance(children, list):
            _walk_macos(children, found)


def is_external_usb(
    name: str,
    device_id: str,
    *,
    pnp_class: str = "",
    parent: str = "",
) -> bool:
    """判断这是不是插在外部 USB 口上的设备。

    内置摄像头、蓝牙、主机控制器和集线器不算。U 盘、USB 无线网卡和飞天 UKey 算。

    Args:
        name: 设备名称。
        device_id: 实例编号。
        pnp_class: Windows 设备类，可空。
        parent: 父设备编号，可空。

    Returns:
        bool: 应出现在特征码下拉中时为 True。

    Examples:
        >>> is_external_usb("大容量存储设备", r"USB\\VID_1234&PID_5678\\DISK1")
        True
        >>> is_external_usb("USB 无线网卡", r"USB\\VID_0BDA&PID_8179\\WIFI1")
        True
        >>> is_external_usb("Integrated Camera", r"USB\\VID_13D3&PID_56FB\\CAM")
        False
        >>> is_external_usb("USB Root Hub", r"USB\\ROOT_HUB30\\4")
        False
    """
    if _SKIP_NAME.search(name or ""):
        return False
    kind = (pnp_class or "").lower()
    if kind in {"bluetooth", "camera", "softwaredevice", "system", "computer"}:
        return False
    upper = (device_id or "").upper()
    parent_upper = (parent or "").upper()
    vid, _pid = _vid_pid(device_id)
    if vid in _UKEY_VIDS:
        return True
    if upper.startswith("HID\\") and not parent_upper.startswith("USB\\VID_"):
        return False
    return upper.startswith("USB\\VID_") or upper.startswith("USBSTOR\\")


def usb_serial(device_id: str) -> str:
    """从实例编号末尾取出序列号。

    插口位置（``6&256A9945&0&0000``）不当作序列号。

    Args:
        device_id: 实例编号。

    Returns:
        str: 稳定序列号；没有时为空字符串。

    Examples:
        >>> usb_serial(r"USB\\VID_096E&PID_0317\\SN-A")
        'SN-A'
        >>> usb_serial(r"USB\\VID_096E&PID_0317\\6&256A9945&0&0000")
        ''
    """
    tail = (device_id or "").replace("/", "\\").split("\\")[-1].strip()
    if not tail or _LOCATION_SERIAL.match(tail):
        return ""
    return tail


def _device(
    device_id: str,
    name: str,
    vid: str,
    pid: str,
    serial: str = "",
) -> UsbDevice:
    """组装一条还没处理重名的设备。

    Args:
        device_id: 实例编号。
        name: 设备名。
        vid: 厂商编号。
        pid: 产品编号。
        serial: 稳定序列号，可空。

    Returns:
        UsbDevice: ``label`` 先等于名称。

    Examples:
        >>> _device("A", "钥匙", "096E", "0317", "SN").ukey_candidate
        True
    """
    return UsbDevice(
        device_id=device_id,
        name=name,
        label=name,
        vid=vid,
        pid=pid,
        ukey_candidate=vid in _UKEY_VIDS,
        serial=serial,
    )


def _with_unique_labels(devices: list[UsbDevice]) -> list[UsbDevice]:
    """同名设备的下拉文字加上编号，避免选中时分不清。

    Args:
        devices: 原始设备。

    Returns:
        list[UsbDevice]: 按显示文字排序，且 ``label`` 互不相同。

    Examples:
        >>> rows = [
        ...     UsbDevice("AAAA", "钥匙", "钥匙", "096E", "0317", True),
        ...     UsbDevice("BBBB", "钥匙", "钥匙", "096E", "0317", True),
        ... ]
        >>> [item.label for item in _with_unique_labels(rows)]
        ['钥匙 · AAAA · 096E:0317', '钥匙 · BBBB · 096E:0317']
    """
    counts: dict[str, int] = {}
    for device in devices:
        counts[device.name] = counts.get(device.name, 0) + 1
    labeled: list[UsbDevice] = []
    for device in devices:
        label = device.name
        if counts[device.name] > 1:
            suffix = device.device_id[-8:] if len(device.device_id) > 8 else device.device_id
            label = f"{device.name} · {suffix}"
        vendor = f"{device.vid}:{device.pid}" if device.vid else ""
        if vendor:
            label = f"{label} · {vendor}"
        labeled.append(
            UsbDevice(
                device_id=device.device_id,
                name=device.name,
                label=label,
                vid=device.vid,
                pid=device.pid,
                ukey_candidate=device.ukey_candidate,
                serial=device.serial,
            )
        )
    return sorted(labeled, key=lambda item: (not item.ukey_candidate, item.label))


def _collapse_interfaces(devices: list[UsbDevice]) -> list[UsbDevice]:
    """把同一把设备的多个接口收成一行。

    只有序列号相同的接口才会合并。没有序列号的两把钥匙仍会各占一行。

    Args:
        devices: 已经滤过的外部设备。

    Returns:
        list[UsbDevice]: 合并后的设备，顺序与第一次出现一致。

    Examples:
        >>> rows = [
        ...     _device(r"USB\\VID_096E&PID_0317\\SN", "USB 输入设备", "096E", "0317", "SN"),
        ...     _device(r"HID\\VID_096E&PID_0317\\SN", "HID 设备", "096E", "0317", "SN"),
        ... ]
        >>> len(_collapse_interfaces(rows))
        1
    """
    grouped: dict[str, UsbDevice] = {}
    order: list[str] = []
    for device in devices:
        key = f"{device.vid}:{device.pid}:{device.serial}" if device.serial else device.device_id
        current = grouped.get(key)
        if current is None:
            grouped[key] = device
            order.append(key)
            continue
        grouped[key] = current if _device_rank(current) >= _device_rank(device) else device
    return [grouped[key] for key in order]


def _device_rank(device: UsbDevice) -> tuple[bool, bool]:
    """给同一把设备的接口排序，优先保留 USB 口上的那条。

    Args:
        device: 一个接口。

    Returns:
        tuple[bool, bool]: 数值越大越适合作为这一把设备的代表。

    Examples:
        >>> left = _device(r"USB\\VID_096E&PID_0317\\SN", "UKey", "096E", "0317", "SN")
        >>> right = _device(r"HID\\VID_096E&PID_0317\\SN", "HID", "096E", "0317", "SN")
        >>> _device_rank(left) >= _device_rank(right)
        True
    """
    generic = device.name in {"USB 输入设备", "符合 HID 标准的供应商定义设备", "HID 设备"}
    on_usb = device.device_id.upper().startswith("USB\\VID_")
    return (not generic, on_usb)


def probe_ukeys() -> dict[str, object]:
    """检测外部 USB 和证书，判断多把 UKey 能不能分开。

    枚举失败时仍返回结论对象，不把异常抛给下载接口。

    Returns:
        dict[str, object]: 设备、证书、是否可区分，以及给现场看的中文结论。

    Examples:
        >>> isinstance(probe_ukeys(), dict)
        True
    """
    try:
        return _probe_ukeys_uncached()
    except Exception as exc:
        return {
            "devices": [],
            "ukeys": [],
            "certificates": [],
            "distinguishable": False,
            "summary": f"读取 USB 设备失败：{exc}",
            "error": str(exc),
        }


def _probe_ukeys_uncached() -> dict[str, object]:
    """真正执行一次 UKey 检测。

    Returns:
        dict[str, object]: 和 :func:`probe_ukeys` 相同的字段。

    Examples:
        >>> callable(_probe_ukeys_uncached)
        True
    """
    error = ""
    try:
        devices = list_usb_devices()
    except Exception as exc:
        devices = []
        error = str(exc)
    try:
        certificates = _list_certificates()
    except Exception as exc:
        certificates = []
        error = error or f"读取证书失败：{exc}"
    ukeys = [device for device in devices if device.ukey_candidate]
    organizations = _certificate_orgs(certificates)
    return {
        "devices": [device.as_dict() for device in devices],
        "ukeys": [device.as_dict() for device in ukeys],
        "certificates": certificates,
        "distinguishable": ukeys_are_distinguishable(ukeys, organizations),
        "summary": ukey_summary(ukeys, organizations, error),
        "error": error,
    }


def ukeys_are_distinguishable(ukeys: list[UsbDevice], organizations: list[str]) -> bool:
    """判断当前这些 UKey 有没有稳定的区别。

    Args:
        ukeys: 飞天 UKey 列表。
        organizations: 证书里的单位名称，已去重。

    Returns:
        bool: 序列号互不相同，或证书单位至少有两个时为 True。

    Examples:
        >>> key_a = _device("A", "钥匙", "096E", "0317", "SN-A")
        >>> key_b = _device("B", "钥匙", "096E", "0317", "SN-B")
        >>> ukeys_are_distinguishable([key_a, key_b], [])
        True
        >>> blank = _device("C", "钥匙", "096E", "0317", "")
        >>> ukeys_are_distinguishable([key_a, blank], ["阳江", "浈江"])
        True
    """
    serials = [device.serial for device in ukeys]
    if len(ukeys) >= 2 and all(serials) and len(set(serials)) == len(serials):
        return True
    return len(organizations) >= 2


def ukey_summary(ukeys: list[UsbDevice], organizations: list[str], error: str = "") -> str:
    """写成给现场人员看的检测结论。

    Args:
        ukeys: 飞天 UKey 列表。
        organizations: 证书单位。
        error: 读设备失败时的原因。

    Returns:
        str: 中文结论。

    Examples:
        >>> "序列号" in ukey_summary([
        ...     _device("A", "钥匙", "096E", "0317", "SN-A"),
        ...     _device("B", "钥匙", "096E", "0317", "SN-B"),
        ... ], [])
        True
    """
    if error:
        return f"读取 USB 设备失败：{error}"
    if not ukeys:
        return (
            "没有发现飞天 UKey。请把钥匙插在外部 USB 口上再检测。"
            "U 盘和 USB 无线网卡可以出现，内置网卡不会出现。"
        )
    serials = [device.serial or "无序列号" for device in ukeys]
    unique_serials = {device.serial for device in ukeys}
    lines = [f"发现 {len(ukeys)} 把飞天 UKey。序列号：{'、'.join(serials)}。"]
    stable = len(ukeys) >= 2 and "" not in unique_serials and len(unique_serials) == len(ukeys)
    if stable:
        lines.append("序列号各不相同，可以把序列号当作特征码，用来区分这几把钥匙。")
    elif len(ukeys) >= 2:
        lines.append("系统没有给出各不相同的序列号。这些编号可能只是插口位置，换 USB 口后会变。")
    else:
        lines.append("目前只有一把。要确认能不能区分，请把第二把也插上再检测一次。")
    if len(organizations) >= 2:
        joined = "；".join(organizations)
        lines.append(f"证书单位也不相同：{joined}。可以在场站里用单位名称里的一段字来匹配。")
    elif len(organizations) == 1:
        lines.append(f"证书单位目前都是「{organizations[0]}」，单靠单位名称还分不开。")
    else:
        lines.append("没有读到证书单位。Windows 上插着钥匙时再检测，才能看到单位和颁发者。")
    return "".join(lines)


def gather_ukey_download_parts() -> tuple[dict[str, object], dict[str, str], str]:
    """并行采集检测结论和原文，任何失败都收进警告。

    下载接口用这个结果写 zip。函数本身不抛异常，这样枚举超时也不会变成 HTTP 500。

    Returns:
        tuple[dict[str, object], dict[str, str], str]: 检测结论、原文、给界面看的警告。
        警告为空表示这次枚举没有失败说明。

    Examples:
        >>> callable(gather_ukey_download_parts)
        True
    """
    try:
        rows = run_named_probes(
            [
                ("report", _probe_outcome),
                ("raw", _raw_outcome),
            ],
            budget=_DOWNLOAD_BUDGET,
        )
    except Exception as exc:
        message = f"读取 USB 设备失败：{exc}"
        report = {
            "devices": [],
            "ukeys": [],
            "certificates": [],
            "distinguishable": False,
            "summary": message,
            "error": str(exc),
        }
        return report, {"usb": message, "certificates": message}, message
    found = {name: text for name, _ok, text in rows}
    report = _decode_report_outcome(found.get("report") or "")
    raw = _decode_raw_outcome(found.get("raw") or "")
    return report, raw, ukey_download_warning(report, raw)


def _probe_outcome() -> tuple[bool, str]:
    """把检测结论收成 JSON 文字，失败也返回。

    Returns:
        tuple[bool, str]: 是否没有错误字段，以及 JSON 文本。

    Examples:
        >>> callable(_probe_outcome)
        True
    """
    try:
        report = probe_ukeys()
        if not isinstance(report, dict):
            report = {
                "devices": [],
                "ukeys": [],
                "certificates": [],
                "distinguishable": False,
                "summary": str(report),
                "error": "检测结果不是对象",
            }
        return not str(report.get("error") or "").strip(), json.dumps(report, ensure_ascii=False)
    except Exception as exc:
        payload = {
            "devices": [],
            "ukeys": [],
            "certificates": [],
            "distinguishable": False,
            "summary": f"读取 USB 设备失败：{exc}",
            "error": str(exc),
        }
        return False, json.dumps(payload, ensure_ascii=False)


def _raw_outcome() -> tuple[bool, str]:
    """把 USB 和证书原文收成 JSON。

    Returns:
        tuple[bool, str]: 原文里没有超时或失败字样时为成功，以及 JSON 文本。

    Examples:
        >>> callable(_raw_outcome)
        True
    """
    try:
        raw = ukey_raw_text()
        if not isinstance(raw, dict):
            raw = {"usb": str(raw), "certificates": ""}
        usb = str(raw.get("usb") or "")
        certificates = str(raw.get("certificates") or "")
        failed = _text_has_probe_failure(usb) or _text_has_probe_failure(certificates)
        payload = {"usb": usb, "certificates": certificates}
        return not failed, json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        message = f"读取诊断原文失败：{exc}"
        return False, json.dumps({"usb": message, "certificates": message}, ensure_ascii=False)


def _decode_report_outcome(text: str) -> dict[str, object]:
    """从并行探测的文字里还原检测结论。

    Args:
        text: JSON 对象，或超时说明。

    Returns:
        dict[str, object]: 检测结论。解析失败时 ``error`` 里是原文。

    Examples:
        >>> _decode_report_outcome("超时")["error"]
        '超时'
    """
    fallback = {
        "devices": [],
        "ukeys": [],
        "certificates": [],
        "distinguishable": False,
        "summary": text or "没有检测结论。",
        "error": text or "没有检测结论。",
    }
    try:
        parsed = json.loads(text or "")
    except json.JSONDecodeError:
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    parsed.setdefault("devices", [])
    parsed.setdefault("ukeys", [])
    parsed.setdefault("certificates", [])
    parsed.setdefault("distinguishable", False)
    parsed.setdefault("summary", "")
    parsed.setdefault("error", "")
    return parsed


def _decode_raw_outcome(text: str) -> dict[str, str]:
    """从并行探测的文字里还原 USB 和证书原文。

    Args:
        text: JSON 对象，或超时说明。

    Returns:
        dict[str, str]: ``usb`` 和 ``certificates``。

    Examples:
        >>> _decode_raw_outcome("超时")["usb"]
        '超时'
    """
    try:
        parsed = json.loads(text or "")
    except json.JSONDecodeError:
        message = text or "没有诊断原文。"
        return {"usb": message, "certificates": message}
    if not isinstance(parsed, dict):
        message = text or "没有诊断原文。"
        return {"usb": message, "certificates": message}
    return {
        "usb": str(parsed.get("usb") or ""),
        "certificates": str(parsed.get("certificates") or ""),
    }


_SECTION_HEADER = re.compile(r"(?m)^===== .+ =====\s*")


def _probe_sections(text: str) -> list[str]:
    """按诊断分段标题切开原文。

    Args:
        text: 一条或几条探测拼起来的原文。

    Returns:
        list[str]: 去掉标题后的各段。没有标题时整段作为一块。

    Examples:
        >>> _probe_sections("===== CIM =====\\nVID_096E")
        ['VID_096E']
    """
    raw = text or ""
    if not _SECTION_HEADER.search(raw):
        return [raw] if raw else []
    return [part.strip() for part in _SECTION_HEADER.split(raw) if part.strip()]


def _section_failed(text: str) -> bool:
    """判断这一段是不是枚举超时或失败说明。

    只看第一行。设备名或证书主题里偶然出现「超时」不算失败。

    Args:
        text: 一段探测原文。

    Returns:
        bool: 第一行是超时或失败说明时为 True。

    Examples:
        >>> _section_failed("超时（6 秒）：pnputil")
        True
        >>> _section_failed('[{"Name":"操作超时"}]')
        False
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    head = stripped.splitlines()[0].strip()
    return head.startswith(("超时", "失败")) or "超过总时限" in head


def _failure_excerpt(text: str) -> str:
    """取出第一段失败说明，跳过诊断分段标题。

    Args:
        text: 探测原文。

    Returns:
        str: 失败段的第一行，最长 180 字。没有失败段时为空串。

    Examples:
        >>> _failure_excerpt("===== CIM =====\\n超时（6 秒）：CIM")
        '超时（6 秒）：CIM'
    """
    for section in _probe_sections(text):
        if _section_failed(section):
            return section.strip().splitlines()[0][:180]
    return ""


def _text_has_probe_failure(text: str) -> bool:
    """判断这次枚举是不是整段都失败了。

    多段探测里只要有一段成功，就不把另外一段的超时当成整次失败。
    否则 CIM 已经列出设备、pnputil 超时，界面仍会报采集不完整。

    Args:
        text: 探测输出。

    Returns:
        bool: 没有成功段时为 True。空文本为 False。

    Examples:
        >>> _text_has_probe_failure("超时（6 秒）：Get-CimInstance")
        True
        >>> _text_has_probe_failure("VID_096E")
        False
        >>> mixed = "===== CIM =====\\nVID_096E\\n\\n===== pnputil =====\\n超时（6 秒）：pnputil"
        >>> _text_has_probe_failure(mixed)
        False
    """
    sections = _probe_sections(text)
    if not sections:
        return False
    return all(_section_failed(part) for part in sections)


def ukey_download_warning(report: dict[str, object], raw: dict[str, str]) -> str:
    """抽出给界面看的一句警告。

    Args:
        report: 检测结论。
        raw: USB 和证书原文。

    Returns:
        str: 没有失败时为空串，最长约 400 字。

    Examples:
        >>> ukey_download_warning({"error": ""}, {"usb": "VID_096E", "certificates": ""})
        ''
        >>> sample = {"usb": "超时（6 秒）：Get-CimInstance", "certificates": ""}
        >>> "超时" in ukey_download_warning({"error": ""}, sample)
        True
    """
    parts: list[str] = []
    error = str(report.get("error") or "").strip()
    if error:
        parts.append(error)
    usb = str(raw.get("usb") or "")
    certificates = str(raw.get("certificates") or "")
    if _text_has_probe_failure(usb) or _text_has_probe_failure(certificates):
        source = usb if _text_has_probe_failure(usb) else certificates
        first = _failure_excerpt(source)
        parts.append(first or "部分设备或证书枚举没有完成，失败说明已经写进诊断包。")
    return "；".join(dict.fromkeys(parts))[:400]


def _list_certificates() -> list[dict[str, str]]:
    """读取当前用户尚未过期的证书。

    只有 Windows 证书库里才有 UKey 登录用的单位名称。其他系统返回空列表。

    Returns:
        list[dict[str, str]]: 每项含主体、颁发者、截止日期、指纹和单位。

    Examples:
        >>> isinstance(_list_certificates(), list)
        True
    """
    if sys.platform != "win32":
        return []
    script = (
        "Get-ChildItem Cert:\\CurrentUser\\My | "
        "Where-Object { $_.NotAfter -gt (Get-Date) } | "
        "ForEach-Object { [PSCustomObject]@{ Subject = $_.Subject; Issuer = $_.Issuer; "
        "NotAfter = $_.NotAfter.ToString('yyyy-MM-dd'); Thumbprint = $_.Thumbprint } } | "
        "ConvertTo-Json -Compress"
    )
    ok, text = _command_text(_powershell(script), _FAST_PROBE_TIMEOUT, "证书库")
    if not ok or not text.strip() or text == "（没有输出）":
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = data if isinstance(data, list) else [data]
    certificates: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        subject = str(row.get("Subject") or "")
        certificates.append({
            "subject": subject,
            "issuer": str(row.get("Issuer") or ""),
            "notAfter": str(row.get("NotAfter") or ""),
            "thumbprint": str(row.get("Thumbprint") or ""),
            "organization": _subject_value(subject, "O"),
        })
    return certificates


def _certificate_orgs(certificates: list[dict[str, str]]) -> list[str]:
    """取出不重复的证书单位。

    Args:
        certificates: :func:`_list_certificates` 的结果。

    Returns:
        list[str]: 单位名称。

    Examples:
        >>> rows = [{"organization": "阳江"}, {"organization": "阳江"}, {"organization": ""}]
        >>> _certificate_orgs(rows)
        ['阳江']
    """
    found: list[str] = []
    for item in certificates:
        name = str(item.get("organization") or "").strip()
        if name and name not in found:
            found.append(name)
    return found


def _subject_value(subject: str, key: str) -> str:
    """从证书主体里取出一个字段。

    Args:
        subject: 例如 ``CN=公司, O=阳江公司, C=CN``。
        key: 字段名，例如 ``O`` 或 ``CN``。

    Returns:
        str: 字段值。没有时为空字符串。

    Examples:
        >>> _subject_value("CN=甲, O=三峡新能源阳江发电有限公司, C=CN", "O")
        '三峡新能源阳江发电有限公司'
    """
    match = re.search(rf"(?:^|,\s*){re.escape(key)}=([^,]+)", subject or "")
    return match.group(1).strip() if match else ""


def _vid_pid(device_id: str) -> tuple[str, str]:
    """从实例编号里取出 VID 和 PID。

    Args:
        device_id: 例如 ``USB\\VID_096E&PID_0317\\A``。

    Returns:
        tuple[str, str]: 四位大写编号。没有时为空字符串。

    Examples:
        >>> _vid_pid(r"USB\\VID_096E&PID_0317\\A")
        ('096E', '0317')
    """
    vid = _one_id(device_id, "VID_")
    pid = _one_id(device_id, "PID_")
    return vid, pid


def _one_id(device_id: str, marker: str) -> str:
    """取出 ``VID_`` 或 ``PID_`` 后面的四位。

    Args:
        device_id: 设备实例编号。
        marker: ``VID_`` 或 ``PID_``。

    Returns:
        str: 四位大写十六进制；找不到时为空。

    Examples:
        >>> _one_id("HID\\\\VID_096E&PID_0317", "VID_")
        '096E'
    """
    upper = device_id.upper()
    start = upper.find(marker)
    if start < 0:
        return ""
    chunk = upper[start + len(marker): start + len(marker) + 4]
    return chunk if re.fullmatch(r"[0-9A-F]{4}", chunk) else ""


def _hex4(value: str) -> str:
    """从 ``0x096e`` 或带厂商说明的文本里取出四位编号。

    Args:
        value: system_profiler 的 vendor_id / product_id。

    Returns:
        str: 四位大写十六进制；没有时为空。

    Examples:
        >>> _hex4("0x096e  (Feitian)")
        '096E'
    """
    match = re.search(r"(?i)0x([0-9a-f]{4})", value)
    if match:
        return match.group(1).upper()
    match = _HEX_ID.search(value)
    return match.group(1).upper() if match else ""
