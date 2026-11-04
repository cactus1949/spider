"""测试网络诊断的前后快照对比推断。"""

from __future__ import annotations

import pytest

from spider_gd.infrastructure.network.diagnoser import NetSnapshot, diagnose


def _snap(routes):
    return NetSnapshot(timestamp="t", default_routes=routes)


@pytest.mark.unit
def test_diagnose_detects_wifi_default_gateway_seizure():
    before = _snap([{"InterfaceAlias": "以太网", "NextHop": "192.168.1.1", "RouteMetric": 10}])
    after = _snap(
        [
            {"InterfaceAlias": "以太网", "NextHop": "192.168.1.1", "RouteMetric": 10},
            {"InterfaceAlias": "Wi-Fi", "NextHop": "192.168.44.1", "RouteMetric": 25},
        ]
    )
    report = diagnose(before, after, wifi_iface="Wi-Fi")
    assert report.conclusion == "WiFi 默认网关抢占"
    assert "删除 WiFi 默认路由" in report.fix_actions


@pytest.mark.unit
def test_diagnose_detects_metric_seizure():
    before = _snap([{"InterfaceAlias": "以太网", "NextHop": "192.168.1.1", "RouteMetric": 10}])
    after = _snap([{"InterfaceAlias": "以太网", "NextHop": "192.168.1.1", "RouteMetric": 35}])
    report = diagnose(before, after, wifi_iface="Wi-Fi")
    assert report.conclusion == "metric 被 U-Key 抢占"


@pytest.mark.unit
def test_diagnose_no_seizure_still_adds_pronet_route():
    before = _snap([{"InterfaceAlias": "以太网", "NextHop": "192.168.1.1", "RouteMetric": 10}])
    after = _snap([{"InterfaceAlias": "以太网", "NextHop": "192.168.1.1", "RouteMetric": 10}])
    report = diagnose(before, after, wifi_iface="Wi-Fi")
    assert "未检测到明显抢占" in report.conclusion
    assert any("/32" in a for a in report.fix_actions)
