"""测试双网络连通性验证（PowerShell 调用 Mock）。"""

from __future__ import annotations

import pytest

from spider_gd.infrastructure.network import network_validator
from spider_gd.infrastructure.network.network_validator import NetworkValidator


@pytest.mark.unit
def test_validate_all_pass(mocker):
    mocker.patch.object(network_validator, "run_ps", side_effect=["True", "True"])
    mocker.patch.object(
        network_validator,
        "run_ps_json",
        return_value=[{"DestinationPrefix": "36.137.0.12/32"}],
    )
    report = NetworkValidator().validate("pm.gd.csg.cn", ["36.137.0.12"])
    assert report.passed is True


@pytest.mark.unit
def test_validate_fails_when_route_missing(mocker):
    mocker.patch.object(network_validator, "run_ps", side_effect=["True", "True"])
    mocker.patch.object(network_validator, "run_ps_json", return_value=[])
    report = NetworkValidator().validate("pm.gd.csg.cn", ["36.137.0.12"])
    assert report.routes_ok is False
    assert report.passed is False


@pytest.mark.unit
def test_validate_fails_when_private_unreachable(mocker):
    mocker.patch.object(network_validator, "run_ps", side_effect=["False", "True"])
    mocker.patch.object(network_validator, "run_ps_json", return_value=[])
    report = NetworkValidator().validate("pm.gd.csg.cn", [])
    assert report.private_ok is False
    assert report.passed is False


@pytest.mark.unit
def test_validate_route_must_match_private_interface(mocker):
    mocker.patch.object(network_validator, "run_ps", side_effect=["True", "True"])
    mocker.patch.object(
        network_validator,
        "run_ps_json",
        return_value=[
            {"DestinationPrefix": "36.137.0.12/32", "InterfaceAlias": "以太网"},
        ],
    )
    report = NetworkValidator().validate(
        "pm.gd.csg.cn",
        ["36.137.0.12"],
        private_iface="Wi-Fi",
    )
    assert report.private_ok is True
    assert report.routes_ok is False
    assert report.passed is False
