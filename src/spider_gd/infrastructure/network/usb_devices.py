"""列出当前插入的 USB 设备，供场站设置里选择。

飞天 UKey 在 Windows 上的设备名通常是通用 HID 名，同一把钥匙还会拆成
两条接口。这里按实例编号把它们都列出来，并标出 VID ``096E``。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass

from spider_gd.infrastructure.proc import run_capture

_UKEY_VIDS = {"096E"}
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


def _list_windows() -> list[UsbDevice]:
    """用 PowerShell 列出当前存在的 USB / HID 设备。

    Returns:
        list[UsbDevice]: 解析后的设备。

    Raises:
        RuntimeError: PowerShell 没有正常返回。

    Examples:
        >>> callable(_list_windows)
        True
    """
    script = (
        "Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.InstanceId -match '^(USB|HID|USBSTOR)\\\\' } | "
        "ForEach-Object { "
        "$parent = ''; "
        "try { $parent = [string](Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_Parent' -ErrorAction Stop).Data } catch {} "
        "[PSCustomObject]@{ FriendlyName = $_.FriendlyName; InstanceId = $_.InstanceId; "
        "Class = $_.Class; Parent = $parent } "
        "} | ConvertTo-Json -Compress"
    )
    result = run_capture(
        ["powershell", "-NoProfile", "-Command", script],
        timeout=20,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
        raise RuntimeError(f"读取 USB 设备失败：{detail}")
    return parse_pnp_devices(result.stdout)


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

    证书查询只含主体、颁发者、截止日期和指纹，不含私钥。

    Returns:
        dict[str, str]: ``usb`` 是设备列举原文，``certificates`` 是证书库原文。

    Examples:
        >>> callable(ukey_raw_text)
        True
    """
    return {"usb": _capture_usb_text(), "certificates": _capture_certificate_text()}


def _capture_usb_text() -> str:
    """执行当前系统的 USB 列举命令并返回原文。

    Returns:
        str: 标准输出。失败时附上退出码和错误输出。
    """
    if sys.platform == "win32":
        command = [
            "powershell", "-NoProfile", "-Command",
            "Get-PnpDevice -PresentOnly | "
            "Where-Object { $_.InstanceId -match '^(USB|HID|USBSTOR)\\\\' } | "
            "ForEach-Object { "
            "$parent = ''; "
            "try { $parent = [string](Get-PnpDeviceProperty -InstanceId $_.InstanceId "
            "-KeyName 'DEVPKEY_Device_Parent' -ErrorAction Stop).Data } catch {} "
            "[PSCustomObject]@{ FriendlyName = $_.FriendlyName; InstanceId = $_.InstanceId; "
            "Class = $_.Class; Parent = $parent } "
            "} | ConvertTo-Json -Compress",
        ]
        timeout = 20
    elif sys.platform == "darwin":
        command = ["system_profiler", "SPUSBDataType", "-json"]
        timeout = 25
    else:
        return "当前系统没有 USB 列举命令。"
    result = run_capture(command, timeout=timeout)
    text = result.stdout or ""
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        text += f"\n退出码 {result.returncode}\n{detail}"
    if len(text) > 1_000_000:
        text = text[:1_000_000] + "\n...已截断"
    return text


def _capture_certificate_text() -> str:
    """读取当前用户证书库的公开字段原文。

    Returns:
        str: PowerShell 输出。非 Windows 或查询失败时返回说明。
    """
    if sys.platform != "win32":
        return "当前系统没有 Windows 证书库，读不到 UKey 证书单位。"
    script = (
        "Get-ChildItem Cert:\\CurrentUser\\My | "
        "Where-Object { $_.NotAfter -gt (Get-Date) } | "
        "ForEach-Object { [PSCustomObject]@{ Subject = $_.Subject; Issuer = $_.Issuer; "
        "NotAfter = $_.NotAfter.ToString('yyyy-MM-dd'); Thumbprint = $_.Thumbprint } } | "
        "ConvertTo-Json -Compress"
    )
    result = run_capture(["powershell", "-NoProfile", "-Command", script], timeout=20)
    text = result.stdout or ""
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        text += f"\n退出码 {result.returncode}\n{detail}"
    if len(text) > 1_000_000:
        text = text[:1_000_000] + "\n...已截断"
    return text or "证书库没有返回内容。"


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

    Returns:
        dict[str, object]: 设备、证书、是否可区分，以及给现场看的中文结论。

    Examples:
        >>> isinstance(probe_ukeys(), dict)
        True
    """
    error = ""
    try:
        devices = list_usb_devices()
    except Exception as exc:
        devices = []
        error = str(exc)
    certificates = _list_certificates()
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
    result = run_capture(["powershell", "-NoProfile", "-Command", script], timeout=20)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
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
