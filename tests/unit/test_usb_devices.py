"""USB 设备列表：同名设备和飞天 UKey 要能分开选。"""

from __future__ import annotations

import ctypes
import json
import subprocess
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from spider_gd.core.station_profile import (
    StationProfile,
    dump_station_profiles,
    parse_station_profiles,
)
from spider_gd.infrastructure.network.usb_devices import parse_macos_usb, parse_pnp_devices


@pytest.mark.unit
def test_two_identical_ukeys_get_different_labels():
    raw = """
    [
      {"FriendlyName": "USB 输入设备", "InstanceId": "USB\\\\VID_096E&PID_0317\\\\PORT-A"},
      {"FriendlyName": "USB 输入设备", "InstanceId": "USB\\\\VID_096E&PID_0317\\\\PORT-B"},
      {"FriendlyName": "Integrated Camera", "InstanceId": "USB\\\\VID_13D3&PID_56FB\\\\CAM"}
    ]
    """
    devices = parse_pnp_devices(raw)
    labels = [item.label for item in devices]
    assert len(labels) == len(set(labels))
    ukeys = [item for item in devices if item.ukey_candidate]
    assert len(ukeys) == 2
    assert ukeys[0].label != ukeys[1].label
    assert devices[0].ukey_candidate is True


@pytest.mark.unit
def _pnp(name: str, instance_id: str, kind: str = "USB") -> dict[str, str]:
    return {"FriendlyName": name, "InstanceId": instance_id, "Class": kind}


def test_external_ports_keep_disk_wifi_and_collapse_one_ukey():
    raw = json.dumps([
        _pnp("大容量存储设备", r"USB\VID_1234&PID_5678\DISK1"),
        _pnp("USB 无线网卡", r"USB\VID_0BDA&PID_8179\WIFI1", "Net"),
        _pnp("USB Root Hub", r"USB\ROOT_HUB30\4"),
        _pnp("Integrated Camera", r"USB\VID_13D3&PID_56FB\CAM", "Camera"),
        _pnp("USB 输入设备", r"USB\VID_096E&PID_0317\SN-A"),
        _pnp("符合 HID 标准的供应商定义设备", r"HID\VID_096E&PID_0317\SN-A", "HIDClass"),
    ])
    devices = parse_pnp_devices(raw)
    names = {item.name for item in devices}
    assert "大容量存储设备" in names
    assert "USB 无线网卡" in names
    assert "USB Root Hub" not in names
    assert "Integrated Camera" not in names
    ukeys = [item for item in devices if item.ukey_candidate]
    assert len(ukeys) == 1
    assert ukeys[0].serial == "SN-A"


@pytest.mark.unit
def test_macos_usb_tree_keeps_serial_and_skips_bus():
    raw = """
    {"SPUSBDataType": [{"_name": "USB Bus", "_items": [
      {"_name": "钥匙", "vendor_id": "0x096e (Feitian)", "product_id": "0x0317",
       "serial_num": "SN1"},
      {"_name": "键盘", "vendor_id": "0x05ac", "product_id": "0x024f", "location_id": "0x01100000"}
    ]}]}
    """
    devices = parse_macos_usb(raw)
    by_name = {item.name: item for item in devices}
    assert set(by_name) == {"钥匙", "键盘"}
    assert by_name["钥匙"].device_id == "SN1"
    assert by_name["钥匙"].ukey_candidate is True
    assert by_name["键盘"].ukey_candidate is False


@pytest.mark.unit
def test_station_profile_keeps_selected_device_id():
    raw = dump_station_profiles([
        StationProfile("a", "钥匙 · SN1", "YJ", "wifi", "pin", "USB\\VID_096E\\SN1")
    ])
    profile = parse_station_profiles(raw)[0]
    assert profile.device_id == "USB\\VID_096E\\SN1"
    assert profile.wifi_password == "wifi"


