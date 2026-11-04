"""现场侦察工具命令行入口。

子命令：start / network / browser / report / pack / doctor。

``start`` 流程（推荐现场使用）：

1. 提示用户**确认 U-Key 尚未插入**，采集 baseline 路由表/IP 快照。
2. 提示用户**插入 U-Key**，按回车继续，再采集 after 快照并诊断。
3. 启动浏览器侦察（HAR 录制 + 逐页 HTML/截图快照）。
4. 解析 HAR、生成 Markdown 摘要报告。
5. 整个会话目录打 zip。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_TARGET_URL = "https://pm.gd.csg.cn/"


def _new_session_dir() -> Path:
    name = f"recon_{datetime.now():%Y%m%d_%H%M%S}"
    path = Path(name)
    (path / "network").mkdir(parents=True, exist_ok=True)
    (path / "browser").mkdir(parents=True, exist_ok=True)
    (path / "analysis").mkdir(parents=True, exist_ok=True)
    return path


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from recon_tool.doctor import run_doctor

    all_ok = True
    for name, (ok, msg) in run_doctor().items():
        mark = "[OK ]" if ok else "[FAIL]"
        all_ok = all_ok and ok
        print(f"{mark} {name}: {msg}")
    return 0 if all_ok else 1


def _cmd_report(args: argparse.Namespace) -> int:
    from recon_tool.har_analyzer import analyze_har, extract_auth
    from recon_tool.report import build_summary_report

    session = Path(args.dir)
    har = session / "browser" / "session.har"
    if not har.exists():
        print(f"未找到 HAR：{har}")
        return 1

    (session / "analysis").mkdir(parents=True, exist_ok=True)
    analysis = analyze_har(har)
    auth = extract_auth(har)
    (session / "analysis" / "api_endpoints.json").write_text(
        json.dumps(analysis["deduped"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (session / "analysis" / "auth_info.json").write_text(
        json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_text = build_summary_report(analysis, auth)
    (session / "summary_report.md").write_text(report_text, encoding="utf-8")
    print(f"报告已生成：{session / 'summary_report.md'}")
    return 0


def _cmd_pack(args: argparse.Namespace) -> int:
    from recon_tool.packer import pack_dir

    zip_path = pack_dir(args.dir)
    print(f"打包完成：{zip_path}")
    return 0


def _cmd_network(args: argparse.Namespace) -> int:
    from recon_tool.network_recon import run_network_recon

    out = Path(args.dir) / "network" if args.dir else _new_session_dir() / "network"
    baseline = getattr(args, "_baseline_snapshot", None)
    run_network_recon(out, baseline_snapshot=baseline)
    print(f"网络侦察完成：{out}")
    return 0


def _cmd_browser(args: argparse.Namespace) -> int:
    from recon_tool.browser_recon import run_browser_recon

    out = Path(args.dir) / "browser" if args.dir else _new_session_dir() / "browser"
    url = getattr(args, "url", None) or DEFAULT_TARGET_URL
    asyncio.run(run_browser_recon(out, start_url=url))
    print(f"浏览器侦察完成：{out}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from recon_tool.har_replay import run_har_replay

    har_path = Path(args.har_file)
    if not har_path.exists():
        print(f"HAR 文件不存在：{har_path}")
        return 1
    not_found = "abort" if getattr(args, "strict", False) else "fallback"
    asyncio.run(
        run_har_replay(
            har_path,
            start_url=getattr(args, "url", None),
            not_found=not_found,
            headless=getattr(args, "headless", False),
            storage_state=getattr(args, "state", None),
        )
    )
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    from spider_gd.infrastructure.network.diagnoser import NetSnapshot

    session = _new_session_dir()
    args.dir = str(session)
    print(f"侦察会话目录：{session}")

    # Step 1: U-Key 未插入 → baseline
    print("\n[1/4] 准备采集 baseline 网络快照（路由表/IP）。")
    print(">>> 请确认 U-Key **尚未插入**，然后按回车继续 <<<")
    try:
        input()
    except EOFError:
        pass
    baseline = NetSnapshot.take()
    print(
        f"baseline 已采集：默认路由 {len(baseline.default_routes)} 条，"
        f"适配器 {len(baseline.adapters)} 个，DNS {len(baseline.dns_servers)} 项。"
    )

    # Step 2: 插入 U-Key → after + 网络侦察
    print("\n[2/4] 现在请**插入 U-Key**，等待网卡识别完成后按回车继续。")
    try:
        input()
    except EOFError:
        pass
    args._baseline_snapshot = baseline  # noqa: SLF001
    _cmd_network(args)

    # Step 3: 浏览器侦察
    print("\n[3/4] 启动浏览器侦察，请手动登录并浏览各数据页面，完成后关闭浏览器窗口。")
    _cmd_browser(args)

    # Step 4: 报告 + 打包
    print("\n[4/4] 生成报告并打包。")
    _cmd_report(args)
    _cmd_pack(args)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recon", description="现场侦察工具")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # 各子命令的参数定义：(name, func, needs_dir, supports_url)
    specs = [
        ("start", _cmd_start, False, True),
        ("network", _cmd_network, False, False),
        ("browser", _cmd_browser, False, True),
        ("report", _cmd_report, True, False),
        ("pack", _cmd_pack, True, False),
        ("doctor", _cmd_doctor, False, False),
    ]
    for name, func, needs_dir, supports_url in specs:
        p = sub.add_parser(name)
        if needs_dir:
            p.add_argument("dir", help="侦察会话目录")
        else:
            p.add_argument("--dir", help="侦察会话目录（默认新建）")
        if supports_url:
            p.add_argument(
                "--url",
                default=DEFAULT_TARGET_URL,
                help=f"浏览器起始 URL（默认 {DEFAULT_TARGET_URL}）",
            )
        p.set_defaults(func=func)

    # replay 子命令：参数形态与其他子命令不同，单独定义
    p_replay = sub.add_parser("replay", help="基于 HAR 文件离线回放网络请求")
    p_replay.add_argument("har_file", help="HAR 文件路径")
    p_replay.add_argument(
        "--url",
        default=None,
        help="起始 URL（默认从 HAR 推断）",
    )
    p_replay.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：HAR 中未录制的请求直接 abort",
    )
    p_replay.add_argument(
        "--headless",
        action="store_true",
        help="无头模式运行",
    )
    p_replay.add_argument(
        "--state",
        default=None,
        help="storage_state.json 路径（默认自动检测 HAR 同目录）",
    )
    p_replay.set_defaults(func=_cmd_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
