"""测试健康检查聚合、状态汇总与告警触发去重。"""

from __future__ import annotations

import time

import pytest
from loguru import logger

from spider_gd.infrastructure.monitor.alert_trigger import AlertTrigger
from spider_gd.infrastructure.monitor.health_checker import (
    HealthChecker,
    _load_ukey_devices,
    _parse_powershell_pnp,
    classify_ukey_devices,
    disk_space_check,
    ukey_check,
)
from spider_gd.infrastructure.monitor.status_reporter import build_status
from spider_gd.interfaces.alert import AlertResult
from spider_gd.interfaces.monitor import HealthLevel, HealthReport


def _report(module: str, level: HealthLevel) -> HealthReport:
    return HealthReport(module=module, level=level, message="m", timestamp="t")


async def _ok_check():
    return _report("a", HealthLevel.OK)


async def _err_check():
    return _report("b", HealthLevel.ERROR)


@pytest.mark.unit
async def test_overall_health_is_worst_level():
    hc = HealthChecker()
    hc.add_check("a", _ok_check)
    hc.add_check("b", _err_check)
    overall = await hc.get_overall_health()
    assert overall.level == HealthLevel.ERROR


@pytest.mark.unit
async def test_check_exception_becomes_error_report():
    hc = HealthChecker()

    async def boom():
        raise RuntimeError("x")

    hc.add_check("boom", boom)
    reports = await hc.check_all()
    assert reports[0].level == HealthLevel.ERROR


@pytest.mark.unit
async def test_disk_space_check_ok():
    report = await disk_space_check(".", min_free_gb=0.0)()
    assert report.level == HealthLevel.OK


@pytest.mark.unit
def test_build_status_maps_overall():
    overall = _report("system", HealthLevel.CRITICAL)
    status = build_status(overall, [_report("disk", HealthLevel.CRITICAL)])
    assert status["overall"] == "critical"
    assert status["checks"][0]["module"] == "disk"


class _SpyGateway:
    def __init__(self):
        self.calls = []

    async def send_alert(self, level, message, event_type="info"):
        self.calls.append((level, message, event_type))
        return AlertResult(sent=True)

    def is_in_silence_window(self):
        return False


@pytest.mark.unit
async def test_alert_trigger_dedupes_repeated_alerts():
    gw = _SpyGateway()
    trigger = AlertTrigger(gw)
    reports = [_report("wifi", HealthLevel.ERROR), _report("disk", HealthLevel.OK)]

    sent1 = await trigger.process(reports)
    sent2 = await trigger.process(reports)  # 同样的告警应被去重
    assert sent1 == 1
    assert sent2 == 0
    assert len(gw.calls) == 1


@pytest.mark.unit
async def test_alert_trigger_skips_ok():
    gw = _SpyGateway()
    trigger = AlertTrigger(gw)
    sent = await trigger.process([_report("disk", HealthLevel.OK)])
    assert sent == 0
    assert gw.calls == []


def _ukey(vid_pid: str, error_code: int = 0, name: str = "feitian") -> dict[str, object]:
    vid, pid = vid_pid.split(":")
    return {
        "vid_pid": vid_pid,
        "desc": "token",
        "name": name,
        "class": "USB",
        "error_code": error_code,
        "device_id": f"USB\\VID_{vid}&PID_{pid}\\A",
    }


@pytest.mark.unit
def test_driver_error_is_reported_instead_of_filtered():
    report = classify_ukey_devices([_ukey("096E:0317", 28)])
    assert report.level == HealthLevel.WARN
    assert "plugged in but driver error (code 28)" in report.message
    assert report.metadata["devices"][0]["error_code"] == 28


@pytest.mark.unit
def test_healthy_ukey_stays_ok_and_unrelated_usb_is_warn():
    ok = classify_ukey_devices([_ukey("096E:0317")])
    assert ok.level == HealthLevel.OK
    assert "096E:0317" in ok.message
    missing = classify_ukey_devices([
        {
            "vid_pid": "1234:5678",
            "desc": "mass storage",
            "name": "disk",
            "class": "DiskDrive",
            "error_code": 28,
            "device_id": r"USB\VID_1234&PID_5678\DISK",
        }
    ])
    assert missing.level == HealthLevel.WARN
    assert "未检测到 U-Key" in missing.message
    assert "driver error" not in missing.message


@pytest.mark.unit
def test_specified_vid_with_driver_error_is_not_reported_missing():
    report = classify_ukey_devices([_ukey("1234:5678", 10, name="disk")], vid_pid="1234:5678")
    assert report.level == HealthLevel.WARN
    assert "plugged in but driver error (code 10)" in report.message


@pytest.mark.unit
def test_powershell_problem_code_is_kept():
    raw = '[{"FriendlyName":"token","InstanceId":"USB\\\\VID_096E&PID_0317\\\\A","Problem":28}]'
    rows = _parse_powershell_pnp(raw)
    assert rows[0]["vid_pid"] == "096E:0317"
    assert rows[0]["error_code"] == 28


@pytest.mark.unit
def test_cim_instance_json_keeps_driver_error_code():
    raw = (
        '{"Name":"token","Description":"feitian","DeviceID":"USB\\\\VID_096E&PID_0317\\\\A",'
        '"PNPClass":"USB","ConfigManagerErrorCode":28}'
    )
    rows = _parse_powershell_pnp(raw)
    assert rows[0]["vid_pid"] == "096E:0317"
    assert rows[0]["error_code"] == 28
    assert rows[0]["name"] == "token"


@pytest.mark.unit
def test_wmi_import_error_is_logged_and_falls_back(monkeypatch):
    monkeypatch.setattr(
        "spider_gd.infrastructure.monitor.health_checker._query_wmi_devices",
        lambda: (_ for _ in ()).throw(ImportError("no wmi")),
    )
    monkeypatch.setattr(
        "spider_gd.infrastructure.monitor.health_checker._query_powershell_devices",
        lambda: [_ukey("096E:0317")],
    )
    monkeypatch.setattr("spider_gd.infrastructure.monitor.health_checker.sys.platform", "win32")
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        devices = _load_ukey_devices()
    finally:
        logger.remove(sink)
    assert devices[0]["vid_pid"] == "096E:0317"
    assert any("WMI" in item for item in messages)


@pytest.mark.unit
async def test_ukey_check_timeout_does_not_report_success():
    def hang() -> list[dict[str, object]]:
        time.sleep(0.4)
        return []

    report = await ukey_check(query=hang, timeout=0.05)()
    assert report.level == HealthLevel.WARN
    assert "超时" in report.message
    time.sleep(0.5)


@pytest.mark.unit
async def test_ukey_check_uses_injected_devices():
    report = await ukey_check(query=lambda: [_ukey("096E:0317", 28)])()
    assert "code 28" in report.message
