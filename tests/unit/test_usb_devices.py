"""USB 设备列表：同名设备和飞天 UKey 要能分开选。"""

from __future__ import annotations

import json
import subprocess
import zipfile

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
