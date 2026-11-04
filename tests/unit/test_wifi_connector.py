from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from spider_gd.infrastructure.network.wifi_connector import (
    WifiConnectOptions,
    WifiConnector,
    decode_ssid_name,
)


def _is_profile_connect(args: list[str]) -> bool:
    return (
        args[:3] == ["netsh", "wlan", "connect"]
        and args[3].startswith("name=spider-gd-")
        and not any(part.startswith("ssid=") for part in args)
    )


def _completed(cmd: Sequence[str], stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(list(cmd), returncode, stdout=stdout, stderr=stderr)


@pytest.mark.unit
def test_wifi_connector_skips_when_already_connected_windows():
    commands: list[list[str]] = []

    def runner(cmd: Sequence[str], **_: Any):
        commands.append(list(cmd))
        return _completed(cmd, stdout="Name : Wi-Fi\nSSID : CSG-UKEY\n")

    result = WifiConnector(platform_name="Windows", runner=runner).prepare(
        WifiConnectOptions(ssid="CSG-UKEY", interface_name="Wi-Fi")
    )

    assert result.ok is True
    assert result.changed is False
    assert commands == [["netsh", "wlan", "show", "interfaces"]]


@pytest.mark.unit
def test_wifi_connector_current_ssid_reports_missing_windows_stdout():
    def runner(cmd: Sequence[str], **_: Any):
        return subprocess.CompletedProcess(list(cmd), 0, stdout=None, stderr=None)

    connector = WifiConnector(platform_name="Windows", runner=runner)

    with pytest.raises(RuntimeError, match="命令未返回输出"):
        connector.current_ssid()


@pytest.mark.unit
def test_wifi_connector_decodes_binary_netsh_output():
    """后端按二进制捕获输出：cp936 的中文表头不能把整段输出丢掉。"""

    def runner(cmd: Sequence[str], **_: Any):
        raw = (
            "\r\n系统上有 1 个接口:\r\n\r\n"
            "    Name                   : WLAN\r\n"
            "    State                  : connected\r\n"
            "    SSID                   : GuestNetwork\r\n"
        ).encode("cp936")
        return subprocess.CompletedProcess(list(cmd), 0, stdout=raw, stderr=b"")

    ssid = WifiConnector(platform_name="Windows", runner=runner).current_ssid()

    assert ssid == "GuestNetwork"


@pytest.mark.unit
def test_wifi_connector_current_ssid_reports_failed_windows_command():
    def runner(cmd: Sequence[str], **_: Any):
        return subprocess.CompletedProcess(
            list(cmd), 1, stdout=None, stderr="WLAN AutoConfig unavailable"
        )

    connector = WifiConnector(platform_name="Windows", runner=runner)

    with pytest.raises(RuntimeError, match="WLAN AutoConfig unavailable"):
        connector.current_ssid()


@pytest.mark.unit
def test_wifi_connector_adds_windows_profile_without_password_in_command():
    connected = False
    commands: list[list[str]] = []

    def runner(cmd: Sequence[str], **_: Any):
        nonlocal connected
        args = list(cmd)
        commands.append(args)
        if args[:4] == ["netsh", "wlan", "show", "interfaces"]:
            ssid = "CSG-UKEY" if connected else "Other"
            return _completed(cmd, stdout=f"Name : Wi-Fi\nSSID : {ssid}\n")
        if args[:4] == ["netsh", "wlan", "add", "profile"]:
            return _completed(cmd, stdout="Profile added")
        if _is_profile_connect(args):
            connected = True
            return _completed(cmd, stdout="Connection request was completed")
        return _completed(cmd, returncode=1)

    sleeps: list[float] = []
    result = WifiConnector(
        platform_name="Windows",
        runner=runner,
        sleeper=sleeps.append,
        poll_interval_sec=0.1,
    ).prepare(
        WifiConnectOptions(
            ssid="CSG-UKEY",
            password="secret-pass",
            interface_name="Wi-Fi",
            timeout_sec=1,
        )
    )

    assert result.ok is True
    assert result.changed is True
    assert sleeps == [3.0]
    assert result.current_ssid == "CSG-UKEY"
    assert any(args[:4] == ["netsh", "wlan", "add", "profile"] for args in commands)
    connect = next(args for args in commands if args[:3] == ["netsh", "wlan", "connect"])
    assert connect[3].startswith("name=spider-gd-")
    assert not any(part.startswith("ssid=") for part in connect)
    assert "secret-pass" not in " ".join(" ".join(args) for args in commands)


@pytest.mark.unit
def test_wifi_connector_reuses_existing_windows_group_policy_profile():
    connected = False
    commands: list[list[str]] = []

    def runner(cmd: Sequence[str], **_: Any):
        nonlocal connected
        args = list(cmd)
        commands.append(args)
        if args[:4] == ["netsh", "wlan", "show", "interfaces"]:
            ssid = "CSG-UKEY" if connected else "Other"
            return _completed(cmd, stdout=f"Name : Wi-Fi\nSSID : {ssid}\n")
        if args[:4] == ["netsh", "wlan", "add", "profile"]:
            return _completed(
                cmd,
                returncode=1,
                stdout="无法添加配置文件。具有此名称的配置文件已存在于组策略或其他用户范围内，且无法覆盖",
            )
        if _is_profile_connect(args):
            connected = True
            return _completed(cmd, stdout="Connection request was completed")
        return _completed(cmd, returncode=1)

    result = WifiConnector(
        platform_name="Windows",
        runner=runner,
        sleeper=lambda _: None,
        poll_interval_sec=0.1,
    ).prepare(
        WifiConnectOptions(
            ssid="CSG-UKEY",
            password="secret-pass",
            interface_name="Wi-Fi",
            timeout_sec=1,
        )
    )

    assert result.ok is True
    assert any("复用现有配置" in item for item in result.details)
    assert any(_is_profile_connect(args) for args in commands)


@pytest.mark.unit
def test_wifi_connector_uses_macos_networksetup():
    connected = False
    commands: list[list[str]] = []

    def runner(cmd: Sequence[str], **_: Any):
        nonlocal connected
        args = list(cmd)
        commands.append(args)
        if args == ["networksetup", "-getairportnetwork", "en0"]:
            ssid = "CSG-UKEY" if connected else "Other"
            return _completed(cmd, stdout=f"Current Wi-Fi Network: {ssid}\n")
        if args[:3] == ["networksetup", "-setairportnetwork", "en0"]:
            connected = True
            return _completed(cmd)
        return _completed(cmd, returncode=1)

    result = WifiConnector(
        platform_name="Darwin",
        runner=runner,
        sleeper=lambda _: None,
        poll_interval_sec=0.1,
    ).prepare(
        WifiConnectOptions(
            ssid="CSG-UKEY",
            password="secret-pass",
            interface_name="en0",
            timeout_sec=1,
        )
    )

    assert result.ok is True
    assert ["networksetup", "-setairportnetwork", "en0", "CSG-UKEY", "secret-pass"] in commands


@pytest.mark.unit
def test_wifi_connector_rejects_unsupported_platform():
    result = WifiConnector(platform_name="Plan9").prepare(
        WifiConnectOptions(ssid="CSG-UKEY")
    )

    assert result.ok is False
    assert "不支持" in result.message


@pytest.mark.unit
def test_wifi_connector_lists_windows_networks_with_current_marker():
    commands: list[list[str]] = []

    def runner(cmd: Sequence[str], **_: Any):
        args = list(cmd)
        commands.append(args)
        if args[:4] == ["netsh", "wlan", "show", "networks"]:
            return _completed(cmd, stdout=(
                "SSID 1 : CSG-UKEY\n"
                "    Authentication         : WPA2-Personal\n"
                "    Signal                 : 86%\n"
                "SSID 2 : Office WiFi\n"
                "    Authentication         : WPA3-Personal\n"
                "    Signal                 : 60%\n"
            ))
        if args[:4] == ["netsh", "wlan", "show", "interfaces"]:
            return _completed(cmd, stdout="Name : Wi-Fi\nSSID : CSG-UKEY\n")
        return _completed(cmd, returncode=1)

    networks = WifiConnector(platform_name="Windows", runner=runner).list_networks("Wi-Fi")

    assert [item.ssid for item in networks] == ["CSG-UKEY", "Office WiFi"]
    assert networks[0].signal == "86%"
    assert networks[0].security == "WPA2-Personal"
    assert networks[0].current is True
    assert any("interface=Wi-Fi" in args for args in commands)


@pytest.mark.unit
def test_wifi_connector_decodes_chinese_ssid_in_scan(monkeypatch):
    """netsh 扫描结果里中文 SSID 按 cp936 解码，不能变成乱码。"""
    from spider_gd.infrastructure import proc

    monkeypatch.setattr(proc.sys, "platform", "win32")
    monkeypatch.setattr(proc, "_windows_console_codepage", lambda: 936)

    def runner(cmd: Sequence[str], **_: Any):
        args = list(cmd)
        if args[:4] == ["netsh", "wlan", "show", "networks"]:
            raw = (
                "\r\nSSID 1 : 状态\r\n"
                "    网络类型            : 结构\r\n"
                "    身份验证            : WPA2-个人\r\n"
                "    加密                : CCMP\r\n"
                "    信号                : 88%\r\n"
            ).encode("cp936")
            return subprocess.CompletedProcess(args, 0, stdout=raw, stderr=b"")
        # current_ssid() 走 interfaces；返回别的网络即可。
        raw = "\r\n系统上有 1 个接口:\r\n\r\n    Name : WLAN\r\n    SSID : Other\r\n".encode("cp936")
        return subprocess.CompletedProcess(args, 0, stdout=raw, stderr=b"")

    networks = WifiConnector(platform_name="Windows", runner=runner).list_networks()

    assert [item.ssid for item in networks] == ["状态"]
    assert networks[0].signal == "88%"
    assert networks[0].security == "WPA2-个人"


def _cp936_console(monkeypatch):
    from spider_gd.infrastructure import proc

    monkeypatch.setattr(proc.sys, "platform", "win32")
    monkeypatch.setattr(proc, "_windows_console_codepage", lambda: 936)


@pytest.mark.unit
def test_wifi_connector_decodes_utf8_ssid_inside_cp936_interfaces(monkeypatch):
    """netsh 原样输出 SSID 字节：UTF-8 中文 SSID 夹在 cp936 表头里不能变乱码。"""
    _cp936_console(monkeypatch)

    def runner(cmd: Sequence[str], **_: Any):
        raw = (
            "\r\n系统上有 1 个接口:\r\n\r\n    名称                   : WLAN\r\n".encode("cp936")
            + "    状态                   : 已连接\r\n".encode("cp936")
            + b"    SSID                   : " + "南网共享电站".encode() + b"\r\n"
            + b"    BSSID                  : aa:bb:cc:dd:ee:ff\r\n"
        )
        return subprocess.CompletedProcess(list(cmd), 0, stdout=raw, stderr=b"")

    assert WifiConnector(platform_name="Windows", runner=runner).current_ssid() == "南网共享电站"


@pytest.mark.unit
def test_wifi_connector_decodes_utf8_ssid_in_scan(monkeypatch):
    _cp936_console(monkeypatch)

    def runner(cmd: Sequence[str], **_: Any):
        args = list(cmd)
        if args[:4] == ["netsh", "wlan", "show", "networks"]:
            raw = (
                b"\r\nSSID 1 : " + "南网共享电站".encode() + b"\r\n"
                + "    身份验证            : WPA2-个人\r\n".encode("cp936")
                + "    信号                : 88%\r\n".encode("cp936")
                + b"SSID 2 : " + "状态".encode("cp936") + b"\r\n"
            )
            return subprocess.CompletedProcess(args, 0, stdout=raw, stderr=b"")
        raw = (
            "\r\n系统上有 1 个接口:\r\n\r\n    名称 : WLAN\r\n".encode("cp936")
            + b"    SSID : " + "南网共享电站".encode() + b"\r\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout=raw, stderr=b"")

    networks = WifiConnector(platform_name="Windows", runner=runner).list_networks()

    assert [item.ssid for item in networks] == ["南网共享电站", "状态"]
    assert networks[0].security == "WPA2-个人"
    assert networks[0].current is True


@pytest.mark.unit
def test_decode_ssid_name_restores_netsh_hex_bytes():
    """netsh 把含中文的 SSID 打成 UTF-8 字节的十六进制串。"""
    assert decode_ssid_name("6950686F6E65E4BB80E4B988") == "iPhone什么"
    assert decode_ssid_name("E58D97E7BD91E585B1E4BAABE794B5E7AB99") == "南网共享电站"
    # 老路由器广播 GBK 字节时也不能解成乱码。
    assert decode_ssid_name("D7B4CCAC") == "状态"


@pytest.mark.unit
def test_decode_ssid_name_keeps_plain_ascii_names():
    """纯 ASCII 的 SSID netsh 会原样输出，不能被当成十六进制改坏。"""
    for value in ("GuestNetwork", "adsx2", "B38", "41424344", "beef", ""):
        assert decode_ssid_name(value) == value


@pytest.mark.unit
def test_wifi_connector_decodes_hex_ssid_in_interfaces():
    def runner(cmd: Sequence[str], **_: Any):
        raw = (
            "\r\n系统上有 1 个接口:\r\n\r\n    名称                   : WLAN\r\n".encode("cp936")
            + "    状态                   : 已连接\r\n".encode("cp936")
            + b"    SSID                   : 6950686F6E65E4BB80E4B988\r\n"
        )
        return subprocess.CompletedProcess(list(cmd), 0, stdout=raw, stderr=b"")

    assert WifiConnector(platform_name="Windows", runner=runner).current_ssid() == "iPhone什么"


@pytest.mark.unit
def test_wifi_connector_decodes_hex_ssid_in_scan():
    def runner(cmd: Sequence[str], **_: Any):
        args = list(cmd)
        if args[:4] == ["netsh", "wlan", "show", "networks"]:
            raw = (
                b"\r\nSSID 1 : E58D97E7BD91E585B1E4BAABE794B5E7AB99\r\n"
                + "    身份验证            : WPA2-个人\r\n".encode("cp936")
                + b"         Signal             : 88%\r\n"
                + b"SSID 2 : GuestNetwork\r\n"
            )
            return subprocess.CompletedProcess(args, 0, stdout=raw, stderr=b"")
        raw = (
            "\r\n系统上有 1 个接口:\r\n\r\n    名称 : WLAN\r\n".encode("cp936")
            + b"    SSID : E58D97E7BD91E585B1E4BAABE794B5E7AB99\r\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout=raw, stderr=b"")

    networks = WifiConnector(platform_name="Windows", runner=runner).list_networks()

    assert [item.ssid for item in networks] == ["南网共享电站", "GuestNetwork"]
    assert networks[0].signal == "88%"
    assert networks[0].security == "WPA2-个人"
    assert networks[0].current is True
    assert networks[1].current is False


@pytest.mark.unit
def test_wifi_connector_matches_chinese_target_against_hex_current_ssid():
    """当前 SSID 是十六进制、目标 SSID 是中文时要认为已经连上，不能反复重连。"""
    commands: list[list[str]] = []

    def runner(cmd: Sequence[str], **_: Any):
        commands.append(list(cmd))
        raw = (
            "\r\n系统上有 1 个接口:\r\n\r\n    Name : WLAN\r\n".encode("cp936")
            + b"    SSID : 6950686F6E65E4BB80E4B988\r\n"
        )
        return subprocess.CompletedProcess(list(cmd), 0, stdout=raw, stderr=b"")

    result = WifiConnector(platform_name="Windows", runner=runner).prepare(
        WifiConnectOptions(ssid="iPhone什么", interface_name="WLAN")
    )

    assert result.ok is True
    assert result.changed is False
    assert commands == [["netsh", "wlan", "show", "interfaces"]]


@pytest.mark.unit
def test_wifi_connector_connects_with_decoded_target_ssid():
    """设置里存成十六进制的目标 SSID，连接时要用还原后的中文名。"""
    connected = False
    commands: list[list[str]] = []

    def runner(cmd: Sequence[str], **_: Any):
        nonlocal connected
        args = list(cmd)
        commands.append(args)
        if args[:4] == ["netsh", "wlan", "show", "interfaces"]:
            raw = (
                "\r\n系统上有 1 个接口:\r\n\r\n    Name : WLAN\r\n".encode("cp936")
                + (b"    SSID : 6950686F6E65E4BB80E4B988\r\n" if connected else b"    SSID : Other\r\n")
            )
            return subprocess.CompletedProcess(args, 0, stdout=raw, stderr=b"")
        if args[:4] == ["netsh", "wlan", "add", "profile"]:
            profile_path = args[4].split("=", 1)[1]
            xml = Path(profile_path).read_text(encoding="utf-16")
            assert "<hex>6950686F6E65E4BB80E4B988</hex>" in xml
            assert "<name>spider-gd-" in xml
            return _completed(cmd, stdout="Profile added")
        if _is_profile_connect(args):
            connected = True
            return _completed(cmd, stdout="Connection request was completed")
        return _completed(cmd, returncode=1)

    result = WifiConnector(
        platform_name="Windows",
        runner=runner,
        sleeper=lambda _: None,
        poll_interval_sec=0.1,
    ).prepare(
        WifiConnectOptions(
            ssid="6950686F6E65E4BB80E4B988",
            password="secret-pass",
            interface_name="WLAN",
            timeout_sec=1,
        )
    )

    assert result.ok is True
    assert any(_is_profile_connect(args) for args in commands)
    assert any("已还原为" in item for item in result.details)
