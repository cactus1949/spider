"""WiFi 识别自检：本机 netsh 是否可用、当前 SSID、附近网络、网卡列表。

用法::

    .venv\\Scripts\\python.exe tools\\check_wifi.py

排查要点：后端以 ``PYTHONUTF8=1`` 启动时，``subprocess.run(text=True)`` 会用 UTF-8
解码 netsh 的 cp936 输出，失败后 ``stdout`` 变成 ``None``（界面显示“命令未返回
输出”）。本脚本会同时打印“旧写法”和 ``run_capture``（二进制捕获 + 控制台编码
解码）两种结果，用于确认环境是否命中该问题。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from spider_gd.infrastructure.network import WifiConnector  # noqa: E402
from spider_gd.infrastructure.proc import run_capture  # noqa: E402

_STEP = "=" * 72


def _sep(title: str) -> None:
    print(f"\n{_STEP}\n{title}\n{_STEP}")


def main() -> int:
    _sep("运行环境")
    print(f"python          : {sys.executable}")
    print(f"sys.flags.utf8_mode : {sys.flags.utf8_mode} (1 表示 PYTHONUTF8=1)")
    print(f"stdout encoding : {getattr(sys.stdout, 'encoding', None)}")

    _sep("netsh 原始输出对比")
    legacy = subprocess.run(
        ["netsh", "wlan", "show", "interfaces"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    print(f"旧写法 text=True   : returncode={legacy.returncode} "
          f"stdout={'None（解码失败被吞）' if legacy.stdout is None else len(legacy.stdout)}")
    fixed = run_capture(["netsh", "wlan", "show", "interfaces"], timeout=10)
    print(f"run_capture        : returncode={fixed.returncode} stdout={len(fixed.stdout)} 字符")
    if not fixed.stdout.strip():
        print("!! netsh 没有返回内容：检查 WLAN AutoConfig(wlansvc) 服务与无线网卡")
        return 1

    connector = WifiConnector()
    _sep("当前 WiFi")
    for line in fixed.stdout.splitlines():
        if ":" in line and line.split(":", 1)[0].strip().upper().startswith("SSID"):
            print(f"netsh 原始值: {line.split(':', 1)[1].strip() or '<空>'}"
                  "   （含非 ASCII 的 SSID，netsh 输出十六进制字节串）")
    current = connector.current_ssid()
    print(f"SSID: {current or '<未连接>'}")

    _sep("附近 WiFi")
    for item in connector.list_networks():
        mark = "*" if item.current else " "
        print(f"{mark} {item.ssid:<32} {item.signal:>5}  {item.security}")

    _sep("网卡")
    interfaces = run_capture(["netsh", "interface", "show", "interface"], timeout=10)
    print(interfaces.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