@pytest.mark.unit
def test_usb_text_timeout_is_a_message(monkeypatch):
    import spider_gd.infrastructure.network.usb_devices as usb

    def boom(cmd, timeout=20, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(usb, "run_capture", boom)
    monkeypatch.setattr(usb.sys, "platform", "win32")
    text = usb._capture_usb_text()
    assert "超时" in text
    assert "Get-CimInstance" in text
    assert "Get-PnpDevice" in text
    assert "Get-PnpDeviceProperty" not in text


@pytest.mark.unit
def test_successful_fast_probe_does_not_call_get_pnpdevice(monkeypatch):
    import spider_gd.infrastructure.network.usb_devices as usb

    calls: list[str] = []

    def fake(cmd, timeout=20, **_kwargs):
        calls.append(cmd[0])
        if cmd[0] == "pnputil":
            return subprocess.CompletedProcess(cmd, 0, "Instance ID: USB\\VID_096E&PID_0317\\A", "")
        payload = '[{"Name":"钥匙","DeviceID":"USB\\\\VID_096E&PID_0317\\\\A"}]'
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(usb, "run_capture", fake)
    monkeypatch.setattr(usb.sys, "platform", "win32")
    text = usb._capture_usb_text()
    assert "Get-CimInstance" in text
    assert "pnputil" in calls
    assert "Get-PnpDevice" not in text


@pytest.mark.unit
def test_gather_still_builds_zip_when_probes_time_out(monkeypatch, tmp_path):
    import spider_gd.infrastructure.network.usb_devices as usb
    from spider_gd.gateway.execution_bundle import build_ukey_zip

    def broken_probe():
        raise subprocess.TimeoutExpired(cmd=["powershell", "Get-PnpDevice"], timeout=20)

    def broken_raw():
        raise RuntimeError("Get-PnpDevice timed out after 20 seconds")

    monkeypatch.setattr(usb, "probe_ukeys", broken_probe)
    monkeypatch.setattr(usb, "ukey_raw_text", broken_raw)
    report, raw, warning = usb.gather_ukey_download_parts()
    assert "timed out" in str(report["error"]).lower() or "Timeout" in str(report["error"])
    assert warning
    dest = tmp_path / "ukey.zip"
    build_ukey_zip(
        dest,
        report=report,
        raw_usb=str(raw["usb"]),
        raw_certificates=str(raw["certificates"]),
        bindings=[],
    )
    assert dest.is_file()
    with zipfile.ZipFile(dest) as archive:
        packed = archive.read("raw_usb.txt").decode("utf-8")
        summary = archive.read("summary.txt").decode("utf-8")
    assert "20" in packed or "timed out" in packed.lower() or "Timeout" in summary


@pytest.mark.unit
def test_partial_probe_timeout_does_not_warn_when_another_section_succeeded():
    import spider_gd.infrastructure.network.usb_devices as usb

    text = (
        "===== Get-CimInstance =====\n"
        '[{"DeviceID":"USB\\\\VID_096E&PID_0317\\\\A","Name":"钥匙"}]\n\n'
        "===== pnputil =====\n"
        "超时（6 秒）：pnputil"
    )
    assert usb._text_has_probe_failure(text) is False
    warning = usb.ukey_download_warning({"error": ""}, {"usb": text, "certificates": "[]"})
    assert warning == ""
    both_failed = (
        "===== Get-CimInstance =====\n超时（6 秒）：Get-CimInstance\n\n"
        "===== pnputil =====\n失败：pnputil：退出码 1"
    )
    assert usb._text_has_probe_failure(both_failed) is True
    warning = usb.ukey_download_warning(
        {"error": ""}, {"usb": both_failed, "certificates": ""},
    )
    assert "超时" in warning


@pytest.mark.unit
def test_named_probes_return_at_budget_without_waiting_for_the_slow_one():
    import time

    import spider_gd.infrastructure.network.usb_devices as usb

    def slow() -> tuple[bool, str]:
        time.sleep(1.2)
        return True, "late"

    started = time.monotonic()
    rows = usb.run_named_probes([("slow", slow)], budget=0.15)
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert rows == [("slow", False, "超时（超过总时限 0 秒）：slow")]


@pytest.mark.unit
def test_probe_exception_is_a_report_not_a_crash(monkeypatch):
    import spider_gd.infrastructure.network.usb_devices as usb

    def boom():
        raise RuntimeError("枚举失败")

    monkeypatch.setattr(usb, "list_usb_devices", boom)
    report = usb.probe_ukeys()
    assert report["devices"] == []
    assert "枚举失败" in str(report["error"])


def _location_ukeys(count: int) -> list[dict[str, object]]:
    hub = r"USB\VID_1234&PID_0001\7&ABC000&0&0"
    rows: list[dict[str, object]] = [{
        "Name": "Generic USB Hub", "DeviceID": hub, "PNPClass": "USB",
        "ConfigManagerErrorCode": 0,
    }]
    for index in range(count):
        root = rf"USB\VID_096E&PID_0317\7&ABC100&0&{index + 1}"
        rows.extend([
            {
                "Name": "USB 输入设备", "DeviceID": root, "PNPClass": "USB",
                "ConfigManagerErrorCode": 0, "Parent": hub,
            },
            {
                "Name": "符合 HID 标准的供应商定义设备",
                "DeviceID": rf"HID\VID_096E&PID_0317\8&DEF100&0&{index + 1:04d}",
                "PNPClass": "HIDClass", "ConfigManagerErrorCode": 0,
                "Parent": root.lower() if index % 2 else root,
            },
        ])
    return rows


def _mock_cfgmgr32(monkeypatch, rows, *, failure=None):
    parents = {row["DeviceID"].upper(): row["Parent"] for row in rows if row.get("Parent")}
    ids = {row["DeviceID"].upper() for row in rows}
    ids.update(parent.upper() for parent in parents.values())
    nodes = {device_id: 0xF0000000 + index for index, device_id in enumerate(sorted(ids))}
    by_node = {node: device_id for device_id, node in nodes.items()}

    def write_dword(pointer, value):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = value

    def locate(output, device_id, flags):
        assert flags == 0
        device_id = device_id.upper()
        if failure == ("locate", device_id):
            return 13
        write_dword(output, nodes[device_id])
        return 0

    def get_parent(output, node, flags):
        assert flags == 0
        device_id = by_node[node.value]
        if failure == ("parent", device_id) or device_id not in parents:
            return 13
        write_dword(output, nodes[parents[device_id].upper()])
        return 0

    def get_size(output, node, flags):
        assert flags == 0
        device_id = by_node[node.value]
        if failure == ("size", device_id):
            return 13
        write_dword(output, len(device_id))
        return 0

    def get_id(node, buffer, length, flags):
        assert flags == 0
        device_id = by_node[node.value]
        if failure == ("id", device_id):
            return 13
        assert length == len(device_id) + 1
        buffer.value = device_id
        return 0

    dll = SimpleNamespace(
        CM_Locate_DevNodeW=Mock(side_effect=locate),
        CM_Get_Parent=Mock(side_effect=get_parent),
        CM_Get_Device_ID_Size=Mock(side_effect=get_size),
        CM_Get_Device_IDW=Mock(side_effect=get_id),
    )
    loader = Mock(return_value=dll)
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    return loader, dll


def _mock_windows_capture(
    monkeypatch, rows, *, pnp_fallback=False, organizations=(), keep_parents=False,
):
    import spider_gd.infrastructure.network.usb_devices as usb

    projected = [
        {key: value for key, value in row.items() if keep_parents or key != "Parent"}
        for row in rows
    ]
    pnp_rows = [
        {
            "FriendlyName": row["Name"], "InstanceId": row["DeviceID"], "Class": row["PNPClass"],
            **({"Parent": row["Parent"]} if "Parent" in row else {}),
        }
        for row in projected
    ]
    calls = []

    def capture(command, timeout=20, **_kwargs):
        script = command[-1]
        calls.append((command, timeout))
        if command[0] == "pnputil":
            return subprocess.CompletedProcess(command, 1, "", "不可用")
        if "Get-ChildItem Cert:" in script:
            payload = [{"Subject": f"CN=脱敏测试, O={org}, C=CN"} for org in organizations]
        elif "Get-CimInstance" in script:
            if pnp_fallback:
                return subprocess.CompletedProcess(command, 1, "", "CIM 不可用")
            payload = projected
        else:
            assert "Get-PnpDevice -PresentOnly" in script
            payload = pnp_rows
        return subprocess.CompletedProcess(command, 0, json.dumps(payload, ensure_ascii=False), "")

    monkeypatch.setattr(usb, "run_capture", capture)
    monkeypatch.setattr(usb.sys, "platform", "win32")
    return calls


@pytest.mark.unit
@pytest.mark.parametrize("count", [2, 4])
def test_windows_list_collapses_location_interfaces_using_native_parents(monkeypatch, count):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(count)
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows)

    devices = usb.list_usb_devices()

    assert len(devices) == count
    assert all(device.ukey_candidate and device.serial == "" for device in devices)
    assert all(device.device_id.startswith("USB\\VID_096E") for device in devices)
    assert len({device.device_id for device in devices}) == count


