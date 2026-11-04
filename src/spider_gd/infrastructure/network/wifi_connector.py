"""Connect the host to a configured WiFi network before network preflight."""

from __future__ import annotations

import hashlib
import html
import platform
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from spider_gd.infrastructure.proc import decode_console_bytes, ensure_text

#: 发出连接请求后，等无线网卡完成切换再读当前 SSID。
WIFI_SWITCH_CHECK_DELAY_SEC = 3.0


class CommandRunner(Protocol):
    def __call__(self, cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Run an OS command."""


@dataclass
class WifiConnectOptions:
    ssid: str
    password: str = ""
    interface_name: str = ""
    timeout_sec: int = 30


@dataclass
class WifiConnectResult:
    ok: bool
    target_ssid: str
    current_ssid: str = ""
    interface_name: str = ""
    platform: str = ""
    changed: bool = False
    message: str = ""
    details: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class WifiNetwork:
    ssid: str
    signal: str = ""
    security: str = ""
    current: bool = False


def _is_cjk_char(char: str) -> bool:
    """CJK 汉字 / 假名 / 全角 / emoji，即 >= U+2E80 的非 ASCII 字符。

    GBK 字节被误当 UTF-8 时会解出 Latin-1、希伯来文、西里尔等 U+0080–U+2E7F
    区间的字符，所以“非 ASCII 且 >= U+2E80”就是“这段字节确实是 UTF-8”的依据。
    """
    return ord(char) >= 0x2E80


def _is_cjk_text(text: str) -> bool:
    return all(char.isascii() or _is_cjk_char(char) for char in text)


def _has_control_char(text: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


def _decode_ssid_bytes(data: bytes) -> str:
    """SSID 值：合法 UTF-8 且非 ASCII 字符都是 CJK 等字符时按 UTF-8，否则按控制台代码页。

    GBK 字节偶尔也是合法 UTF-8（“状态”→ ``״̬``），但解出来不是 CJK，靠这一点区分。
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return decode_console_bytes(data)
    if _is_cjk_text(text):
        return text
    return decode_console_bytes(data)


#: netsh 对含非 ASCII 字节的 SSID 输出连续十六进制，例如
#: “iPhone什么” → ``6950686F6E65E4BB80E4B988``。
_HEX_SSID_RE = re.compile(r"\A(?:[0-9A-Fa-f]{2})+\Z")
#: 少于 4 字节的十六进制串更可能是 SSID 本身（例如 ``beef``），不还原。
_HEX_SSID_MIN_CHARS = 8


def _try_decode(raw: bytes, encoding: str) -> str:
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return ""
    return "" if _has_control_char(text) else text


def windows_wlan_profile_name(ssid: str) -> str:
    """为 SSID 生成只含 ASCII 的 WLAN 配置名。

    netsh 用配置名查找配置，再用配置里的 SSID 去连网络。配置名如果直接用中文，
    和 ``ssid=`` 参数对不上时会报「配置文件中不存在 SSID」。

    Args:
        ssid: 已还原的 WiFi 名称。

    Returns:
        str: ``spider-gd-`` 加 SSID 的稳定短摘要。

    Examples:
        >>> windows_wlan_profile_name("iPhone什么").startswith("spider-gd-")
        True
        >>> windows_wlan_profile_name("WiFiA") != windows_wlan_profile_name("WiFiB")
        True
    """
    digest = hashlib.sha256(ssid.encode("utf-8")).hexdigest()[:12]
    return f"spider-gd-{digest}"


def ssid_utf8_hex(ssid: str) -> str:
    """把 SSID 编成 Windows 配置文件里的十六进制。

    中文热点广播的是 UTF-8 字节。配置里的 ``hex`` 才是连接时真正使用的 SSID。

    Args:
        ssid: WiFi 名称。

    Returns:
        str: 大写十六进制。

    Examples:
        >>> ssid_utf8_hex("iPhone什么")
        '6950686F6E65E4BB80E4B988'
    """
    return ssid.encode("utf-8").hex().upper()


def decode_ssid_name(value: str) -> str:
    """把 netsh 输出的 SSID 文本还原成真实名称。

    Windows 的 ``netsh wlan show interfaces`` / ``show networks`` 只对**纯 ASCII**
    的 SSID 输出文本；一旦 SSID 里有中文（或任何 >=0x80 的字节），它就把 802.11
    里的原始字节打成连续十六进制，例如 ``6950686F6E65E4BB80E4B988`` 其实是
    ``iPhone什么``。手机热点和路由器广播的中文 SSID 基本都是 UTF-8，因此先按
    UTF-8 还原，再退回 GBK（老路由器的中文名可能是 GBK 字节）。

    纯 ASCII 的 SSID（``adsx2`` / ``GuestNetwork``）原样返回；不像 SSID 字节、
    或还原不出可读文字的值也原样返回，避免把“名字就是个十六进制串”的网络改坏。
    """
    text = (value or "").strip()
    if len(text) < _HEX_SSID_MIN_CHARS or len(text) % 2 or not _HEX_SSID_RE.match(text):
        return text
    raw = bytes.fromhex(text)
    utf8_text = _try_decode(raw, "utf-8")
    if utf8_text and not utf8_text.isascii() and _is_cjk_text(utf8_text):
        return utf8_text
    gbk_text = _try_decode(raw, "gbk")
    if gbk_text and not gbk_text.isascii() and _is_cjk_text(gbk_text):
        return gbk_text
    if utf8_text and not utf8_text.isascii():
        return utf8_text
    return text


def _decode_netsh_output(data: str | bytes | None) -> str:
    """netsh 输出按行解码：表头走控制台代码页，SSID 值单独判断编码。

    SSID 在 802.11 里是原始字节。纯 ASCII 的 SSID netsh 原样写出，中文 SSID 则
    被打成十六进制（见 :func:`decode_ssid_name`）；旧版输出也可能直接是原始字节，
    跟着整段按 cp936 解码会变成“鍗楃綉鍏变韩…”这类乱码，所以单独判定。
    """
    if not isinstance(data, bytes):
        return ensure_text(data)
    lines: list[str] = []
    for raw_line in data.split(b"\n"):
        key, sep, value = raw_line.partition(b":")
        if sep and key.strip().upper().startswith(b"SSID"):
            lines.append(decode_console_bytes(key + sep) + _decode_ssid_bytes(value))
        else:
            lines.append(decode_console_bytes(raw_line))
    return "\n".join(lines)


def detect_platform(system_name: str | None = None) -> str:
    name = (system_name or platform.system()).lower()
    if name in {"windows", "win32"}:
        return "windows"
    if name in {"darwin", "macos"}:
        return "macos"
    if name == "linux":
        return "linux"
    return "unsupported"


class WifiConnector:
    """Best-effort WiFi connector for Windows/macOS/Linux."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        runner: CommandRunner | None = None,
        sleeper: Callable[[float], None] | None = None,
        poll_interval_sec: float = 1.0,
    ) -> None:
        self.platform = detect_platform(platform_name)
        self.runner = runner or subprocess.run
        self.sleeper = sleeper or time.sleep
        self.poll_interval_sec = max(0.1, poll_interval_sec)

    def prepare(self, options: WifiConnectOptions) -> WifiConnectResult:
        raw_target = options.ssid.strip()
        # 设置页里选到的 SSID 可能已经是 netsh 的十六进制形态，连接/比较都用还原后的名字。
        target_ssid = decode_ssid_name(raw_target)
        interface_name = options.interface_name.strip()
        result = WifiConnectResult(
            ok=False,
            target_ssid=target_ssid,
            interface_name=interface_name,
            platform=self.platform,
        )
        if raw_target and raw_target != target_ssid:
            result.details.append(f"目标 SSID 为十六进制串，已还原为: {target_ssid}")
        if not target_ssid:
            result.error = "未配置 WiFi SSID"
            result.message = result.error
            return result
        if self.platform == "unsupported":
            result.error = "当前平台不支持自动连接 WiFi"
            result.message = result.error
            return result

        current_ssid = self.current_ssid(interface_name)
        result.current_ssid = current_ssid
        if current_ssid == target_ssid:
            result.ok = True
            result.message = f"已连接目标 WiFi: {target_ssid}"
            result.details.append(result.message)
            return result

        try:
            details = self._connect(replace(options, ssid=target_ssid))
            result.details.extend(details)
        except Exception as exc:
            result.error = str(exc)
            result.message = f"连接 WiFi 失败: {exc}"
            return result

        result.changed = True
        result.details.append(f"等待 {int(WIFI_SWITCH_CHECK_DELAY_SEC)} 秒后核对当前 WiFi")
        self.sleeper(WIFI_SWITCH_CHECK_DELAY_SEC)
        result.current_ssid = self.current_ssid(interface_name)
        if result.current_ssid == target_ssid:
            result.ok = True
            result.message = f"已切换到目标 WiFi: {target_ssid}"
            result.details.append(result.message)
        else:
            actual = result.current_ssid or "<未连接>"
            result.error = f"WiFi 未切换到 {target_ssid}，当前为 {actual}"
            result.message = result.error
        return result

    def current_ssid(self, interface_name: str = "") -> str:
        if self.platform == "windows":
            return self._current_ssid_windows(interface_name)
        if self.platform == "macos":
            return self._current_ssid_macos(interface_name)
        if self.platform == "linux":
            return self._current_ssid_linux(interface_name)
        return ""

    def list_networks(self, interface_name: str = "") -> list[WifiNetwork]:
        if self.platform == "unsupported":
            return []
        if self.platform == "windows":
            networks = self._list_windows_networks(interface_name)
        elif self.platform == "macos":
            networks = self._list_macos_networks(interface_name)
        elif self.platform == "linux":
            networks = self._list_linux_networks(interface_name)
        else:
            networks = []
        current = self.current_ssid(interface_name)
        for item in networks:
            item.current = bool(current and item.ssid == current)
        return networks

    def _connect(self, options: WifiConnectOptions) -> list[str]:
        if self.platform == "windows":
            return self._connect_windows(options)
        if self.platform == "macos":
            return self._connect_macos(options)
        if self.platform == "linux":
            return self._connect_linux(options)
        raise RuntimeError("当前平台不支持自动连接 WiFi")

    def _run(
        self,
        cmd: Sequence[str],
        *,
        timeout: int = 20,
        decode: Callable[[str | bytes | None], str] = ensure_text,
    ) -> subprocess.CompletedProcess[str]:
        # 二进制捕获后自行解码：后端以 PYTHONUTF8=1 运行时，text=True 会用 UTF-8
        # 解码 netsh 的 cp936 输出，失败后 stdout 变 None（表现为“命令未返回输出”）。
        result = self.runner(cmd, capture_output=True, timeout=timeout)
        result.stdout = decode(result.stdout)
        result.stderr = ensure_text(result.stderr)
        return result

    def _current_ssid_windows(self, interface_name: str = "") -> str:
        result = self._run(
            ["netsh", "wlan", "show", "interfaces"], timeout=8, decode=_decode_netsh_output
        )
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip() or "命令未返回输出"
            raise RuntimeError(f"netsh 读取当前 WiFi 失败: {error}")
        if not result.stdout.strip():
            raise RuntimeError("netsh 读取当前 WiFi 失败: 命令未返回输出")
        current_name = ""
        wanted = interface_name.strip()
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            key, value = [part.strip() for part in line.split(":", 1)]
            key_l = key.lower()
            if key_l in {"name", "名称"}:
                current_name = value
            elif key_l == "ssid" and (not wanted or current_name == wanted):
                return decode_ssid_name(value)
        return ""

    def _current_ssid_macos(self, interface_name: str = "") -> str:
        device = interface_name.strip() or self._default_macos_wifi_device()
        if not device:
            return ""
        result = self._run(["networksetup", "-getairportnetwork", device], timeout=8)
        text = result.stdout.strip()
        if ":" not in text:
            return ""
        return text.split(":", 1)[1].strip()

    def _current_ssid_linux(self, interface_name: str = "") -> str:
        cmd = ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"]
        if interface_name.strip():
            cmd.extend(["ifname", interface_name.strip()])
        result = self._run(cmd, timeout=8)
        for line in result.stdout.splitlines():
            active, _, ssid = line.partition(":")
            if active == "yes":
                return ssid.strip()
        return ""

    def _connect_windows(self, options: WifiConnectOptions) -> list[str]:
        ssid = options.ssid.strip()
        interface_name = options.interface_name.strip()
        profile_name = windows_wlan_profile_name(ssid)
        details: list[str] = []
        if options.password:
            self._install_windows_profile(
                ssid, options.password, interface_name, profile_name, details,
            )
        cmd = ["netsh", "wlan", "connect", f"name={profile_name}"]
        if interface_name:
            cmd.append(f"interface={interface_name}")
        result = self._run(cmd, timeout=15)
        if result.returncode != 0:
            output = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"netsh connect 失败: {output}")
        details.append(f"已请求 Windows 连接 WiFi: {ssid}")
        return details

    def _install_windows_profile(
        self,
        ssid: str,
        password: str,
        interface_name: str,
        profile_name: str,
        details: list[str],
    ) -> None:
        """写入当前用户的 WLAN 配置；普通同名配置先删掉再写。

        组策略下发、不能覆盖的配置会保留，随后仍按这个配置名去连接。

        Args:
            ssid: 已还原的 WiFi 名称。
            password: WiFi 密码。
            interface_name: 无线网卡名，可空。
            profile_name: ASCII 配置名。
            details: 追加给调用方的说明。

        Returns:
            None: 失败时抛出 RuntimeError。

        Raises:
            RuntimeError: netsh 无法写入配置。

        Examples:
            >>> callable(WifiConnector._install_windows_profile)
            True
        """
        profile_path = self._write_windows_profile(ssid, password, profile_name)
        try:
            result = self._add_windows_profile(profile_path, interface_name)
            output = result.stderr.strip() or result.stdout.strip()
            if result.returncode == 0:
                details.append("已写入 Windows WLAN 配置")
                return
            if self._is_windows_profile_locked(output):
                details.append("Windows 已存在同名 WLAN 配置，跳过写入并复用现有配置")
                return
            if self._is_windows_profile_exists_error(output):
                self._delete_windows_profile(profile_name, interface_name)
                replaced = self._add_windows_profile(profile_path, interface_name)
                replaced_output = replaced.stderr.strip() or replaced.stdout.strip()
                if replaced.returncode != 0:
                    raise RuntimeError(f"netsh add profile 失败: {replaced_output}")
                details.append("已替换 Windows WLAN 配置")
                return
            raise RuntimeError(f"netsh add profile 失败: {output}")
        finally:
            Path(profile_path).unlink(missing_ok=True)

    def _add_windows_profile(
        self, profile_path: str, interface_name: str,
    ) -> subprocess.CompletedProcess[str]:
        """调用 netsh 添加 WLAN 配置。

        Args:
            profile_path: 配置 XML 路径。
            interface_name: 无线网卡名，可空。

        Returns:
            subprocess.CompletedProcess[str]: netsh 的执行结果。

        Examples:
            >>> callable(WifiConnector._add_windows_profile)
            True
        """
        cmd = ["netsh", "wlan", "add", "profile", f"filename={profile_path}", "user=current"]
        if interface_name:
            cmd.append(f"interface={interface_name}")
        return self._run(cmd, timeout=15)

    def _delete_windows_profile(self, profile_name: str, interface_name: str) -> None:
        """删除当前用户里同名的旧 WLAN 配置。

        Args:
            profile_name: ASCII 配置名。
            interface_name: 无线网卡名，可空。

        Returns:
            None: 删除失败时忽略，交给随后的添加命令报错。

        Examples:
            >>> callable(WifiConnector._delete_windows_profile)
            True
        """
        cmd = ["netsh", "wlan", "delete", "profile", f"name={profile_name}"]
        if interface_name:
            cmd.append(f"interface={interface_name}")
        self._run(cmd, timeout=15)

    def _is_windows_profile_locked(self, text: str) -> bool:
        """组策略等不能覆盖的配置，不能删掉重写。

        Args:
            text: netsh 的输出。

        Returns:
            bool: 配置存在且禁止覆盖时为 True。

        Examples:
            >>> WifiConnector._is_windows_profile_locked(None, "组策略且无法覆盖")
            True
        """
        normalized = text.lower()
        return any(
            token in normalized
            for token in ("group policy", "cannot be overwritten", "cannot overwrite")
        ) or any(token in text for token in ("组策略", "无法覆盖"))

    def _is_windows_profile_exists_error(self, text: str) -> bool:
        normalized = text.lower()
        english_tokens = (
            "profile already exists",
            "already exists",
            "exists in group policy",
            "another user scope",
        )
        chinese_tokens = (
            "配置文件已存在",
            "具有此名称的配置文件已存在",
            "组策略",
            "其他用户范围",
            "无法覆盖",
        )
        return any(token in normalized for token in english_tokens) or any(
            token in text for token in chinese_tokens
        )

    def _connect_macos(self, options: WifiConnectOptions) -> list[str]:
        device = options.interface_name.strip() or self._default_macos_wifi_device()
        if not device:
            raise RuntimeError("未找到 macOS WiFi 设备")
        cmd = ["networksetup", "-setairportnetwork", device, options.ssid.strip()]
        if options.password:
            cmd.append(options.password)
        result = self._run(cmd, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(f"networksetup 连接失败: {result.stderr.strip() or result.stdout.strip()}")
        return [f"已请求 macOS 连接 WiFi: {options.ssid.strip()}"]

    def _connect_linux(self, options: WifiConnectOptions) -> list[str]:
        cmd = ["nmcli", "device", "wifi", "connect", options.ssid.strip()]
        if options.password:
            cmd.extend(["password", options.password])
        if options.interface_name.strip():
            cmd.extend(["ifname", options.interface_name.strip()])
        result = self._run(cmd, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(f"nmcli 连接失败: {result.stderr.strip() or result.stdout.strip()}")
        return [f"已请求 Linux 连接 WiFi: {options.ssid.strip()}"]

    def _list_windows_networks(self, interface_name: str = "") -> list[WifiNetwork]:
        cmd = ["netsh", "wlan", "show", "networks", "mode=bssid"]
        if interface_name.strip():
            cmd.append(f"interface={interface_name.strip()}")
        result = self._run(cmd, timeout=12, decode=_decode_netsh_output)
        if result.returncode != 0:
            raise RuntimeError(f"netsh 扫描 WiFi 失败: {result.stderr.strip() or result.stdout.strip()}")
        networks: list[WifiNetwork] = []
        current: WifiNetwork | None = None

        def flush() -> None:
            if current and current.ssid and not any(item.ssid == current.ssid for item in networks):
                networks.append(current)

        for line in result.stdout.splitlines():
            text = line.strip()
            if not text or ":" not in text:
                continue
            key, value = [part.strip() for part in text.split(":", 1)]
            key_l = key.lower()
            if key_l.startswith("ssid ") or key.startswith("SSID "):
                flush()
                current = WifiNetwork(ssid=decode_ssid_name(value))
                continue
            if not current:
                continue
            if key_l in {"authentication", "身份验证"}:
                current.security = value
            elif key_l in {"signal", "信号"} and not current.signal:
                current.signal = value
        flush()
        return networks

    def _list_macos_networks(self, interface_name: str = "") -> list[WifiNetwork]:
        airport = self._airport_path()
        if not airport:
            raise RuntimeError("未找到 macOS WiFi 扫描工具")
        result = self._run([airport, "-s"], timeout=12)
        if result.returncode != 0:
            raise RuntimeError(f"airport 扫描 WiFi 失败: {result.stderr.strip() or result.stdout.strip()}")
        return self._parse_macos_airport_scan(result.stdout)

    def _list_linux_networks(self, interface_name: str = "") -> list[WifiNetwork]:
        cmd = ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi"]
        if interface_name.strip():
            cmd.extend(["ifname", interface_name.strip()])
        result = self._run(cmd, timeout=12)
        if result.returncode != 0:
            raise RuntimeError(f"nmcli 扫描 WiFi 失败: {result.stderr.strip() or result.stdout.strip()}")
        networks: list[WifiNetwork] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            parts = self._split_nmcli_line(line)
            if not parts:
                continue
            ssid = parts[0].strip()
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            networks.append(WifiNetwork(
                ssid=ssid,
                signal=f"{parts[1].strip()}%" if len(parts) > 1 and parts[1].strip() else "",
                security=parts[2].strip() if len(parts) > 2 else "",
            ))
        return networks

    def _wait_for_ssid(self, target_ssid: str, interface_name: str, *, timeout_sec: int) -> str:
        deadline = time.monotonic() + timeout_sec
        current = ""
        while time.monotonic() <= deadline:
            current = self.current_ssid(interface_name)
            if current == target_ssid:
                return current
            self.sleeper(self.poll_interval_sec)
        return current

    def _default_macos_wifi_device(self) -> str:
        result = self._run(["networksetup", "-listallhardwareports"], timeout=8)
        current_port = ""
        for line in result.stdout.splitlines():
            text = line.strip()
            if text.startswith("Hardware Port:"):
                current_port = text.split(":", 1)[1].strip().lower()
            elif text.startswith("Device:") and current_port in {"wi-fi", "airport"}:
                return text.split(":", 1)[1].strip()
        return ""

    def _airport_path(self) -> str:
        candidates = [
            "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport",
            "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/A/Resources/airport",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
        return ""

    def _parse_macos_airport_scan(self, stdout: str) -> list[WifiNetwork]:
        lines = [line.rstrip("\n") for line in stdout.splitlines() if line.strip()]
        if not lines:
            return []
        header = lines[0]
        bssid_pos = header.find("BSSID")
        if bssid_pos < 0:
            return []
        networks: list[WifiNetwork] = []
        seen: set[str] = set()
        for line in lines[1:]:
            if len(line) <= bssid_pos:
                continue
            ssid = line[:bssid_pos].strip()
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            rest = line[bssid_pos:].split()
            signal = rest[1] if len(rest) > 1 else ""
            security = rest[-1] if len(rest) > 5 else ""
            networks.append(WifiNetwork(ssid=ssid, signal=signal, security=security))
        return networks

    def _split_nmcli_line(self, line: str) -> list[str]:
        parts: list[str] = []
        current = []
        escaped = False
        for char in line:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == ":":
                parts.append("".join(current))
                current = []
            else:
                current.append(char)
        if escaped:
            current.append("\\")
        parts.append("".join(current))
        return parts

    def _write_windows_profile(self, ssid: str, password: str, profile_name: str) -> str:
        ssid_hex = ssid_utf8_hex(ssid)
        profile = f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{html.escape(profile_name)}</name>
  <SSIDConfig>
    <SSID>
      <hex>{ssid_hex}</hex>
      <name>{html.escape(ssid)}</name>
    </SSID>
  </SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>auto</connectionMode>
  <MSM>
    <security>
      <authEncryption>
        <authentication>WPA2PSK</authentication>
        <encryption>AES</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
      <sharedKey>
        <keyType>passPhrase</keyType>
        <protected>false</protected>
        <keyMaterial>{html.escape(password)}</keyMaterial>
      </sharedKey>
    </security>
  </MSM>
</WLANProfile>
"""
        tmp = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16")
        try:
            tmp.write(profile)
            return tmp.name
        finally:
            tmp.close()
