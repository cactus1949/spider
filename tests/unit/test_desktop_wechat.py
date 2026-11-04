from __future__ import annotations

import subprocess
import sys
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from spider_gd.gateway.delivery.desktop_wechat import (
    DesktopWechatOptions,
    DesktopWechatSender,
    _applescript_string,
    _windows_bind_script,
    _windows_inspect_script,
    _windows_send_script,
    detect_platform,
)


@pytest.mark.unit
def test_detect_platform_normalizes_common_names():
    assert detect_platform("Darwin") == "macos"
    assert detect_platform("Windows") == "windows"
    assert detect_platform("Linux") == "unsupported"


@pytest.mark.unit
def test_dry_run_plans_message_without_running_commands():
    calls: list[Sequence[str]] = []

    def runner(cmd: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    sender = DesktopWechatSender(platform_name="Darwin", runner=runner)
    result = sender.send(DesktopWechatOptions(target="文件传输助手", message="hello"))

    assert result.ok is True
    assert result.dry_run is True
    assert calls == []
    assert result.actions == [
        "自动选择本机微信客户端后端",
        "激活微信客户端 (WeChat, 微信)",
        "校验当前微信会话包含目标: 文件传输助手",
        "发送文本消息: 5 字符",
    ]


@pytest.mark.unit
def test_windows_auto_uses_powershell_plan_for_current_selection_by_default():
    sender = DesktopWechatSender(platform_name="Windows")
    result = sender.send(DesktopWechatOptions(target="文件传输助手", message="hello"))

    assert result.ok is True
    assert result.actions[0] == "Windows 使用 PowerShell UIA 后端；wxauto 需显式指定"
    assert "校验当前微信会话包含目标: 文件传输助手" in result.actions


@pytest.mark.unit
def test_real_powershell_default_send_uses_current_selection_without_unstable_flag():
    calls: list[tuple[Sequence[str], dict[str, Any]]] = []

    def runner(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    result = DesktopWechatSender(platform_name="Windows", runner=runner).send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hi",
            backend="powershell",
            dry_run=False,
        )
    )

    assert result.ok is True
    assert calls
    assert calls[0][1]["env"]["SPIDER_GD_WECHAT_SELECT_MODE"] == "current"


@pytest.mark.unit
def test_dry_run_plans_file(tmp_path: Path):
    fp = tmp_path / "result.zip"
    fp.write_bytes(b"zip")

    sender = DesktopWechatSender(platform_name="Windows")
    result = sender.send(DesktopWechatOptions(target="测试群", file_path=fp))

    assert result.ok is True
    assert "发送文件:" in result.actions[-1]
    assert str(fp.resolve()) in result.actions[-1]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("options", "error"),
    [
        (DesktopWechatOptions(target="", message="hi"), "缺少目标会话"),
        (DesktopWechatOptions(target="文件传输助手"), "缺少发送内容"),
        (
            DesktopWechatOptions(target="文件传输助手", file_path="/not/exist.zip"),
            "文件不存在",
        ),
    ],
)
def test_validation_errors(options: DesktopWechatOptions, error: str):
    result = DesktopWechatSender(platform_name="Darwin").send(options)

    assert result.ok is False
    assert error in result.error_msg


@pytest.mark.unit
def test_rejects_unknown_select_mode():
    result = DesktopWechatSender(platform_name="Windows").send(
        DesktopWechatOptions(target="文件传输助手", message="hi", select_mode="guess")
    )

    assert result.ok is False
    assert "未知选择模式" in result.error_msg


@pytest.mark.unit
def test_rejects_unknown_backend():
    result = DesktopWechatSender(platform_name="Windows").send(
        DesktopWechatOptions(target="文件传输助手", message="hi", backend="nope")
    )

    assert result.ok is False
    assert "未知发送后端" in result.error_msg


@pytest.mark.unit
def test_legacy_search_mode_keeps_down_step_plan():
    result = DesktopWechatSender(platform_name="Windows").send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hi",
            select_mode="legacy-search",
            search_down_steps=3,
        )
    )

    assert result.ok is True
    assert "搜索并打开会话: 文件传输助手" in result.actions
    assert "兼容旧搜索参数：不再使用方向键/回车，改用功能/联系人分组安全选择" in result.actions