@pytest.mark.unit
@pytest.mark.parametrize("pnp_fallback", [False, True])
def test_two_composite_ukeys_on_one_hub_are_two_visible_devices(monkeypatch, pnp_fallback):
    import spider_gd.infrastructure.network.usb_devices as usb

    hub = r"USB\VID_1234&PID_0001\7&ABC000&0&0"
    rows = [{"Name": "Generic USB Hub", "DeviceID": hub, "PNPClass": "USB"}]
    for index in range(2):
        root = rf"USB\VID_096E&PID_0317\7&ABC100&0&{index + 1}"
        rows.append({
            "Name": "USB Composite Device", "DeviceID": root,
            "PNPClass": "USB", "Parent": hub,
        })
        for interface in range(2):
            child = rf"USB\VID_096E&PID_0317&MI_0{interface}\8&DEF{index}00&0&000{interface}"
            rows.extend([
                {"Name": "USB 输入设备", "DeviceID": child, "PNPClass": "USB", "Parent": root},
                {
                    "Name": "符合 HID 标准的供应商定义设备",
                    "DeviceID": (
                        rf"HID\VID_096E&PID_0317&MI_0{interface}\9&FED{index}00&0&000{interface}"
                    ),
                    "PNPClass": "HIDClass", "Parent": child.lower(),
                },
            ])
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows, pnp_fallback=pnp_fallback)

    devices = usb.list_usb_devices()

    assert len(devices) == 2
    assert len({device.device_id for device in devices}) == 2
    assert all(device.ukey_candidate and not device.serial for device in devices)
    assert all(device.name == "USB 输入设备" for device in devices)
    assert {device.device_id for device in devices} == {
        r"USB\VID_096E&PID_0317\7&ABC100&0&1",
        r"USB\VID_096E&PID_0317\7&ABC100&0&2",
    }


