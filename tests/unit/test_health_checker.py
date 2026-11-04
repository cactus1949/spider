"""测试健康检查聚合、状态汇总与告警触发去重。"""

from __future__ import annotations

import pytest

from spider_gd.infrastructure.monitor.alert_trigger import AlertTrigger
from spider_gd.infrastructure.monitor.health_checker import HealthChecker, disk_space_check
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