@pytest.mark.unit
def test_real_powershell_send_rejects_unstable_search_selection():
    result = DesktopWechatSender(platform_name="Windows").send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hi",
            backend="powershell",
            select_mode="exact",
            dry_run=False,
        )
    )

    assert result.ok is False
    assert "真实发送禁止使用搜索式选择模式" in result.error_msg


@pytest.mark.unit
def test_real_powershell_send_allows_unstable_search_when_explicitly_enabled():
    calls: list[Sequence[str]] = []

    def runner(cmd: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    result = DesktopWechatSender(platform_name="Windows", runner=runner).send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hi",
            backend="powershell",
            select_mode="exact",
            allow_unstable_search=True,
            dry_run=False,
        )
    )

    assert result.ok is True
    assert calls


@pytest.mark.unit
def test_real_macos_send_rejects_search_script_without_explicit_enable():
    result = DesktopWechatSender(platform_name="Darwin").send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hi",
            select_mode="current",
            dry_run=False,
        )
    )

    assert result.ok is False
    assert "macOS 本机微信脚本只能通过搜索式 UI 操作选择会话" in result.error_msg


@pytest.mark.unit
def test_bound_mode_rejects_non_windows_platform():
    result = DesktopWechatSender(platform_name="Darwin").send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hi",
            select_mode="bound",
        )
    )

    assert result.ok is False
    assert "绑定模式目前只支持 Windows" in result.error_msg


@pytest.mark.unit
def test_bound_mode_loads_profile_and_passes_it_to_powershell(tmp_path: Path):
    profile_path = tmp_path / "wechat_targets.json"
    profile_path.write_text(
        """{
  "version": 1,
  "targets": {
    "文件传输助手": {
      "target": "文件传输助手",
      "input_x_ratio": 0.7,
      "input_y_ratio": 0.9,
      "title_x_ratio": 0.38,
      "title_y_ratio": 0.04,
      "title_width_ratio": 0.45,
      "title_height_ratio": 0.08,
      "title_hash": "0101"
    }
  }
}""",
        encoding="utf-8",
    )
    calls: list[tuple[Sequence[str], dict[str, Any]]] = []

    def runner(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    result = DesktopWechatSender(platform_name="Windows", runner=runner).send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hello",
            backend="powershell",
            select_mode="bound",
            profile_path=profile_path,
            dry_run=False,
        )
    )

    assert result.ok is True
    env = calls[0][1]["env"]
    assert '"title_hash": "0101"' in env["SPIDER_GD_WECHAT_BOUND_PROFILE"]
    assert env["SPIDER_GD_WECHAT_HASH_THRESHOLD"] == "12"


@pytest.mark.unit
def test_bind_writes_target_profile(tmp_path: Path):
    profile_path = tmp_path / "wechat_targets.json"
    stdout = """{
      "target": "文件传输助手",
      "input_x_ratio": 0.7,
      "input_y_ratio": 0.9,
      "title_x_ratio": 0.38,
      "title_y_ratio": 0.04,
      "title_width_ratio": 0.45,
      "title_height_ratio": 0.08,
      "title_hash": "0101"
    }"""

    def runner(cmd: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    result = DesktopWechatSender(platform_name="Windows", runner=runner).bind(
        target="文件传输助手",
        profile_path=profile_path,
    )

    assert result.ok is True
    assert "已绑定" in result.message
    assert "0101" in profile_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_wxauto_backend_sends_to_target(monkeypatch: pytest.MonkeyPatch):
    sent: list[tuple[str, str, str]] = []
    fake_module = types.ModuleType("wxauto")

    class FakeWeChat:
        def IsOnline(self) -> bool:
            return True

        def SendMsg(self, message: str, target: str, exact: bool = False) -> None:
            sent.append(("msg", message, f"{target}:{exact}"))

        def SendFiles(self, file_path: str, target: str, exact: bool = False) -> None:
            sent.append(("file", Path(file_path).name, f"{target}:{exact}"))

    fake_module.WeChat = FakeWeChat
    monkeypatch.setitem(sys.modules, "wxauto", fake_module)
    monkeypatch.setitem(sys.modules, "wxautox4", None)
    monkeypatch.setitem(sys.modules, "wxauto4", None)

    fp = Path(__file__)
    result = DesktopWechatSender(platform_name="Windows").send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hello",
            file_path=fp,
            backend="wxauto",
            dry_run=False,
        )
    )

    assert result.ok is True
    assert sent == [
        ("msg", "hello", "文件传输助手:True"),
        ("file", Path(__file__).name, "文件传输助手:True"),
    ]