def _serial_composite_ukeys() -> list[dict[str, str]]:
    hub = r"USB\VID_1234&PID_0001\7&ABC000&0&0"
    rows = [{"Name": "Generic USB Hub", "DeviceID": hub, "PNPClass": "USB"}]
    for index, serial in enumerate(("SN-TEST-A", "SN-TEST-B")):
        root = rf"USB\VID_096E&PID_0317\{serial}"
        rows.append({
            "Name": "USB Composite Device", "DeviceID": root,
            "PNPClass": "USB", "Parent": hub,
        })
        for interface in range(2):
            child = rf"USB\VID_096E&PID_0317&MI_0{interface}\8&DEF{index}00&0&000{interface}"
            rows.extend([
                {"Name": "USB 输入设备", "DeviceID": child, "PNPClass": "USB", "Parent": root},
                {
                    "Name": "符合 HID 标准的供应商定义设备",
                    "DeviceID": (
                        rf"HID\VID_096E&PID_0317&MI_0{interface}\9&FED{index}00&0&000{interface}"
                    ),
                    "PNPClass": "HIDClass", "Parent": child.lower(),
                },
            ])
    return rows


@pytest.mark.unit
@pytest.mark.parametrize("pnp_fallback", [False, True])
@pytest.mark.parametrize("seam", ["list", "probe", "gather"])
def test_composite_root_serial_reaches_list_probe_and_download(monkeypatch, pnp_fallback, seam):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _serial_composite_ukeys()
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows, pnp_fallback=pnp_fallback)

    if seam == "list":
        devices = [device.as_dict() for device in usb.list_usb_devices()]
    elif seam == "probe":
        report = usb.probe_ukeys()
        devices = report["ukeys"]
    else:
        report, _raw, warning = usb.gather_ukey_download_parts()
        devices = report["ukeys"]
        assert warning == ""

    assert sorted((device["id"], device["serial"]) for device in devices) == [
        (r"USB\VID_096E&PID_0317\SN-TEST-A", "SN-TEST-A"),
        (r"USB\VID_096E&PID_0317\SN-TEST-B", "SN-TEST-B"),
    ]
    assert all(device["name"] == "USB 输入设备" for device in devices)
    if seam != "list":
        assert len(report["devices"]) == 2
        assert report["distinguishable"] is True
        assert report["error"] == ""


@pytest.mark.unit
@pytest.mark.parametrize("pnp_fallback", [False, True])
@pytest.mark.parametrize("seam", ["list", "probe", "gather"])
def test_hexadecimal_location_suffix_is_not_a_stable_serial(monkeypatch, pnp_fallback, seam):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = [
        {
            key: value.replace(r"\SN-TEST-A", r"\7&ABC100&0&A").replace(
                r"\SN-TEST-B", r"\7&ABC100&0&B",
            )
            for key, value in row.items()
        }
        for row in _serial_composite_ukeys()
    ]
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows, pnp_fallback=pnp_fallback)

    if seam == "list":
        devices = [device.as_dict() for device in usb.list_usb_devices()]
    elif seam == "probe":
        report = usb.probe_ukeys()
        devices = report["ukeys"]
    else:
        report, _raw, _warning = usb.gather_ukey_download_parts()
        devices = report["ukeys"]

    assert len(devices) == 2
    assert all(device["serial"] == "" for device in devices)
    if seam != "list":
        assert report["distinguishable"] is False


