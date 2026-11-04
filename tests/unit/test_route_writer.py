"""测试路由配置命令拼接与权限校验（subprocess 全部 Mock）。"""

from __future__ import annotations

import subprocess

import pytest

from spider_gd.infrastructure.network import route_writer
from spider_gd.infrastructure.network.route_writer import RouteWriter


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.unit
def test_configure_requires_admin(mocker):
    mocker.patch.object(route_writer, "is_admin", return_value=False)
    rw = RouteWriter()
    with pytest.raises(PermissionError, match="管理员"):
        rw.configure(["10.0.0.1"])


@pytest.mark.unit
def test_configure_builds_correct_commands(mocker):
    mocker.patch.object(route_writer, "is_admin", return_value=True)
    calls: list[list[str]] = []

    def fake_run(args, timeout=30):
        calls.append(args)
        if "IPv4DefaultGateway" in args[-1]:
            return _completed(stdout="192.168.44.1")
        return _completed(returncode=0)

    mocker.patch.object(route_writer, "_run", side_effect=fake_run)

    rw = RouteWriter(wifi_iface="Wi-Fi")
    ok = rw.configure(["36.137.0.12", "36.137.0.13"])

    assert ok is True
    joined = " ".join(c[-1] for c in calls)
    assert "Remove-NetRoute" in joined  # 删除 WiFi 默认路由
    assert "36.137.0.12/32" in joined
    assert "36.137.0.13/32" in joined
    assert "192.168.44.1" in joined  # 下一跳为 WiFi 网关
    assert rw.last_gateway == "192.168.44.1"


@pytest.mark.unit
def test_configure_returns_false_when_add_fails(mocker):
    mocker.patch.object(route_writer, "is_admin", return_value=True)

    def fake_run(args, timeout=30):
        if "IPv4DefaultGateway" in args[-1]:
            return _completed(stdout="192.168.44.1")
        if "New-NetRoute" in args[-1]:
            return _completed(returncode=1, stderr="路由冲突")
        return _completed()

    mocker.patch.object(route_writer, "_run", side_effect=fake_run)
    assert RouteWriter().configure(["10.0.0.1"]) is False


@pytest.mark.unit
def test_configure_cleans_stale_dynamic_ips(mocker):
    mocker.patch.object(route_writer, "is_admin", return_value=True)
    calls: list[list[str]] = []

    def fake_run(args, timeout=30):
        calls.append(args)
        if "IPv4DefaultGateway" in args[-1]:
            return _completed(stdout="192.168.44.1")
        return _completed()

    mocker.patch.object(route_writer, "_run", side_effect=fake_run)
    RouteWriter().configure(["36.137.0.13"], stale_ips=["36.137.0.12"])

    joined = " ".join(c[-1] for c in calls)
    assert 'DestinationPrefix "36.137.0.12/32"' in joined
    assert 'DestinationPrefix "36.137.0.13/32"' in joined
    assert 'New-NetRoute -DestinationPrefix "36.137.0.13/32"' in joined


@pytest.mark.unit
def test_cleanup_removes_routes(mocker):
    calls: list[list[str]] = []
    mocker.patch.object(
        route_writer,
        "_run",
        side_effect=lambda args, timeout=30: calls.append(args) or _completed(),
    )
    RouteWriter().cleanup(["10.0.0.1", "10.0.0.2"])
    joined = " ".join(c[-1] for c in calls)
    assert "10.0.0.1/32" in joined
    assert "10.0.0.2/32" in joined