@pytest.mark.unit
def test_real_send_rejects_unsupported_platform():
    sender = DesktopWechatSender(platform_name="Linux")
    result = sender.send(
        DesktopWechatOptions(
            target="文件传输助手",
            message="hi",
            select_mode="current",
            dry_run=False,
        )
    )

    assert result.ok is False
    assert "暂不支持" in result.error_msg


@pytest.mark.unit
def test_applescript_string_escapes_quotes_and_backslashes():
    assert _applescript_string('a"b\\c') == '"a\\"b\\\\c"'


@pytest.mark.unit
def test_windows_script_uses_native_ctrl_shortcuts_instead_of_sendkeys_text():
    script = _windows_send_script()

    assert "keybd_event" in script
    assert "Send-CtrlKey $VK_F" in script
    assert "Find-VisibleElementByName" in script
    assert "Find-VisibleElementByNameInMainPane" in script
    assert "Is-InMainTitleArea" in script
    assert "Find-SearchInput" in script
    assert "Set-SearchInputText" in script
    assert "Find-SearchTargetResult" in script
    assert 'Find-FirstLeftPaneHeader $root @("功能")' in script
    assert "Test-NetworkSearchCandidate" in script
    assert "Get-SearchResultClickElement" in script
    assert "$TrustedSection -or" in script
    assert "Test-ElementInSection" in script
    assert '"搜一搜"' in script
    assert '"网络结果"' in script
    assert '"搜索网络"' in script
    assert '$networkHeader = Find-FirstLeftPaneHeader' in script
    assert "不会按回车选择第一条" in script
    assert "legacy-search 已停用方向键/回车选择" in script
    assert "Send-CtrlKey $VK_A" in script
    assert "Focus-MessageInput" in script
    assert "Verify-BoundTarget" in script
    assert "Focus-BoundInput" in script
    assert "Open-Target-Exact" in script
    assert "Verify-Current-Target" in script
    assert '$wshell.SendKeys("^f")' not in script


@pytest.mark.unit
def test_exact_search_does_not_press_enter_to_pick_first_result():
    script = _windows_send_script()
    exact_body = script.split("function Open-Target-Exact", 1)[1].split(
        "function Verify-Current-Target", 1
    )[0]

    assert "Send-Key $VK_RETURN" not in exact_body
    assert "Find-SearchTargetResult" in exact_body
    assert "Focus-AndFillSearch" in exact_body


@pytest.mark.unit
def test_legacy_search_does_not_press_enter_to_pick_first_result():
    script = _windows_send_script()
    legacy_body = script.split("function Open-Target-LegacySearch", 1)[1].split(
        "if (-not (Activate-WeChat))", 1
    )[0]

    assert "Send-Key $VK_RETURN" not in legacy_body
    assert "Send-Key $VK_DOWN" not in legacy_body
    assert "Focus-AndFillSearch" in legacy_body
    assert "Find-SearchTargetResult" in legacy_body
    assert "legacy-search 已停用方向键/回车选择" in legacy_body


@pytest.mark.unit
def test_inspect_returns_powershell_stdout():
    calls: list[tuple[Sequence[str], dict[str, Any]]] = []

    def runner(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "tree output", "")

    sender = DesktopWechatSender(platform_name="Windows", runner=runner)
    result = sender.inspect(max_depth=3, name_filter="文件")

    assert result.ok is True
    assert result.message == "tree output"
    assert calls
    assert calls[0][1]["env"]["SPIDER_GD_WECHAT_INSPECT_DEPTH"] == "3"
    assert calls[0][1]["env"]["SPIDER_GD_WECHAT_INSPECT_FILTER"] == "文件"


@pytest.mark.unit
def test_windows_inspect_script_dumps_ui_automation_tree():
    script = _windows_inspect_script()

    assert "UIAutomationClient" in script
    assert "Dump-Element" in script
    assert "BoundingRectangle" in script


@pytest.mark.unit
def test_windows_bind_script_computes_title_hash():
    script = _windows_bind_script()

    assert "Get-RegionHash" in script
    assert "title_hash" in script
    assert "ConvertTo-Json" in script