@pytest.mark.unit
@pytest.mark.parametrize("pnp_fallback", [False, True])
@pytest.mark.parametrize("composite", [False, True])
def test_runtime_plan_keeps_legacy_interface_bindings_after_collapse(
    monkeypatch, pnp_fallback, composite,
):
    import spider_gd.infrastructure.network.usb_devices as usb
    from spider_gd.core.ukey_plan import collect_runtime_plan

    if composite:
        rows = _serial_composite_ukeys()
        usb_id = r"USB\VID_096E&PID_0317&MI_01\8&DEF000&0&0001"
        hid_id = r"HID\VID_096E&PID_0317&MI_01\9&FED100&0&0001"
        missing_usb = r"USB\VID_096E&PID_0317&MI_01\8&DEF200&0&0001"
        missing_hid = r"HID\VID_096E&PID_0317&MI_01\9&FED200&0&0001"
    else:
        rows = _location_ukeys(2)
        usb_id = r"USB\VID_096E&PID_0317\7&ABC100&0&1"
        hid_id = r"HID\VID_096E&PID_0317\8&DEF100&0&0002"
        missing_usb = r"USB\VID_096E&PID_0317\7&ABC100&0&3"
        missing_hid = r"HID\VID_096E&PID_0317\8&DEF100&0&0003"
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows, pnp_fallback=pnp_fallback)
    bindings = [
        ("usb", usb_id), ("hid", hid_id),
        ("missing-usb", missing_usb), ("missing-hid", missing_hid),
        ("hub", r"USB\VID_1234&PID_0001\7&ABC000&0&0"),
    ]
    if composite:
        bindings.append(("root", r"USB\VID_096E&PID_0317\SN-TEST-A"))
    stations = parse_station_profiles(json.dumps([
        {"id": station_id, "name": station_id, "wifiSsid": "TEST", "deviceId": device_id}
        for station_id, device_id in bindings
    ]))
    saved = dump_station_profiles(stations)

    devices = usb.list_usb_devices()
    plan = collect_runtime_plan(
        stations, selected_ids=[station.id for station in stations], mode="scheduled",
    )

    assert len(devices) == 2
    assert all(set(device.as_dict()) == {
        "id", "name", "label", "vid", "pid", "ukeyCandidate", "serial",
    } for device in devices)
    assert [station["id"] for station in plan.runnable] == (
        ["usb", "hid", "root"] if composite else ["usb", "hid"]
    )
    assert plan.started is True
    assert [(station.id, station.reason) for station in plan.skipped] == [
        ("missing-usb", "当前未插入"), ("missing-hid", "当前未插入"), ("hub", "当前未插入"),
    ]
    assert dump_station_profiles(stations) == saved


@pytest.mark.unit
def test_parent_binding_preserves_usb_representative_when_hid_is_listed_first():
    rows = list(reversed(_serial_composite_ukeys()))

    devices = parse_pnp_devices(json.dumps(rows))

    assert len(devices) == 2
    assert all(device.name == "USB 输入设备" for device in devices)
    assert {device.device_id for device in devices} == {
        r"USB\VID_096E&PID_0317\SN-TEST-A", r"USB\VID_096E&PID_0317\SN-TEST-B",
    }


@pytest.mark.unit
@pytest.mark.parametrize("pnp_fallback", [False, True])
def test_new_parent_binding_survives_child_interface_rebuild(monkeypatch, pnp_fallback):
    import spider_gd.infrastructure.network.usb_devices as usb
    from spider_gd.core.ukey_plan import collect_runtime_plan

    rows = _serial_composite_ukeys()
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows, pnp_fallback=pnp_fallback)
    selected = next(device for device in usb.list_usb_devices() if device.serial == "SN-TEST-A")
    stored = dump_station_profiles(parse_station_profiles(json.dumps([
        {
            "id": "selected", "name": "场站甲", "wifiSsid": "TEST",
            "deviceId": selected.as_dict()["id"],
        },
        {
            "id": "absent", "name": "场站乙", "wifiSsid": "TEST",
            "deviceId": r"USB\VID_096E&PID_0317\SN-TEST-C",
        },
    ])))
    rebuilt = [
        {key: value.replace("DEF000", "DEF999").replace("FED000", "FED999")
         for key, value in row.items()}
        for row in rows
    ]
    _mock_cfgmgr32(monkeypatch, rebuilt)
    _mock_windows_capture(monkeypatch, rebuilt, pnp_fallback=pnp_fallback)

    plan = collect_runtime_plan(
        parse_station_profiles(stored), selected_ids=["selected", "absent"], mode="scheduled",
    )

    assert [station["id"] for station in plan.runnable] == ["selected"]
    assert [(station.id, station.reason) for station in plan.skipped] == [("absent", "当前未插入")]
    assert selected.device_id == r"USB\VID_096E&PID_0317\SN-TEST-A"
    assert parse_station_profiles(stored)[0].device_id == selected.device_id


