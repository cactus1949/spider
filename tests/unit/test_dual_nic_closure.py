"""测试双网卡动态闭环编排。"""

from __future__ import annotations

from pathlib import Path

import pytest

from spider_gd.infrastructure.network.dual_nic_closure import DualNicClosure
from spider_gd.interfaces.network import ValidateReport


class FakeResolver:
    def __init__(self, domain: str, cache_path: Path, wifi_iface: str) -> None:
        self.domain = domain
        self.cache_path = cache_path
        self.wifi_iface = wifi_iface
        self._cached_ips = ["36.137.0.12"]

    @property
    def cached_ips(self) -> list[str]:
        return self._cached_ips

    def resolve(self) -> list[str]:
        return ["36.137.0.13"]


class FakeRouteWriter:
    calls: list[dict] = []

    def __init__(self, wifi_iface: str, wan_iface: str = "以太网") -> None:
        self.wifi_iface = wifi_iface
        self.wan_iface = wan_iface
        self.last_gateway = "10.88.0.1"

    def configure(self, target_ips: list[str], *, stale_ips: list[str] | None = None) -> bool:
        self.calls.append({
            "wifi_iface": self.wifi_iface,
            "target_ips": target_ips,
            "stale_ips": stale_ips or [],
        })
        return True


class FakeValidator:
    def validate(self, target_domain: str, target_ips: list[str], **kwargs) -> ValidateReport:
        self.last = {
            "target_domain": target_domain,
            "target_ips": target_ips,
            **kwargs,
        }
        return ValidateReport(public_ok=True, private_ok=True, routes_ok=True)


class PublicDownValidator:
    def validate(self, target_domain: str, target_ips: list[str], **kwargs) -> ValidateReport:
        return ValidateReport(public_ok=False, private_ok=True, routes_ok=True)


@pytest.mark.unit
def test_dual_nic_closure_resolves_cleans_routes_and_validates(tmp_path: Path):
    FakeRouteWriter.calls = []
    validator = FakeValidator()
    closure = DualNicClosure(
        cache_dir=tmp_path,
        resolver_factory=FakeResolver,
        route_writer_factory=FakeRouteWriter,
        validator=validator,
    )

    report = closure.apply_rules([
        {"domain_pattern": "pm.gd.csg.cn", "interface_name": "UKey-WiFi", "enabled": True},
    ])

    assert report.status == "ok"
    assert report.applied == 1
    assert report.results[0].resolved_ips == ["36.137.0.13"]
    assert report.results[0].stale_ips == ["36.137.0.12"]
    assert report.results[0].gateway == "10.88.0.1"
    assert FakeRouteWriter.calls == [{
        "wifi_iface": "UKey-WiFi",
        "target_ips": ["36.137.0.13"],
        "stale_ips": ["36.137.0.12"],
    }]
    assert validator.last["private_iface"] == "UKey-WiFi"
    assert validator.last["target_ips"] == ["36.137.0.13"]


@pytest.mark.unit
def test_dual_nic_closure_degrades_when_only_public_is_down(tmp_path: Path):
    FakeRouteWriter.calls = []
    closure = DualNicClosure(
        cache_dir=tmp_path,
        resolver_factory=FakeResolver,
        route_writer_factory=FakeRouteWriter,
        validator=PublicDownValidator(),
    )

    report = closure.apply_rules([
        {"domain_pattern": "pm.gd.csg.cn", "interface_name": "UKey-WiFi", "enabled": True},
    ])

    assert report.status == "degraded"
    assert report.results[0].status == "degraded"
    assert report.results[0].passed is True
    assert report.results[0].private_ok is True
    assert report.results[0].public_ok is False
    assert "允许采集落本地" in report.message


@pytest.mark.unit
def test_dual_nic_closure_checks_expected_ssid(tmp_path: Path, mocker):
    mocker.patch(
        "spider_gd.infrastructure.network.dual_nic_closure.get_connected_ssid",
        return_value="Wrong-WiFi",
    )
    closure = DualNicClosure(
        cache_dir=tmp_path,
        resolver_factory=FakeResolver,
        route_writer_factory=FakeRouteWriter,
        validator=FakeValidator(),
    )

    report = closure.apply_rules([
        {
            "domain_pattern": "pm.gd.csg.cn",
            "interface_name": "UKey-WiFi",
            "expected_ssid": "CSG-UKEY",
            "enabled": True,
        },
    ])

    assert report.status == "error"
    assert report.results[0].current_ssid == "Wrong-WiFi"
    assert "期望 CSG-UKEY" in report.results[0].error


@pytest.mark.unit
def test_dual_nic_closure_reports_route_permission_failure(tmp_path: Path):
    class DeniedWriter(FakeRouteWriter):
        def configure(self, target_ips: list[str], *, stale_ips: list[str] | None = None) -> bool:
            raise PermissionError("路由配置需要管理员权限")

    closure = DualNicClosure(
        cache_dir=tmp_path,
        resolver_factory=FakeResolver,
        route_writer_factory=DeniedWriter,
        validator=FakeValidator(),
    )

    report = closure.apply_rules([
        {"domain_pattern": "pm.gd.csg.cn", "interface_name": "UKey-WiFi", "enabled": True},
    ])

    assert report.status == "error"
    assert report.applied == 0
    assert "管理员权限" in report.results[0].error
