"""Local WeChat desktop PoC delivery.

This module intentionally drives the real desktop client by keyboard and
clipboard, so callers must opt in with ``dry_run=False``.
"""

from __future__ import annotations

import base64
import importlib
import inspect as pyinspect
import json
import os
import platform
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from spider_gd.infrastructure.proc import ensure_text


class CommandRunner(Protocol):
    def __call__(self, cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Run an OS command."""


@dataclass
class DesktopWechatOptions:
    """Options for one desktop WeChat send attempt."""

    target: str
    message: str = ""
    file_path: str | Path | None = None
    app_name: str = ""
    pause_sec: float = 0.7
    backend: str = "auto"
    select_mode: str = "current"
    search_down_steps: int = 2
    profile_path: str | Path = "./data/wechat_targets.json"
    hash_threshold: int = 12
    allow_unstable_search: bool = False
    dry_run: bool = True


@dataclass
class DesktopWechatResult:
    ok: bool
    dry_run: bool
    platform: str
    actions: list[str] = field(default_factory=list)
    message: str = ""
    error_msg: str = ""


def detect_platform(sys_platform: str | None = None) -> str:
    name = sys_platform or platform.system()
    normalized = name.lower()
    if normalized in {"darwin", "macos"}:
        return "macos"
    if normalized in {"windows", "win32"}:
        return "windows"
    return "unsupported"


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class DesktopWechatSender:
    """Best-effort sender that operates the local WeChat desktop UI."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._platform = detect_platform(platform_name)
        self._runner = runner or subprocess.run

    def send(self, options: DesktopWechatOptions) -> DesktopWechatResult:
        actions, error = self._plan(options)
        if error:
            return DesktopWechatResult(
                ok=False,
                dry_run=options.dry_run,
                platform=self._platform,
                actions=actions,
                error_msg=error,
            )

        if options.dry_run:
            return DesktopWechatResult(
                ok=True,
                dry_run=True,
                platform=self._platform,
                actions=actions,
                message="dry-run：未操作微信；确认目标无误后加 --send 才会真实发送",
            )

        if self._platform == "macos":
            return self._send_macos(options, actions)
        if self._platform == "windows":
            return self._send_windows(options, actions)
        return DesktopWechatResult(
            ok=False,
            dry_run=False,
            platform=self._platform,
            actions=actions,
            error_msg="当前平台暂不支持本机微信 UI 自动化",
        )

    def _plan(self, options: DesktopWechatOptions) -> tuple[list[str], str]:
        actions: list[str] = []
        target = options.target.strip()
        if not target:
            return actions, "缺少目标会话：请传 --target"

        file_path = self._resolve_file_path(options.file_path)
        message = options.message.strip()
        if not message and file_path is None:
            return actions, "缺少发送内容：请传 --message 或 --file"
        if file_path is not None and not file_path.exists():
            return actions, f"文件不存在: {file_path}"
        if file_path is not None and file_path.is_dir():
            return actions, f"不支持发送文件夹: {file_path}"
        if options.select_mode not in {"exact", "current", "legacy-search", "bound"}:
            return actions, f"未知选择模式: {options.select_mode}"
        if options.backend not in {"auto", "wxauto", "powershell"}:
            return actions, f"未知发送后端: {options.backend}"
        if options.select_mode == "bound" and self._platform != "windows":
            return actions, "绑定模式目前只支持 Windows 微信客户端"
        if (
            not options.dry_run
            and self._platform == "macos"
            and options.backend in {"auto", "powershell"}
            and not options.allow_unstable_search
        ):
            return actions, (
                "macOS 本机微信脚本只能通过搜索式 UI 操作选择会话，真实发送默认禁止。"
                "请改用稳定 webhook/远程链接链路；仅临时 PoC 可显式加 --allow-unstable-search。"
            )
        if (
            not options.dry_run
            and self._platform == "windows"
            and options.backend in {"auto", "powershell"}
            and options.select_mode in {"exact", "legacy-search"}
            and not options.allow_unstable_search
        ):
            return actions, (
                "真实发送禁止使用搜索式选择模式，以避免命中微信“网络结果/搜一搜”或误发当前会话。"
                "请先手动打开目标后使用 --select-mode current，或先 --bind 后使用 --select-mode bound；"
                "仅临时 PoC 可显式加 --allow-unstable-search。"
            )

        if options.backend == "wxauto":
            actions.append("使用 wxauto 系列后端")
        elif options.backend == "auto":
            if self._platform == "windows":
                actions.append("Windows 使用 PowerShell UIA 后端；wxauto 需显式指定")
            else:
                actions.append("自动选择本机微信客户端后端")
        else:
            actions.append("使用 PowerShell UIA 后端")

        actions.append(f"激活微信客户端 ({', '.join(self._app_names(options))})")
        if options.backend != "wxauto":
            if options.select_mode == "bound":
                actions.append(f"使用绑定指纹校验会话: {target}")
            elif options.select_mode == "current":
                actions.append(f"校验当前微信会话包含目标: {target}")
            elif options.select_mode == "exact":
                actions.append(f"精确选择会话/功能结果: {target}")
            else:
                actions.append(f"搜索并打开会话: {target}")
        if options.select_mode == "legacy-search":
            actions.append(
                "兼容旧搜索参数：不再使用方向键/回车，改用功能/联系人分组安全选择"
            )
        if message:
            actions.append(f"发送文本消息: {len(message)} 字符")
        if file_path is not None:
            actions.append(f"发送文件: {file_path}")
        return actions, ""

    def bind(
        self,
        *,
        target: str,
        app_name: str = "",
        profile_path: str | Path = "./data/wechat_targets.json",
        input_x_ratio: float = 0.70,
        input_y_ratio: float = 0.90,
        title_x_ratio: float = 0.38,
        title_y_ratio: float = 0.04,
        title_width_ratio: float = 0.45,
        title_height_ratio: float = 0.08,
    ) -> DesktopWechatResult:
        actions = [f"绑定当前微信会话为固定目标: {target}"]
        target = target.strip()
        if not target:
            return DesktopWechatResult(
                ok=False,
                dry_run=False,
                platform=self._platform,
                actions=actions,
                error_msg="缺少目标会话：请传 --target",
            )
        if self._platform != "windows":
            return DesktopWechatResult(
                ok=False,
                dry_run=False,
                platform=self._platform,
                actions=actions,
                error_msg="绑定模式目前只支持 Windows 微信客户端",
            )

        env = os.environ.copy()
        options = DesktopWechatOptions(target=target, app_name=app_name)
        env.update(
            {
                "SPIDER_GD_WECHAT_APP_NAMES": "|".join(self._app_names(options)),
                "SPIDER_GD_WECHAT_TARGET": target,
                "SPIDER_GD_WECHAT_INPUT_X_RATIO": str(input_x_ratio),
                "SPIDER_GD_WECHAT_INPUT_Y_RATIO": str(input_y_ratio),
                "SPIDER_GD_WECHAT_TITLE_X_RATIO": str(title_x_ratio),
                "SPIDER_GD_WECHAT_TITLE_Y_RATIO": str(title_y_ratio),
                "SPIDER_GD_WECHAT_TITLE_W_RATIO": str(title_width_ratio),
                "SPIDER_GD_WECHAT_TITLE_H_RATIO": str(title_height_ratio),
            }
        )
        encoded = base64.b64encode(_windows_bind_script().encode("utf-16le")).decode("ascii")
        result = self._run(
            [
                "powershell",
                "-NoProfile",
                "-Sta",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            env=env,
        )
        if result.returncode != 0:
            return DesktopWechatResult(
                ok=False,
                dry_run=False,
                platform=self._platform,
                actions=actions,
                error_msg=_stderr(result),
            )

        try:
            record = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return DesktopWechatResult(
                ok=False,
                dry_run=False,
                platform=self._platform,
                actions=actions,
                error_msg=f"绑定脚本返回 JSON 解析失败: {exc}",
            )
        record["bound_at"] = datetime.now().isoformat(timespec="seconds")
        profile_file = Path(profile_path).expanduser().resolve()
        profile_file.parent.mkdir(parents=True, exist_ok=True)
        profile = _load_wechat_profile(profile_file)
        targets = profile.setdefault("targets", {})
        targets[target] = record
        profile_file.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        return DesktopWechatResult(
            ok=True,
            dry_run=False,
            platform=self._platform,
            actions=actions,
            message=f"已绑定 {target} -> {profile_file}",
        )

    def _send_macos(
        self,
        options: DesktopWechatOptions,
        actions: list[str],
    ) -> DesktopWechatResult:
        app_name, error = self._open_macos_app(options)
        if error:
            return self._failure(options, actions, error)

        target = options.target.strip()
        try:
            self._copy_text_macos(target)
        except RuntimeError as exc:
            return self._failure(options, actions, str(exc))
        result = self._run_osascript(_mac_search_script(app_name, options.pause_sec))
        if result.returncode != 0:
            return self._failure(options, actions, _stderr(result))

        if options.message.strip():
            try:
                self._copy_text_macos(options.message.strip())
            except RuntimeError as exc:
                return self._failure(options, actions, str(exc))
            result = self._run_osascript(_mac_paste_and_enter_script(app_name, options.pause_sec))
            if result.returncode != 0:
                return self._failure(options, actions, _stderr(result))

        file_path = self._resolve_file_path(options.file_path)
        if file_path is not None:
            result = self._run_osascript(
                _mac_file_clipboard_and_send_script(app_name, file_path, options.pause_sec)
            )
            if result.returncode != 0:
                return self._failure(options, actions, _stderr(result))

        return DesktopWechatResult(
            ok=True,
            dry_run=False,
            platform=self._platform,
            actions=actions,
            message="已把发送动作提交给本机微信客户端",
        )

    def inspect(
        self,
        *,
        app_name: str = "",
        max_depth: int = 4,
        name_filter: str = "",
    ) -> DesktopWechatResult:
        actions = [f"检查微信窗口控件树: depth={max_depth} filter={name_filter or '*'}"]
        if self._platform != "windows":
            return DesktopWechatResult(
                ok=False,
                dry_run=False,
                platform=self._platform,
                actions=actions,
                error_msg="控件树检查目前只支持 Windows 微信客户端",
            )
        env = os.environ.copy()
        options = DesktopWechatOptions(target="inspect", app_name=app_name)
        env.update(
            {
                "SPIDER_GD_WECHAT_APP_NAMES": "|".join(self._app_names(options)),
                "SPIDER_GD_WECHAT_INSPECT_DEPTH": str(max(1, max_depth)),
                "SPIDER_GD_WECHAT_INSPECT_FILTER": name_filter,
            }
        )
        encoded = base64.b64encode(_windows_inspect_script().encode("utf-16le")).decode("ascii")
        result = self._run(
            [
                "powershell",
                "-NoProfile",
                "-Sta",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            env=env,
        )
        if result.returncode != 0:
            return DesktopWechatResult(
                ok=False,
                dry_run=False,
                platform=self._platform,
                actions=actions,
                error_msg=_stderr(result),
            )
        return DesktopWechatResult(
            ok=True,
            dry_run=False,
            platform=self._platform,
            actions=actions,
            message=result.stdout.strip(),
        )

    def _send_windows(
        self,
        options: DesktopWechatOptions,
        actions: list[str],
    ) -> DesktopWechatResult:
        if options.backend == "wxauto":
            wxauto_result = self._send_windows_wxauto(options, actions)
            return wxauto_result

        file_path = self._resolve_file_path(options.file_path)
        pause_ms = max(100, int(options.pause_sec * 1000))
        env = os.environ.copy()
        bound_profile = ""
        if options.select_mode == "bound":
            try:
                bound_profile = json.dumps(
                    _load_bound_wechat_target(options.profile_path, options.target.strip()),
                    ensure_ascii=False,
                )
            except Exception as exc:
                return self._failure(options, actions, f"绑定配置不可用: {exc}")
        env.update(
            {
                "SPIDER_GD_WECHAT_TARGET": options.target.strip(),
                "SPIDER_GD_WECHAT_MESSAGE": options.message.strip(),
                "SPIDER_GD_WECHAT_FILE": str(file_path) if file_path is not None else "",
                "SPIDER_GD_WECHAT_APP_NAMES": "|".join(self._app_names(options)),
                "SPIDER_GD_WECHAT_PAUSE_MS": str(pause_ms),
                "SPIDER_GD_WECHAT_SELECT_MODE": options.select_mode,
                "SPIDER_GD_WECHAT_SEARCH_DOWN": str(max(0, options.search_down_steps)),
                "SPIDER_GD_WECHAT_BOUND_PROFILE": bound_profile,
                "SPIDER_GD_WECHAT_HASH_THRESHOLD": str(max(0, options.hash_threshold)),
            }
        )
        script = _windows_send_script()
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = self._run(
            [
                "powershell",
                "-NoProfile",
                "-Sta",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            env=env,
        )
        if result.returncode != 0:
            return self._failure(options, actions, _stderr(result))
        return DesktopWechatResult(
            ok=True,
            dry_run=False,
            platform=self._platform,
            actions=actions,
            message="已把发送动作提交给本机微信客户端",
        )

    def _send_windows_wxauto(
        self,
        options: DesktopWechatOptions,
        actions: list[str],
    ) -> DesktopWechatResult:
        try:
            module_name, module = _import_wxauto_module()
            wechat_cls = module.WeChat
        except Exception as exc:
            return self._failure(options, actions, f"wxauto 后端不可用: {exc}")

        target = options.target.strip()
        file_path = self._resolve_file_path(options.file_path)
        try:
            wx = wechat_cls()
            is_online = getattr(wx, "IsOnline", None)
            if callable(is_online) and not is_online():
                return self._failure(options, actions, "wxauto 检测到微信未登录")

            if options.message.strip():
                _wxauto_send_message(wx, options.message.strip(), target)
            if file_path is not None:
                _wxauto_send_file(wx, file_path, target)
        except Exception as exc:
            return self._failure(options, actions, f"{module_name} 发送失败: {exc}")

        return DesktopWechatResult(
            ok=True,
            dry_run=False,
            platform=self._platform,
            actions=actions,
            message=f"已通过 {module_name} 提交发送动作",
        )

    def _open_macos_app(self, options: DesktopWechatOptions) -> tuple[str, str]:
        last_error = ""
        for app_name in self._app_names(options):
            result = self._run(["open", "-a", app_name])
            if result.returncode == 0:
                return app_name, ""
            last_error = _stderr(result)
        return "", last_error or "无法打开微信客户端，请确认 WeChat/微信 已安装并已登录"

    def _copy_text_macos(self, text: str) -> None:
        result = self._run(["pbcopy"], input=text)
        if result.returncode != 0:
            raise RuntimeError(_stderr(result))

    def _run_osascript(self, script: str) -> subprocess.CompletedProcess[str]:
        return self._run(["osascript", "-e", script])

    def _run(
        self,
        cmd: Sequence[str],
        *,
        input: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self._runner(
            cmd,
            input=input,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        # PowerShell 在中文系统上输出 cp936；PYTHONUTF8=1 时 text=True 会解码失败并
        # 把 stdout 吞成 None，这里统一归一成 str。
        result.stdout = ensure_text(result.stdout)
        result.stderr = ensure_text(result.stderr)
        return result

    def _app_names(self, options: DesktopWechatOptions) -> list[str]:
        if options.app_name.strip():
            return [options.app_name.strip()]
        if self._platform == "macos":
            return ["WeChat", "微信"]
        if self._platform == "windows":
            return ["微信", "WeChat"]
        return ["WeChat"]

    def _failure(
        self,
        options: DesktopWechatOptions,
        actions: list[str],
        error_msg: str,
    ) -> DesktopWechatResult:
        return DesktopWechatResult(
            ok=False,
            dry_run=options.dry_run,
            platform=self._platform,
            actions=actions,
            error_msg=error_msg,
        )

    @staticmethod
    def _resolve_file_path(file_path: str | Path | None) -> Path | None:
        if file_path is None or str(file_path).strip() == "":
            return None
        return Path(file_path).expanduser().resolve()


def _stderr(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "命令执行失败").strip()


def _load_wechat_profile(profile_path: Path) -> dict[str, Any]:
    if not profile_path.exists():
        return {"version": 1, "targets": {}}
    return json.loads(profile_path.read_text(encoding="utf-8"))


def _load_bound_wechat_target(profile_path: str | Path, target: str) -> dict[str, Any]:
    profile_file = Path(profile_path).expanduser().resolve()
    profile = _load_wechat_profile(profile_file)
    targets = profile.get("targets", {})
    if target not in targets:
        raise RuntimeError(f"未找到绑定目标 {target!r}，请先运行 --bind")
    record = targets[target]
    if not isinstance(record, dict):
        raise RuntimeError(f"绑定目标配置格式错误: {target}")
    return record


def _import_wxauto_module() -> tuple[str, Any]:
    errors: list[str] = []
    for module_name in ("wxautox4", "wxauto4", "wxauto"):
        try:
            module = importlib.import_module(module_name)
            if not hasattr(module, "WeChat"):
                errors.append(f"{module_name} 缺少 WeChat 类")
                continue
            return module_name, module
        except Exception as exc:
            errors.append(f"{module_name}: {exc}")
    raise RuntimeError("; ".join(errors))


def _call_with_supported_args(method: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        signature = pyinspect.signature(method)
    except (TypeError, ValueError):
        return method(*args, **kwargs)

    parameters = signature.parameters
    if any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
        return method(*args, **kwargs)
    supported_kwargs = {k: v for k, v in kwargs.items() if k in parameters}
    return method(*args, **supported_kwargs)


def _wxauto_chat_with(wx: Any, target: str) -> None:
    chat_with = getattr(wx, "ChatWith", None)
    if not callable(chat_with):
        raise RuntimeError("当前 wxauto 模块不支持 ChatWith，无法安全选择目标会话")
    _call_with_supported_args(chat_with, target, exact=True)


def _wxauto_send_message(wx: Any, message: str, target: str) -> None:
    send_msg = getattr(wx, "SendMsg", None)
    if not callable(send_msg):
        raise RuntimeError("当前 wxauto 模块不支持 SendMsg")
    try:
        _call_with_supported_args(send_msg, message, target, exact=True)
        return
    except TypeError:
        pass
    _wxauto_chat_with(wx, target)
    _call_with_supported_args(send_msg, message)


def _wxauto_send_file(wx: Any, file_path: Path, target: str) -> None:
    send_files = getattr(wx, "SendFiles", None)
    if not callable(send_files):
        raise RuntimeError("当前 wxauto 模块不支持 SendFiles")
    try:
        _call_with_supported_args(send_files, str(file_path), target, exact=True)
        return
    except TypeError:
        pass
    _wxauto_chat_with(wx, target)
    _call_with_supported_args(send_files, str(file_path))


def _mac_search_script(app_name: str, pause_sec: float) -> str:
    return f"""
tell application {_applescript_string(app_name)} to activate
delay {pause_sec}
tell application "System Events"
    keystroke "f" using command down
    delay {pause_sec}
    keystroke "v" using command down
    delay {pause_sec * 2}
    key code 36
    delay {pause_sec * 2}
end tell
"""


def _mac_paste_and_enter_script(app_name: str, pause_sec: float) -> str:
    return f"""
tell application {_applescript_string(app_name)} to activate
delay {pause_sec}
tell application "System Events"
    keystroke "v" using command down
    delay {pause_sec}
    key code 36
    delay {pause_sec}
end tell
"""


def _mac_file_clipboard_and_send_script(
    app_name: str,
    file_path: Path,
    pause_sec: float,
) -> str:
    return f"""
set the clipboard to (POSIX file {_applescript_string(str(file_path))})
tell application {_applescript_string(app_name)} to activate
delay {pause_sec}
tell application "System Events"
    keystroke "v" using command down
    delay {pause_sec * 2}
    key code 36
    delay {pause_sec}
end tell
"""


def _windows_send_script() -> str:
    return r"""
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class KeyboardNative {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(
        uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}
"@
$wshell = New-Object -ComObject WScript.Shell
$pauseMs = [int]$env:SPIDER_GD_WECHAT_PAUSE_MS
$target = $env:SPIDER_GD_WECHAT_TARGET
$message = $env:SPIDER_GD_WECHAT_MESSAGE
$file = $env:SPIDER_GD_WECHAT_FILE
$appNames = $env:SPIDER_GD_WECHAT_APP_NAMES -split "\|"
$selectMode = $env:SPIDER_GD_WECHAT_SELECT_MODE
$searchDown = [int]$env:SPIDER_GD_WECHAT_SEARCH_DOWN
$boundProfileJson = $env:SPIDER_GD_WECHAT_BOUND_PROFILE
$hashThreshold = [int]$env:SPIDER_GD_WECHAT_HASH_THRESHOLD
$KEYEVENTF_KEYUP = 0x0002
$MOUSEEVENTF_LEFTDOWN = 0x0002
$MOUSEEVENTF_LEFTUP = 0x0004
$VK_CONTROL = 0x11
$VK_A = 0x41
$VK_F = 0x46
$VK_V = 0x56
$VK_RETURN = 0x0D
$VK_DOWN = 0x28

function Wait-Step {
    param([int]$Multiplier = 1)
    Start-Sleep -Milliseconds ($pauseMs * $Multiplier)
}

function Send-Key {
    param([byte]$KeyCode)
    [KeyboardNative]::keybd_event($KeyCode, 0, 0, [UIntPtr]::Zero)
    Wait-Step
    [KeyboardNative]::keybd_event($KeyCode, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
}

function Send-CtrlKey {
    param([byte]$KeyCode)
    [KeyboardNative]::keybd_event($VK_CONTROL, 0, 0, [UIntPtr]::Zero)
    Wait-Step
    [KeyboardNative]::keybd_event($KeyCode, 0, 0, [UIntPtr]::Zero)
    Wait-Step
    [KeyboardNative]::keybd_event($KeyCode, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
    Wait-Step
    [KeyboardNative]::keybd_event($VK_CONTROL, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
}

function Activate-WeChat {
    foreach ($name in $appNames) {
        if ([string]::IsNullOrWhiteSpace($name)) {
            continue
        }
        if ($wshell.AppActivate($name)) {
            Wait-Step
            return $true
        }
    }
    return $false
}

function Get-WeChatRoot {
    $hwnd = [KeyboardNative]::GetForegroundWindow()
    if ($hwnd -eq [IntPtr]::Zero) {
        throw "无法读取当前微信窗口句柄"
    }
    return [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
}

function Test-VisibleElement {
    param([System.Windows.Automation.AutomationElement]$Element)
    if ($null -eq $Element) {
        return $false
    }
    try {
        $rect = $Element.Current.BoundingRectangle
    } catch {
        return $false
    }
    return ($rect.Width -gt 1 -and $rect.Height -gt 1)
}

function Get-VisibleElementsByName {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Name
    )
    $nameProperty = [System.Windows.Automation.AutomationElement]::NameProperty
    $pc = "System.Windows.Automation.PropertyCondition"
    $condition = New-Object $pc -ArgumentList $nameProperty, $Name
    $items = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    $matches = New-Object System.Collections.ArrayList
    foreach ($item in $items) {
        if (Test-VisibleElement $item) {
            [void]$matches.Add($item)
        }
    }
    return $matches
}

function Find-VisibleElementByName {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Name
    )
    foreach ($item in (Get-VisibleElementsByName $Root $Name)) {
        return $item
    }
    return $null
}

function Is-InMainPane {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [System.Windows.Automation.AutomationElement]$Element
    )
    $rootRect = $Root.Current.BoundingRectangle
    $rect = $Element.Current.BoundingRectangle
    if ($rect.Width -le 1 -or $rect.Height -le 1) {
        return $false
    }
    $centerX = $rect.Left + ($rect.Width / 2)
    $mainPaneLeft = $rootRect.Left + ($rootRect.Width * 0.35)
    return $centerX -ge $mainPaneLeft
}

function Is-InMainTitleArea {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [System.Windows.Automation.AutomationElement]$Element
    )
    if (-not (Is-InMainPane $Root $Element)) {
        return $false
    }
    $rootRect = $Root.Current.BoundingRectangle
    $rect = $Element.Current.BoundingRectangle
    $centerY = $rect.Top + ($rect.Height / 2)
    $titleBottom = $rootRect.Top + ($rootRect.Height * 0.20)
    return $centerY -le $titleBottom
}

function Is-InLeftPane {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [System.Windows.Automation.AutomationElement]$Element
    )
    if (-not (Test-VisibleElement $Element)) {
        return $false
    }
    $rootRect = $Root.Current.BoundingRectangle
    $rect = $Element.Current.BoundingRectangle
    $centerX = $rect.Left + ($rect.Width / 2)
    $centerY = $rect.Top + ($rect.Height / 2)
    $leftLimit = $rootRect.Left + ($rootRect.Width * 0.42)
    return (
        $centerX -ge $rootRect.Left -and
        $centerX -le $leftLimit -and
        $centerY -ge $rootRect.Top
    )
}

function Find-VisibleElementByNameInMainPane {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Name
    )
    foreach ($item in (Get-VisibleElementsByName $Root $Name)) {
        if (Is-InMainTitleArea $Root $item) {
            return $item
        }
    }
    return $null
}

function Click-Element {
    param([System.Windows.Automation.AutomationElement]$Element)
    $rect = $Element.Current.BoundingRectangle
    if ($rect.Width -le 1 -or $rect.Height -le 1) {
        return $false
    }
    $x = [int]($rect.Left + ($rect.Width / 2))
    $y = [int]($rect.Top + ($rect.Height / 2))
    [KeyboardNative]::SetCursorPos($x, $y) | Out-Null
    Wait-Step
    [KeyboardNative]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
    Wait-Step
    [KeyboardNative]::mouse_event($MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
    Wait-Step 2
    return $true
}

function Find-SearchInput {
    $root = Get-WeChatRoot
    $rootRect = $root.Current.BoundingRectangle
    $editType = [System.Windows.Automation.ControlType]::Edit
    $typeProperty = [System.Windows.Automation.AutomationElement]::ControlTypeProperty
    $condition = New-Object System.Windows.Automation.PropertyCondition `
        -ArgumentList $typeProperty, $editType
    $items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    $best = $null
    $bestTop = [double]::PositiveInfinity
    foreach ($item in $items) {
        if (-not (Test-VisibleElement $item)) {
            continue
        }
        if (-not (Is-InLeftPane $root $item)) {
            continue
        }
        $rect = $item.Current.BoundingRectangle
        if ($rect.Width -le 30 -or $rect.Height -le 8) {
            continue
        }
        if ($rect.Top -gt ($rootRect.Top + ($rootRect.Height * 0.25))) {
            continue
        }
        if ($rect.Top -lt $bestTop) {
            $best = $item
            $bestTop = $rect.Top
        }
    }
    return $best
}

function Set-SearchInputText {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [string]$Text
    )
    $valuePattern = $null
    try {
        if ($Element.TryGetCurrentPattern(
            [System.Windows.Automation.ValuePattern]::Pattern,
            [ref]$valuePattern
        )) {
            $valuePattern.SetValue($Text)
            Wait-Step 2
            return
        }
    } catch {
    }

    Click-Element $Element | Out-Null
    Send-CtrlKey $VK_A
    Wait-Step
    [System.Windows.Forms.Clipboard]::SetText($Text)
    Send-CtrlKey $VK_V
    Wait-Step 2
}

function Focus-AndFillSearch {
    $searchInput = Find-SearchInput
    if ($null -eq $searchInput) {
        Send-CtrlKey $VK_F
        Wait-Step 2
        $searchInput = Find-SearchInput
    }
    if ($null -eq $searchInput) {
        throw "无法定位微信左上搜索框，已停止，避免把搜索词误输入当前聊天。"
    }
    Set-SearchInputText $searchInput $target
    Wait-Step 3
}

function Find-FirstLeftPaneElementByName {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Name
    )
    $best = $null
    $bestTop = [double]::PositiveInfinity
    foreach ($item in (Get-VisibleElementsByName $Root $Name)) {
        if (-not (Is-InLeftPane $Root $item)) {
            continue
        }
        $rect = $item.Current.BoundingRectangle
        if ($rect.Top -lt $bestTop) {
            $best = $item
            $bestTop = $rect.Top
        }
    }
    return $best
}

function Get-ElementNameSafe {
    param([System.Windows.Automation.AutomationElement]$Element)
    if ($null -eq $Element) {
        return ""
    }
    try {
        return [string]$Element.Current.Name
    } catch {
        return ""
    }
}

function Get-ElementControlTypeName {
    param([System.Windows.Automation.AutomationElement]$Element)
    if ($null -eq $Element) {
        return ""
    }
    try {
        return [string]$Element.Current.ControlType.ProgrammaticName
    } catch {
        return ""
    }
}

function Test-NameContains {
    param(
        [string]$Name,
        [string]$Needle
    )
    if ([string]::IsNullOrWhiteSpace($Name) -or [string]::IsNullOrWhiteSpace($Needle)) {
        return $false
    }
    return $Name.IndexOf($Needle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Test-HeaderName {
    param(
        [string]$Name,
        [string[]]$Headers
    )
    $trimmed = $Name.Trim()
    foreach ($header in $Headers) {
        if ($trimmed -eq $header -or (Test-NameContains $trimmed $header)) {
            return $true
        }
    }
    return $false
}

function Get-AllVisibleLeftPaneElements {
    param([System.Windows.Automation.AutomationElement]$Root)
    $condition = [System.Windows.Automation.Condition]::TrueCondition
    $items = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    $matches = New-Object System.Collections.ArrayList
    foreach ($item in $items) {
        if (-not (Test-VisibleElement $item)) {
            continue
        }
        if (-not (Is-InLeftPane $Root $item)) {
            continue
        }
        [void]$matches.Add($item)
    }
    return $matches
}

function Find-FirstLeftPaneHeader {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string[]]$Headers,
        [double]$MinTop = 0
    )
    $best = $null
    $bestTop = [double]::PositiveInfinity
    foreach ($item in (Get-AllVisibleLeftPaneElements $Root)) {
        $name = Get-ElementNameSafe $item
        if (-not (Test-HeaderName $name $Headers)) {
            continue
        }
        $rect = $item.Current.BoundingRectangle
        if ($rect.Top -lt $MinTop) {
            continue
        }
        if ($rect.Top -lt $bestTop) {
            $best = $item
            $bestTop = $rect.Top
        }
    }
    return $best
}

function Find-NextLeftPaneHeaderBelow {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [double]$Top
    )
    $knownHeaders = @(
        "联系人",
        "群聊",
        "聊天记录",
        "公众号",
        "小程序",
        "功能",
        "网络结果",
        "搜一搜",
        "网络搜索"
    )
    $best = $null
    $bestTop = [double]::PositiveInfinity
    foreach ($item in (Get-AllVisibleLeftPaneElements $Root)) {
        $name = Get-ElementNameSafe $item
        if (-not (Test-HeaderName $name $knownHeaders)) {
            continue
        }
        $rect = $item.Current.BoundingRectangle
        if ($rect.Top -le ($Top + 2)) {
            continue
        }
        if ($rect.Top -lt $bestTop) {
            $best = $item
            $bestTop = $rect.Top
        }
    }
    return $best
}

function Test-NetworkSearchCandidate {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [System.Windows.Automation.AutomationElement]$Element
    )
    $patterns = @("搜一搜", "网络结果", "网络搜索", "搜索网络", "网页搜索", "Search Web")
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $current = $Element
    for ($i = 0; $i -lt 5; $i++) {
        if ($null -eq $current) {
            break
        }
        foreach ($pattern in $patterns) {
            if (Test-NameContains (Get-ElementNameSafe $current) $pattern) {
                return $true
            }
        }
        try {
            $current = $walker.GetParent($current)
        } catch {
            break
        }
        if ($null -eq $current -or [object]::ReferenceEquals($current, $Root)) {
            break
        }
        if (-not (Is-InLeftPane $Root $current)) {
            break
        }
    }
    return $false
}

function Get-SearchResultClickElement {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [System.Windows.Automation.AutomationElement]$Element,
        [bool]$TrustedSection = $false
    )
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $best = $Element
    $current = $Element
    $elementRect = $Element.Current.BoundingRectangle
    $elementCenterY = $elementRect.Top + ($elementRect.Height / 2)
    for ($i = 0; $i -lt 5; $i++) {
        if ($null -eq $current) {
            break
        }
        $typeName = Get-ElementControlTypeName $current
        $rect = $current.Current.BoundingRectangle
        $centerY = $rect.Top + ($rect.Height / 2)
        if (
            (Test-VisibleElement $current) -and
            (Is-InLeftPane $Root $current) -and
            ($TrustedSection -or (-not (Test-NetworkSearchCandidate $Root $current))) -and
            $rect.Width -ge 80 -and
            $rect.Height -ge 18 -and
            $rect.Height -le 96 -and
            ([Math]::Abs($centerY - $elementCenterY) -le 48) -and
            (
                $typeName -like "*ListItem*" -or
                $typeName -like "*Button*" -or
                $typeName -like "*Pane*" -or
                $rect.Width -gt $best.Current.BoundingRectangle.Width
            )
        ) {
            $best = $current
        }
        try {
            $current = $walker.GetParent($current)
        } catch {
            break
        }
        if ($null -eq $current -or [object]::ReferenceEquals($current, $Root)) {
            break
        }
        if (-not (Is-InLeftPane $Root $current)) {
            break
        }
    }
    return $best
}

function Test-ElementInSection {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [System.Windows.Automation.AutomationElement]$Header,
        [System.Windows.Automation.AutomationElement]$NextHeader
    )
    if ($null -eq $Header) {
        return $false
    }
    $rect = $Element.Current.BoundingRectangle
    $headerRect = $Header.Current.BoundingRectangle
    if ($rect.Top -lt ($headerRect.Bottom - 2)) {
        return $false
    }
    if ($null -ne $NextHeader) {
        $nextTop = $NextHeader.Current.BoundingRectangle.Top
        if ($rect.Top -ge ($nextTop - 2)) {
            return $false
        }
    }
    return $true
}

function Get-SearchTargetCandidates {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [bool]$AllowContains = $false,
        [bool]$TrustedSection = $false
    )
    $rootRect = $Root.Current.BoundingRectangle
    $matches = New-Object System.Collections.ArrayList
    foreach ($item in (Get-AllVisibleLeftPaneElements $Root)) {
        $rect = $item.Current.BoundingRectangle
        if ($rect.Top -lt ($rootRect.Top + 60)) {
            continue
        }
        $name = (Get-ElementNameSafe $item).Trim()
        if ([string]::IsNullOrWhiteSpace($name)) {
            continue
        }
        $isMatch = $name -eq $target
        if (-not $isMatch -and $AllowContains) {
            $isMatch = Test-NameContains $name $target
        }
        if (-not $isMatch) {
            continue
        }
        if ((-not $TrustedSection) -and (Test-NetworkSearchCandidate $Root $item)) {
            continue
        }
        [void]$matches.Add($item)
    }
    return $matches
}

function Find-SearchTargetResult {
    $root = Get-WeChatRoot
    $featureHeader = Find-FirstLeftPaneHeader $root @("功能")
    if ($null -ne $featureHeader) {
        $featureRect = $featureHeader.Current.BoundingRectangle
        $nextHeader = Find-NextLeftPaneHeaderBelow $root $featureRect.Bottom
        $bestFeature = $null
        $bestFeatureTop = [double]::PositiveInfinity
        foreach ($item in (Get-SearchTargetCandidates $root $true $true)) {
            $rect = $item.Current.BoundingRectangle
            if (-not (Test-ElementInSection $item $featureHeader $nextHeader)) {
                continue
            }
            if ($rect.Top -lt $bestFeatureTop) {
                $bestFeature = $item
                $bestFeatureTop = $rect.Top
            }
        }
        if ($null -ne $bestFeature) {
            return (Get-SearchResultClickElement $root $bestFeature $true)
        }
    }

    $nonNetworkHeader = Find-FirstLeftPaneHeader $root @(
        "联系人",
        "群聊",
        "聊天记录",
        "公众号",
        "小程序"
    )
    if ($null -ne $nonNetworkHeader) {
        $headerRect = $nonNetworkHeader.Current.BoundingRectangle
        $nextHeader = Find-NextLeftPaneHeaderBelow $root $headerRect.Bottom
        $bestUnderHeader = $null
        $bestUnderHeaderTop = [double]::PositiveInfinity
        foreach ($item in (Get-SearchTargetCandidates $root $false $true)) {
            $rect = $item.Current.BoundingRectangle
            if (-not (Test-ElementInSection $item $nonNetworkHeader $nextHeader)) {
                continue
            }
            if ($rect.Top -lt $bestUnderHeaderTop) {
                $bestUnderHeader = $item
                $bestUnderHeaderTop = $rect.Top
            }
        }
        if ($null -ne $bestUnderHeader) {
            return (Get-SearchResultClickElement $root $bestUnderHeader $true)
        }
    }

    $networkHeader = Find-FirstLeftPaneHeader $root @(
        "搜一搜",
        "网络结果",
        "网络搜索",
        "搜索网络"
    )
    if ($null -ne $networkHeader) {
        return $null
    }

    return $null
}

function Open-Target-Exact {
    if ($null -ne (Find-VisibleElementByNameInMainPane (Get-WeChatRoot) $target)) {
        return
    }
    $leftTarget = Find-SearchTargetResult
    if ($null -ne $leftTarget) {
        Click-Element $leftTarget | Out-Null
        Wait-Step
        if ($null -ne (Find-VisibleElementByNameInMainPane (Get-WeChatRoot) $target)) {
            return
        }
    }
    Focus-AndFillSearch
    $searchTarget = Find-SearchTargetResult
    if ($null -eq $searchTarget) {
        $msg = "未能在微信窗口或搜索结果中精确找到目标会话: $target。"
        $msg += "已优先查找搜索结果中“功能/联系人/群聊/聊天记录/公众号/小程序”分组下的同名项，并跳过“搜一搜/网络结果”。"
        $msg += "如果搜索结果第一块是网络结果且控件树没有暴露后续分组，脚本会停止，不会按回车选择第一条。"
        $msg += "请运行 --inspect --inspect-filter 文件 查看控件树，或先手动打开目标后使用 --select-mode current。"
        throw $msg
    }
    Click-Element $searchTarget | Out-Null
    Wait-Step
    if ($null -eq (Find-VisibleElementByNameInMainPane (Get-WeChatRoot) $target)) {
        $msg = "已点击匹配项，但未能确认右侧主聊天区是目标会话: $target。"
        $msg += "为避免误发，已停止。"
        throw $msg
    }
}

function Verify-Current-Target {
    $root = Get-WeChatRoot
    $element = Find-VisibleElementByNameInMainPane $root $target
    if ($null -eq $element) {
        throw "当前微信窗口未检测到目标会话: $target。为避免误发，已停止。请先手动打开该会话。"
    }
}

function Focus-MessageInput {
    $root = Get-WeChatRoot
    $rootRect = $root.Current.BoundingRectangle
    $editType = [System.Windows.Automation.ControlType]::Edit
    $typeProperty = [System.Windows.Automation.AutomationElement]::ControlTypeProperty
    $condition = New-Object System.Windows.Automation.PropertyCondition `
        -ArgumentList $typeProperty, $editType
    $items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    $best = $null
    $bestScore = -1
    foreach ($item in $items) {
        $rect = $item.Current.BoundingRectangle
        if ($rect.Width -le 20 -or $rect.Height -le 10) {
            continue
        }
        if (-not (Is-InMainPane $root $item)) {
            continue
        }
        $score = $rect.Top
        if ($score -gt $bestScore) {
            $best = $item
            $bestScore = $score
        }
    }
    if ($null -ne $best) {
        Click-Element $best | Out-Null
        return
    }
    $x = [int]($rootRect.Left + ($rootRect.Width * 0.70))
    $y = [int]($rootRect.Top + ($rootRect.Height * 0.90))
    [KeyboardNative]::SetCursorPos($x, $y) | Out-Null
    Wait-Step
    [KeyboardNative]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
    Wait-Step
    [KeyboardNative]::mouse_event($MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
    Wait-Step
}

function Get-RegionHash {
    param(
        [int]$X,
        [int]$Y,
        [int]$Width,
        [int]$Height
    )
    if ($Width -lt 8 -or $Height -lt 8) {
        throw "截图区域过小，无法计算指纹"
    }
    $bmp = New-Object System.Drawing.Bitmap($Width, $Height)
    $gfx = [System.Drawing.Graphics]::FromImage($bmp)
    $gfx.CopyFromScreen($X, $Y, 0, 0, $bmp.Size)
    $small = New-Object System.Drawing.Bitmap(16, 16)
    $smallGfx = [System.Drawing.Graphics]::FromImage($small)
    $smallGfx.DrawImage($bmp, 0, 0, 16, 16)
    $values = New-Object System.Collections.Generic.List[int]
    for ($yy = 0; $yy -lt 16; $yy++) {
        for ($xx = 0; $xx -lt 16; $xx++) {
            $pixel = $small.GetPixel($xx, $yy)
            $gray = [int](($pixel.R * 0.299) + ($pixel.G * 0.587) + ($pixel.B * 0.114))
            $values.Add($gray)
        }
    }
    $avg = ($values | Measure-Object -Average).Average
    $chars = New-Object System.Text.StringBuilder
    foreach ($value in $values) {
        if ($value -ge $avg) {
            [void]$chars.Append("1")
        } else {
            [void]$chars.Append("0")
        }
    }
    $smallGfx.Dispose()
    $small.Dispose()
    $gfx.Dispose()
    $bmp.Dispose()
    return $chars.ToString()
}

function Get-BoundTitleHash {
    param([object]$Profile)
    $root = Get-WeChatRoot
    $rect = $root.Current.BoundingRectangle
    $x = [int]($rect.Left + ($rect.Width * [double]$Profile.title_x_ratio))
    $y = [int]($rect.Top + ($rect.Height * [double]$Profile.title_y_ratio))
    $w = [int]($rect.Width * [double]$Profile.title_width_ratio)
    $h = [int]($rect.Height * [double]$Profile.title_height_ratio)
    return Get-RegionHash $x $y $w $h
}

function Get-HammingDistance {
    param(
        [string]$A,
        [string]$B
    )
    if ($A.Length -ne $B.Length) {
        return 9999
    }
    $distance = 0
    for ($i = 0; $i -lt $A.Length; $i++) {
        if ($A[$i] -ne $B[$i]) {
            $distance += 1
        }
    }
    return $distance
}

function Get-BoundProfile {
    if ([string]::IsNullOrWhiteSpace($boundProfileJson)) {
        throw "缺少绑定配置，请先运行 --bind"
    }
    return $boundProfileJson | ConvertFrom-Json
}

function Verify-BoundTarget {
    $profile = Get-BoundProfile
    $currentHash = Get-BoundTitleHash $profile
    $distance = Get-HammingDistance $currentHash $profile.title_hash
    if ($distance -gt $hashThreshold) {
        $msg = "绑定会话指纹不匹配，已停止以避免误发。"
        $msg += " target=$($profile.target) distance=$distance threshold=$hashThreshold"
        throw $msg
    }
    return $profile
}

function Focus-BoundInput {
    param([object]$Profile)
    $root = Get-WeChatRoot
    $rect = $root.Current.BoundingRectangle
    $x = [int]($rect.Left + ($rect.Width * [double]$Profile.input_x_ratio))
    $y = [int]($rect.Top + ($rect.Height * [double]$Profile.input_y_ratio))
    [KeyboardNative]::SetCursorPos($x, $y) | Out-Null
    Wait-Step
    [KeyboardNative]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
    Wait-Step
    [KeyboardNative]::mouse_event($MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero)
    Wait-Step
}

function Open-Target-LegacySearch {
    Focus-AndFillSearch
    $searchTarget = Find-SearchTargetResult
    if ($null -eq $searchTarget) {
        $msg = "legacy-search 已停用方向键/回车选择，因为微信第一块结果可能是“网络结果/搜一搜”。"
        $msg += "未能在“功能/联系人/群聊/聊天记录/公众号/小程序”分组下找到目标: $target。"
        $msg += "请运行 --inspect --inspect-filter 文件 查看控件树，或先手动打开目标后使用 --select-mode current。"
        throw $msg
    }
    Click-Element $searchTarget | Out-Null
    Wait-Step
    if ($null -eq (Find-VisibleElementByNameInMainPane (Get-WeChatRoot) $target)) {
        $msg = "legacy-search 已点击匹配项，但未能确认右侧主聊天区是目标会话: $target。"
        $msg += "为避免误发，已停止。"
        throw $msg
    }
}

if (-not (Activate-WeChat)) {
    throw "无法激活微信窗口，请先登录并打开微信客户端"
}

if ($selectMode -eq "bound") {
    Verify-BoundTarget | Out-Null
} elseif ($selectMode -eq "current") {
    Verify-Current-Target
} elseif ($selectMode -eq "legacy-search") {
    Open-Target-LegacySearch
} else {
    Open-Target-Exact
}

if (-not [string]::IsNullOrWhiteSpace($message)) {
    if (-not (Activate-WeChat)) {
        throw "发送文本前微信窗口失焦"
    }
    if ($selectMode -eq "bound") {
        $profile = Verify-BoundTarget
        Focus-BoundInput $profile
    } elseif ($selectMode -ne "legacy-search") {
        Verify-Current-Target
        Focus-MessageInput
    } else {
        Focus-MessageInput
    }
    [System.Windows.Forms.Clipboard]::SetText($message)
    Send-CtrlKey $VK_V
    Wait-Step
    Send-Key $VK_RETURN
    Wait-Step
}

if (-not [string]::IsNullOrWhiteSpace($file)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "文件不存在: $file"
    }
    if (-not (Activate-WeChat)) {
        throw "发送文件前微信窗口失焦"
    }
    if ($selectMode -eq "bound") {
        $profile = Verify-BoundTarget
        Focus-BoundInput $profile
    } elseif ($selectMode -ne "legacy-search") {
        Verify-Current-Target
        Focus-MessageInput
    } else {
        Focus-MessageInput
    }
    $files = New-Object System.Collections.Specialized.StringCollection
    [void]$files.Add($file)
    [System.Windows.Forms.Clipboard]::SetFileDropList($files)
    Send-CtrlKey $VK_V
    Wait-Step 2
    Send-Key $VK_RETURN
    Wait-Step
}
"""


def _windows_bind_script() -> str:
    return r"""
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$wshell = New-Object -ComObject WScript.Shell
$appNames = $env:SPIDER_GD_WECHAT_APP_NAMES -split "\|"
$target = $env:SPIDER_GD_WECHAT_TARGET
$inputXRatio = [double]$env:SPIDER_GD_WECHAT_INPUT_X_RATIO
$inputYRatio = [double]$env:SPIDER_GD_WECHAT_INPUT_Y_RATIO
$titleXRatio = [double]$env:SPIDER_GD_WECHAT_TITLE_X_RATIO
$titleYRatio = [double]$env:SPIDER_GD_WECHAT_TITLE_Y_RATIO
$titleWRatio = [double]$env:SPIDER_GD_WECHAT_TITLE_W_RATIO
$titleHRatio = [double]$env:SPIDER_GD_WECHAT_TITLE_H_RATIO

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class BindNative {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}
"@

function Activate-WeChat {
    foreach ($name in $appNames) {
        if ([string]::IsNullOrWhiteSpace($name)) {
            continue
        }
        if ($wshell.AppActivate($name)) {
            Start-Sleep -Milliseconds 500
            return $true
        }
    }
    return $false
}

function Get-WeChatRoot {
    $hwnd = [BindNative]::GetForegroundWindow()
    if ($hwnd -eq [IntPtr]::Zero) {
        throw "无法读取当前微信窗口句柄"
    }
    return [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
}

function Get-RegionHash {
    param(
        [int]$X,
        [int]$Y,
        [int]$Width,
        [int]$Height
    )
    if ($Width -lt 8 -or $Height -lt 8) {
        throw "截图区域过小，无法计算指纹"
    }
    $bmp = New-Object System.Drawing.Bitmap($Width, $Height)
    $gfx = [System.Drawing.Graphics]::FromImage($bmp)
    $gfx.CopyFromScreen($X, $Y, 0, 0, $bmp.Size)
    $small = New-Object System.Drawing.Bitmap(16, 16)
    $smallGfx = [System.Drawing.Graphics]::FromImage($small)
    $smallGfx.DrawImage($bmp, 0, 0, 16, 16)
    $values = New-Object System.Collections.Generic.List[int]
    for ($yy = 0; $yy -lt 16; $yy++) {
        for ($xx = 0; $xx -lt 16; $xx++) {
            $pixel = $small.GetPixel($xx, $yy)
            $gray = [int](($pixel.R * 0.299) + ($pixel.G * 0.587) + ($pixel.B * 0.114))
            $values.Add($gray)
        }
    }
    $avg = ($values | Measure-Object -Average).Average
    $chars = New-Object System.Text.StringBuilder
    foreach ($value in $values) {
        if ($value -ge $avg) {
            [void]$chars.Append("1")
        } else {
            [void]$chars.Append("0")
        }
    }
    $smallGfx.Dispose()
    $small.Dispose()
    $gfx.Dispose()
    $bmp.Dispose()
    return $chars.ToString()
}

if (-not (Activate-WeChat)) {
    throw "无法激活微信窗口，请先登录并打开微信客户端"
}

$root = Get-WeChatRoot
$rect = $root.Current.BoundingRectangle
$x = [int]($rect.Left + ($rect.Width * $titleXRatio))
$y = [int]($rect.Top + ($rect.Height * $titleYRatio))
$w = [int]($rect.Width * $titleWRatio)
$h = [int]($rect.Height * $titleHRatio)
$hash = Get-RegionHash $x $y $w $h
$record = [ordered]@{
    target = $target
    window_width = [int]$rect.Width
    window_height = [int]$rect.Height
    input_x_ratio = $inputXRatio
    input_y_ratio = $inputYRatio
    title_x_ratio = $titleXRatio
    title_y_ratio = $titleYRatio
    title_width_ratio = $titleWRatio
    title_height_ratio = $titleHRatio
    title_hash = $hash
}
$record | ConvertTo-Json -Compress
"""


def _windows_inspect_script() -> str:
    return r"""
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$wshell = New-Object -ComObject WScript.Shell
$appNames = $env:SPIDER_GD_WECHAT_APP_NAMES -split "\|"
$maxDepth = [int]$env:SPIDER_GD_WECHAT_INSPECT_DEPTH
$filter = $env:SPIDER_GD_WECHAT_INSPECT_FILTER

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class WindowNative {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}
"@

function Activate-WeChat {
    foreach ($name in $appNames) {
        if ([string]::IsNullOrWhiteSpace($name)) {
            continue
        }
        if ($wshell.AppActivate($name)) {
            Start-Sleep -Milliseconds 500
            return $true
        }
    }
    return $false
}

function Should-Print {
    param(
        [string]$Name,
        [string]$ClassName,
        [string]$ControlType
    )
    if ([string]::IsNullOrWhiteSpace($filter)) {
        return $true
    }
    return (
        $Name -like "*$filter*" -or
        $ClassName -like "*$filter*" -or
        $ControlType -like "*$filter*"
    )
}

function Dump-Element {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [int]$Depth
    )
    if ($Depth -gt $maxDepth) {
        return
    }
    $prefix = "  " * $Depth
    $name = $Element.Current.Name
    $className = $Element.Current.ClassName
    $controlType = $Element.Current.ControlType.ProgrammaticName
    $rect = $Element.Current.BoundingRectangle
    if (Should-Print $name $className $controlType) {
        $bounds = "x=$([int]$rect.Left),y=$([int]$rect.Top),"
        $bounds += "w=$([int]$rect.Width),h=$([int]$rect.Height)"
        Write-Output "$prefix- name=[$name] type=[$controlType] class=[$className] $bounds"
    }
    $children = $Element.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($child in $children) {
        Dump-Element $child ($Depth + 1)
    }
}

if (-not (Activate-WeChat)) {
    throw "无法激活微信窗口，请先登录并打开微信客户端"
}

$hwnd = [WindowNative]::GetForegroundWindow()
if ($hwnd -eq [IntPtr]::Zero) {
    throw "无法读取当前微信窗口句柄"
}
$root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
Dump-Element $root 0
"""