@pytest.mark.unit
@pytest.mark.parametrize("pnp_fallback", [False, True])
def test_runtime_plan_does_not_match_another_ukeys_serial_prefix(monkeypatch, pnp_fallback):
    from spider_gd.core.ukey_plan import collect_runtime_plan

    rows = _serial_composite_ukeys()[:6]
    rows = [
        {key: value.replace(r"\SN-TEST-A", r"\SN-TEST-AB") for key, value in row.items()}
        for row in rows
    ]
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows, pnp_fallback=pnp_fallback)
    stations = parse_station_profiles(json.dumps([
        {"id": station_id, "name": station_id, "wifiSsid": "TEST", "deviceId": device_id}
        for station_id, device_id in [
            ("missing-root", r"USB\VID_096E&PID_0317\SN-TEST-A"),
            ("missing-sn", "SN-TEST-A"),
            ("root", r"USB\VID_096E&PID_0317\SN-TEST-AB"),
            ("sn", "SN-TEST-AB"),
            ("usb", r"USB\VID_096E&PID_0317&MI_01\8&DEF000&0&0001"),
            ("hid", r"HID\VID_096E&PID_0317&MI_01\9&FED000&0&0001"),
        ]
    ]))

    plan = collect_runtime_plan(
        stations, selected_ids=[station.id for station in stations], mode="scheduled",
    )

    assert [station["id"] for station in plan.runnable] == ["root", "sn", "usb", "hid"]
    assert [(station.id, station.reason) for station in plan.skipped] == [
        ("missing-root", "当前未插入"), ("missing-sn", "当前未插入"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("pnp_fallback", [False, True])
def test_download_raw_and_report_share_enriched_parent_topology(monkeypatch, pnp_fallback):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    _mock_cfgmgr32(monkeypatch, rows)
    calls = _mock_windows_capture(monkeypatch, rows, pnp_fallback=pnp_fallback)

    report, raw, warning = usb.gather_ukey_download_parts()

    assert len(report["ukeys"]) == 2
    label = "Get-PnpDevice" if pnp_fallback else "Get-CimInstance"
    section = raw["usb"].split(f"===== {label} =====\n", 1)[1].split("\n\n===== ", 1)[0]
    assert len(parse_pnp_devices(section)) == 2
    captured = json.loads(section)
    assert len(captured) == 5
    children = [
        row for row in captured
        if str(row.get("InstanceId") or row.get("DeviceID")).startswith("HID\\")
    ]
    assert len(children) == 2
    assert all(row["Parent"].upper().startswith("USB\\VID_096E") for row in children)
    assert warning == ""
    assert all(timeout == 6 for _command, timeout in calls)
    assert all("Get-PnpDeviceProperty" not in " ".join(command) for command, _timeout in calls)


@pytest.mark.unit
def test_native_parent_reader_uses_unsigned_win32_abi_and_wide_buffers(monkeypatch):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    loader, dll = _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows)

    assert len(usb.list_usb_devices()) == 2
    loader.assert_called_once_with("cfgmgr32")
    dword = ctypes.c_uint32
    pdword = ctypes.POINTER(dword)
    assert ctypes.sizeof(dword) == 4
    assert dll.CM_Locate_DevNodeW.argtypes == [pdword, ctypes.c_wchar_p, dword]
    assert dll.CM_Get_Parent.argtypes == [pdword, dword, dword]
    assert dll.CM_Get_Device_ID_Size.argtypes == [pdword, dword, dword]
    assert dll.CM_Get_Device_IDW.argtypes == [dword, ctypes.POINTER(ctypes.c_wchar), dword, dword]
    for function in vars(dll).values():
        assert function.restype is dword
        assert function.called


@pytest.mark.unit
def test_native_parent_reader_does_not_start_another_probe_after_deadline(monkeypatch):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    loader, _dll = _mock_cfgmgr32(monkeypatch, rows)
    monkeypatch.setattr(usb.sys, "platform", "win32")

    assert usb._read_windows_parents([row["DeviceID"] for row in rows], deadline=0.0) == {}
    loader.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("stage", [0, 1, 2])
def test_windows_list_stops_native_parent_stages_when_budget_expires(monkeypatch, stage):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)[1:]
    _loader, dll = _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows)
    clock = [0.0]
    monkeypatch.setattr(usb.time, "monotonic", lambda: clock[0])
    functions = [
        dll.CM_Locate_DevNodeW, dll.CM_Get_Parent,
        dll.CM_Get_Device_ID_Size, dll.CM_Get_Device_IDW,
    ]
    original = functions[stage].side_effect

    def expire_budget(*args):
        result = original(*args)
        clock[0] = 7.0
        return result

    functions[stage].side_effect = expire_budget

    devices = usb.list_usb_devices()

    assert len(devices) == 4
    assert all(function.call_count == 1 for function in functions[:stage + 1])
    assert all(function.call_count == 0 for function in functions[stage + 1:])


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["locate", "parent", "size", "id"])
def test_one_native_parent_failure_keeps_only_that_interface_separate(monkeypatch, stage):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    target = rows[2]["DeviceID"] if stage in {"locate", "parent"} else rows[1]["DeviceID"]
    _mock_cfgmgr32(monkeypatch, rows, failure=(stage, target.upper()))
    _mock_windows_capture(monkeypatch, rows)

    devices = usb.list_usb_devices()

    assert len(devices) == 3
    assert {device.device_id for device in devices} == {
        rows[1]["DeviceID"], rows[2]["DeviceID"], rows[3]["DeviceID"],
    }
    assert all(device.serial == "" for device in devices)


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["missing", "load_failure", "all_failure"])
@pytest.mark.parametrize("count", [2, 4])
def test_without_native_parents_same_vid_pid_is_not_hardware_identity(monkeypatch, mode, count):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(count)
    loader, dll = _mock_cfgmgr32(monkeypatch, rows)
    if mode == "missing":
        monkeypatch.delattr(ctypes, "WinDLL")
    elif mode == "load_failure":
        loader.side_effect = OSError("cfgmgr32 不可用")
    else:
        dll.CM_Locate_DevNodeW.side_effect = None
        dll.CM_Locate_DevNodeW.return_value = 13
    _mock_windows_capture(monkeypatch, rows)

    devices = usb.list_usb_devices()

    assert len(devices) == count * 2
    assert all(device.ukey_candidate and device.serial == "" for device in devices)


@pytest.mark.unit
@pytest.mark.parametrize("pnp_fallback", [False, True])
def test_existing_parents_survive_native_failure_in_download(monkeypatch, pnp_fallback):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    loader, dll = _mock_cfgmgr32(monkeypatch, rows)
    loader.side_effect = OSError("cfgmgr32 不可用")
    _mock_windows_capture(monkeypatch, rows, pnp_fallback=pnp_fallback, keep_parents=True)

    report, raw, warning = usb.gather_ukey_download_parts()

    assert len(report["ukeys"]) == 2
    label = "Get-PnpDevice" if pnp_fallback else "Get-CimInstance"
    section = raw["usb"].split(f"===== {label} =====\n", 1)[1].split("\n\n===== ", 1)[0]
    captured = json.loads(section)
    for original, row in zip(rows, captured, strict=True):
        assert row.get("Parent") == original.get("Parent")
    assert len(parse_pnp_devices(section)) == 2
    assert not dll.CM_Locate_DevNodeW.called
    assert warning == ""


@pytest.mark.unit
def test_stable_serial_interfaces_deduplicate_even_without_native_parents(monkeypatch):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    for row in rows[1:]:
        row["DeviceID"] = row["DeviceID"].rsplit("\\", 1)[0] + (
            "\\SN-TEST-A" if row in rows[1:3] else "\\SN-TEST-B"
        )
    loader, _dll = _mock_cfgmgr32(monkeypatch, rows)
    loader.side_effect = OSError("cfgmgr32 不可用")
    _mock_windows_capture(monkeypatch, rows)

    devices = usb.list_usb_devices()

    assert len(devices) == 2
    assert {device.serial for device in devices} == {"SN-TEST-A", "SN-TEST-B"}


@pytest.mark.unit
@pytest.mark.parametrize(("name", "kind", "vid"), [
    ("Goodix Fingerprint USB Device", "USB", "27C6"),
    ("USB 指纹设备", "USB", "27C6"),
    ("USB 设备", "bIoMeTrIc", "27C6"),
    ("USB 设备", "Biometric", "096E"),
])
def test_windows_list_excludes_synthetic_fingerprint_and_biometric(monkeypatch, name, kind, vid):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    rows.append({
        "Name": name, "DeviceID": rf"USB\VID_{vid}&PID_1234\7&ABC200&0&1",
        "PNPClass": kind, "ConfigManagerErrorCode": 0,
    })
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows)

    devices = usb.list_usb_devices()

    assert len(devices) == 2
    assert all(device.ukey_candidate and device.pid == "0317" for device in devices)


@pytest.mark.unit
def test_download_keeps_external_usb_and_raw_fingerprint_diagnostics(monkeypatch):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    external = [
        ("USB 无线网卡", "0BDA", "Net"),
        ("USB 大容量存储设备", "1234", "USB"),
        ("USB 键盘", "2345", "Keyboard"),
        ("USB 鼠标", "3456", "Mouse"),
        ("其他 USB 安全设备", "1050", "USB"),
    ]
    for index, (name, vid, kind) in enumerate(external):
        rows.append({
            "Name": name, "DeviceID": rf"USB\VID_{vid}&PID_4321\7&ABC300&0&{index + 1}",
            "PNPClass": kind, "ConfigManagerErrorCode": 0,
        })
    rows.append({
        "Name": "Goodix Fingerprint USB Device",
        "DeviceID": r"USB\VID_27C6&PID_1234\7&ABC200&0&1", "PNPClass": "Biometric",
        "ConfigManagerErrorCode": 0,
    })
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows)

    report, raw, warning = usb.gather_ukey_download_parts()

    assert len(report["ukeys"]) == 2
    assert len(report["devices"]) == 7
    assert {device["name"] for device in report["devices"] if not device["ukeyCandidate"]} == {
        name for name, _vid, _kind in external
    }
    assert "Goodix Fingerprint USB Device" in raw["usb"]
    assert warning == ""


@pytest.mark.unit
@pytest.mark.parametrize(("count", "org_count"), [(2, 2), (4, 4), (1, 2), (0, 4)])
def test_probe_certificate_units_do_not_make_no_serial_hardware_distinguishable(
    monkeypatch, count, org_count,
):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(count)
    _mock_cfgmgr32(monkeypatch, rows)
    organizations = [f"脱敏单位{index + 1}" for index in range(org_count)]
    _mock_windows_capture(monkeypatch, rows, organizations=organizations)

    report = usb.probe_ukeys()

    assert len(report["ukeys"]) == count
    assert len(report["certificates"]) == org_count
    assert all(device["serial"] == "" for device in report["ukeys"])
    assert report["distinguishable"] is False


@pytest.mark.unit
@pytest.mark.parametrize("count", [1, 2, 4])
def test_download_summary_separates_port_selection_from_certificate_matching(monkeypatch, count):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(count)
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows, organizations=["脱敏单位甲", "脱敏单位乙"])

    report, _raw, warning = usb.gather_ukey_download_parts()

    summary = report["summary"]
    assert "可以选择当前插口特征码" in summary
    assert "换 USB 口后可能改变" in summary
    assert "证书窗口匹配" in summary
    assert "不会自动关联到实体 UKey" in summary
    assert report["distinguishable"] is False
    assert warning == ""


@pytest.mark.unit
@pytest.mark.parametrize(("serials", "expected_count", "distinguishable"), [
    (("SN-TEST-A", "SN-TEST-B"), 2, True),
    (("SN-TEST-A", "SN-TEST-A"), 1, False),
    (("SN-TEST-A", ""), 2, False),
])
@pytest.mark.parametrize("organizations", [[], ["脱敏单位甲", "脱敏单位乙"]])
def test_probe_requires_two_distinct_real_serials(
    monkeypatch, serials, expected_count, distinguishable, organizations,
):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    for index, serial in enumerate(serials):
        if not serial:
            continue
        root, child = rows[index * 2 + 1:index * 2 + 3]
        root["DeviceID"] = rf"USB\VID_096E&PID_0317\{serial}"
        child["DeviceID"] = rf"HID\VID_096E&PID_0317\{serial}"
        child["Parent"] = root["DeviceID"]
    _mock_cfgmgr32(monkeypatch, rows)
    _mock_windows_capture(monkeypatch, rows, organizations=organizations)

    report = usb.probe_ukeys()

    assert len(report["ukeys"]) == expected_count
    assert report["distinguishable"] is distinguishable
    assert ("序列号各不相同，可以" in report["summary"]) is distinguishable


@pytest.mark.unit
@pytest.mark.parametrize("all_fail", [False, True])
def test_native_failures_keep_download_raw_records_and_conservative_report(monkeypatch, all_fail):
    import spider_gd.infrastructure.network.usb_devices as usb

    rows = _location_ukeys(2)
    _loader, dll = _mock_cfgmgr32(
        monkeypatch, rows, failure=("locate", rows[2]["DeviceID"].upper()),
    )
    if all_fail:
        dll.CM_Locate_DevNodeW.side_effect = None
        dll.CM_Locate_DevNodeW.return_value = 13
    _mock_windows_capture(monkeypatch, rows)

    report, raw, warning = usb.gather_ukey_download_parts()

    expected_count = 4 if all_fail else 3
    assert len(report["ukeys"]) == expected_count
    section = raw["usb"].split("===== Get-CimInstance =====\n", 1)[1].split("\n\n===== ", 1)[0]
    captured = json.loads(section)
    assert len(captured) == 5
    assert len(parse_pnp_devices(section)) == expected_count
    assert all(device["serial"] == "" for device in report["ukeys"])
    assert "Parent" not in captured[2]
    assert warning == ""


@pytest.mark.unit
def test_download_preserves_existing_parent_case_while_enriching_other_rows(monkeypatch):
    import spider_gd.infrastructure.network.usb_devices as usb

    topology = _location_ukeys(2)
    _mock_cfgmgr32(monkeypatch, topology)
    rows = [dict(row) for row in topology]
    for row in rows[:4]:
        row.pop("Parent", None)
    _mock_windows_capture(monkeypatch, rows, keep_parents=True)

    report, raw, warning = usb.gather_ukey_download_parts()

    assert len(report["ukeys"]) == 2
    section = raw["usb"].split("===== Get-CimInstance =====\n", 1)[1].split("\n\n===== ", 1)[0]
    captured = json.loads(section)
    assert captured[4]["Parent"] == topology[4]["Parent"]
    assert all(captured[index]["Parent"] for index in range(1, 5))
    assert warning == ""
