"""行为指纹对拍：迁移/加固前后，证明 workflow 没有退回会触发瑞数 400 的程序化路径。

用法（迁移前后各 capture 一次，再 compare）::

    # 迁移前
    .venv/bin/python tools/behavior_fingerprint.py capture --mock --out /tmp/fp-a.json \\
        [--workflow workflows/xxx.yml]
    # 迁移后
    .venv/bin/python tools/behavior_fingerprint.py capture --mock --out /tmp/fp-b.json [...]
    .venv/bin/python tools/behavior_fingerprint.py compare --baseline /tmp/fp-a.json --current /tmp/fp-b.json

- ``--mock``：在本机线程里起 mock_site，把 workflow 里的 https://pm.gd.csg.cn 改写到 mock 地址，
  不依赖门户网络。不给 ``--workflow`` 时跑内置的 widget-lab（select + pick_date_range + click）。
- 浏览器用 Playwright Chromium，但启动参数取自 ``shared_browser._build_launch_args``
  （去掉 Chrome 路径、端口、profile），即现场实际下发的 stealth flags。
- 采集内容：
  * launch：启动参数、关键 stealth flag（LocalNetworkAccessChecks 等）是否在位；
  * pointer：真实 pointerdown→pointerup 按压时长、每次按压前的 mousemove 点数、是否全部 isTrusted；
  * keyboard：keydown/keyup 数、按键间隔分布、零按键赋值（input 前没有 keydown，即 page.fill 式）次数；
  * programmatic：Playwright Page/Locator/ElementHandle 的 click/fill/press/type/check/... 调用次数；
  * press_at：``WorkflowRunner._press_at`` 调用次数、trace phase 序列；
  * hover：hover.before→hover.after 窗口内的 mousemove 数与 pointerdown 数（应只移动、不按下）。
- 指纹分两层：``invariants``（必须逐项一致，含所有计数与安全判定）和 ``stats``（随机抖动的时序，
  只做人形区间判定，不参与精确比较）。compare 发现任何 invariants 差异即以 1 退出。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "tools", ROOT / "src", ROOT / "mock_site"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

STEALTH_FLAGS = (
    "LocalNetworkAccessChecks",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
)
# 人形区间：现场手动点击按压 ~225ms；_press_at 取 160~260ms；键入 delay 60~140ms。
HUMAN_PRESS_MS = 120
HUMAN_KEY_INTERVAL_MS = 30
MIN_MOVES_PER_PRESS = 5

DEFAULT_WORKFLOW: dict[str, Any] = {
    "name": "widget-lab 指纹基准",
    "type": "indicator",
    "steps": [
        {"action": "navigate", "url": "https://pm.gd.csg.cn/widget-lab"},
        {"action": "hover", "selector": ".ant-menu-submenu-title:has-text('资讯公告')"},
        {"action": "wait", "selector": ".ant-menu-sub .ant-menu-item", "timeout": 3000},
        {"action": "select", "selector": "#el-select-input", "value": "实时市场"},
        {"action": "select", "selector": "#ant-select .ant-select-selector", "value": "深圳"},
        {"action": "pick_date_range", "start_selector": "#range-start", "end_selector": "#range-end",
         "start_value": "2026-06-01", "end_value": "2026-06-15"},
        {"action": "click", "selector": "#el-select-input"},
    ],
}

_RECORDER_JS = r"""(() => {
  if (window.__fpInstalled) return; window.__fpInstalled = true;
  for (const type of ['pointerdown','pointerup','mousemove','keydown','keyup','input']) {
    document.addEventListener(type, (e) => {
      try { window.__fpEmit({type, t: performance.timeOrigin + performance.now(),
        trusted: e.isTrusted, inputType: e.inputType || null}); } catch (_) {}
    }, true);
  }
})();"""

_WATCHED = {
    "Page": ("click", "dblclick", "fill", "press", "type", "check", "uncheck", "select_option",
             "tap", "hover", "focus", "set_input_files", "dispatch_event", "query_selector",
             "query_selector_all", "wait_for_selector"),
    "Locator": ("click", "dblclick", "fill", "press", "type", "press_sequentially", "check",
                "uncheck", "select_option", "tap", "hover", "focus", "set_input_files",
                "dispatch_event", "element_handle", "element_handles", "scroll_into_view_if_needed"),
    "ElementHandle": ("click", "dblclick", "fill", "press", "type", "check", "uncheck",
                      "select_option", "tap", "hover", "focus", "set_input_files", "dispatch_event"),
}


def launch_args() -> list[str]:
    """取 shared_browser 实际下发的参数，去掉与本机路径/端口相关的项。"""
    from spider_gd.core.crawler.shared_browser import _build_launch_args

    raw = _build_launch_args("chrome", Path("/tmp/fp-profile"), headless=False, cdp_port=9)
    skip = ("--remote-debugging", "--user-data-dir")
    return [a for a in raw[1:] if not a.startswith(skip)]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_mock() -> tuple[str, Any]:
    import uvicorn
    import main as mock_main

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mock_main.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("mock_site 启动超时")
    return f"http://127.0.0.1:{port}", server


def _rewrite(value: Any, base: str) -> Any:
    if isinstance(value, str):
        return value.replace("https://pm.gd.csg.cn", base)
    if isinstance(value, list):
        return [_rewrite(v, base) for v in value]
    if isinstance(value, dict):
        out = {k: _rewrite(v, base) for k, v in value.items()}
        if out.get("action") == "wait" and "ms" in out:
            out["ms"] = min(int(out["ms"]), 50)
        return out
    return value


def _patch_playwright(counter: dict[str, int]) -> list[tuple[type, str, Any]]:
    from playwright.async_api import ElementHandle, Locator, Page

    classes = {"Page": Page, "Locator": Locator, "ElementHandle": ElementHandle}
    restore = []
    for cls_name, methods in _WATCHED.items():
        cls = classes[cls_name]
        for method in methods:
            original = getattr(cls, method, None)
            if original is None:
                continue

            def make(orig, key):
                async def wrapper(self, *args, **kwargs):
                    counter[key] = counter.get(key, 0) + 1
                    return await orig(self, *args, **kwargs)
                return wrapper

            setattr(cls, method, make(original, f"{cls_name}.{method}"))
            restore.append((cls, method, original))
    return restore


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"n": len(values), "min": round(min(values), 1), "median": round(statistics.median(values), 1),
            "max": round(max(values), 1)}


def _hover_windows(phase_times: list[tuple[str, float]]) -> list[tuple[float, float]]:
    windows, start = [], None
    for phase, t in phase_times:
        if phase == "hover.before":
            start = t
        elif phase == "hover.after" and start is not None:
            windows.append((start, t))
            start = None
    return windows


def summarize(events: list[dict], *, args: list[str], programmatic: dict[str, int],
              press_at_calls: int, phases: list[str], workflow: str, ok: bool,
              phase_times: list[tuple[str, float]] | None = None) -> dict:
    """把原始事件折叠成指纹。纯函数，便于单测。"""
    events = sorted(events, key=lambda e: e["t"])
    presses, moves_per_press, key_intervals = [], [], []
    down_at, moves_since, last_key, prev_keydown = None, 0, None, False
    zero_key_inputs, keydowns, keyups = 0, 0, 0
    for e in events:
        kind = e["type"]
        if kind == "mousemove":
            moves_since += 1
        elif kind == "pointerdown":
            down_at = e["t"]
            moves_per_press.append(moves_since)
            moves_since = 0
        elif kind == "pointerup" and down_at is not None:
            presses.append(e["t"] - down_at)
            down_at = None
        elif kind == "keydown":
            keydowns += 1
            if last_key is not None:
                key_intervals.append(e["t"] - last_key)
            last_key = e["t"]
            prev_keydown = True
        elif kind == "keyup":
            keyups += 1
        elif kind == "input":
            if not prev_keydown:
                zero_key_inputs += 1
            prev_keydown = False
    # 只统计连续键入间隔（>2s 视为不同输入框）
    typing_intervals = [i for i in key_intervals if i < 2000]
    windows = _hover_windows(phase_times or [])
    in_hover = lambda e: any(a <= e["t"] <= b for a, b in windows)  # noqa: E731
    hover_moves = [sum(1 for e in events if e["type"] == "mousemove" and a <= e["t"] <= b) for a, b in windows]
    hover_downs = sum(1 for e in events if e["type"] == "pointerdown" and in_hover(e))
    flags = {flag: any(flag in a for a in args) for flag in STEALTH_FLAGS}
    calls = {k: v for k, v in sorted(programmatic.items()) if v}
    invariants = {
        "workflow_ok": ok,
        "stealth_flags": flags,
        "pointer.presses": len(presses),
        "pointer.all_trusted": all(e.get("trusted") for e in events),
        "pointer.press_human": bool(presses) and min(presses) >= HUMAN_PRESS_MS,
        "pointer.moves_before_each_press": bool(moves_per_press) and min(moves_per_press) >= MIN_MOVES_PER_PRESS,
        "keyboard.keydowns": keydowns,
        "keyboard.keyups": keyups,
        "keyboard.zero_key_inputs": zero_key_inputs,
        "keyboard.interval_human": (not typing_intervals) or statistics.median(typing_intervals) >= HUMAN_KEY_INTERVAL_MS,
        "press_at.calls": press_at_calls,
        "press_at.covers_all_presses": press_at_calls == len(presses),
        "hover.count": len(windows),
        "hover.pointerdowns": hover_downs,
        "hover.moves_ok": all(m >= MIN_MOVES_PER_PRESS for m in hover_moves),
        "programmatic_calls": calls,
        "phases": phases,
    }
    return {
        "version": 1,
        "workflow": workflow,
        "launch_args": args,
        "invariants": invariants,
        "stats": {
            "press_ms": _stats(presses),
            "moves_per_press": _stats([float(m) for m in moves_per_press]),
            "hover_moves": hover_moves,
            "mousemove_total": sum(1 for e in events if e["type"] == "mousemove"),
            "key_interval_ms": _stats(typing_intervals),
        },
        "safe": bool(not calls and zero_key_inputs == 0 and hover_downs == 0 and invariants["pointer.all_trusted"]
                     and (not presses or (invariants["pointer.press_human"]
                                          and invariants["press_at.covers_all_presses"]))),
    }


def compare(baseline: dict, current: dict) -> list[str]:
    """返回差异描述；空列表表示一致。stats 只看人形区间，不比具体数值。"""
    diffs = []
    base_inv, cur_inv = baseline.get("invariants", {}), current.get("invariants", {})
    for key in sorted(set(base_inv) | set(cur_inv)):
        if base_inv.get(key) != cur_inv.get(key):
            diffs.append(f"{key}: baseline={base_inv.get(key)!r} current={cur_inv.get(key)!r}")
    if baseline.get("launch_args") != current.get("launch_args"):
        diffs.append(f"launch_args: baseline={baseline.get('launch_args')} current={current.get('launch_args')}")
    if not current.get("safe", False):
        diffs.append("current.safe=false：出现程序化输入/零按键赋值/非人形按压")
    return diffs


async def capture(workflow_path: str | None, base_url: str, *, headless: bool) -> dict:
    import yaml
    from playwright.async_api import async_playwright
    import workflow_runner as wr

    cfg = yaml.safe_load(Path(workflow_path).read_text(encoding="utf-8")) if workflow_path else DEFAULT_WORKFLOW
    cfg = _rewrite(cfg, base_url)
    cfg["trace_browser_actions"] = True
    args = launch_args()
    events: list[dict] = []
    programmatic: dict[str, int] = {}
    press_calls = 0
    phases: list[str] = []
    phase_times: list[tuple[str, float]] = []

    original_press = wr.WorkflowRunner._press_at
    original_trace = wr.WorkflowRunner._trace_browser_action

    async def press_at(self, box):
        nonlocal press_calls
        press_calls += 1
        return await original_press(self, box)

    async def trace(self, phase):
        phases.append(phase)
        phase_times.append((phase, time.time() * 1000))

    wr.WorkflowRunner._press_at = press_at
    wr.WorkflowRunner._trace_browser_action = trace
    restore: list = []
    ok = False
    import tempfile

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, args=[a for a in args if a != "--headless=new"])
            context = await browser.new_context(viewport={"width": 1280, "height": 860}, accept_downloads=True)
            await context.expose_function("__fpEmit", lambda e: events.append(e))
            await context.add_init_script(_RECORDER_JS)
            page = await context.new_page()
            restore = _patch_playwright(programmatic)
            with tempfile.TemporaryDirectory() as tmp:
                runner = wr.WorkflowRunner(cfg, download_dir=tmp, headless=headless)
                runner._pointer_settle_s = 0.05
                context.on("page", runner._on_new_page)
                await runner.run(None, None, browser=browser, context=context, pages=[page])
                ok = not runner.failed
            for cls, name, orig in restore:
                setattr(cls, name, orig)
            restore = []
            await browser.close()
    finally:
        for cls, name, orig in restore:
            setattr(cls, name, orig)
        wr.WorkflowRunner._press_at = original_press
        wr.WorkflowRunner._trace_browser_action = original_trace
    return summarize(events, args=args, programmatic=programmatic, press_at_calls=press_calls,
                     phases=phases, workflow=str(workflow_path or "builtin:widget-lab"), ok=ok,
                     phase_times=phase_times)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture", aliases=["baseline"], help="跑 workflow 并生成指纹 JSON")
    cap.add_argument("--workflow", help="workflow yml；不填则用内置 widget-lab 基准")
    cap.add_argument("--mock", action="store_true", help="启动 mock_site 并改写门户地址（推荐）")
    cap.add_argument("--base-url", help="不用 --mock 时，把 https://pm.gd.csg.cn 改写成该地址")
    cap.add_argument("--headed", action="store_true")
    cap.add_argument("--out", required=True)
    cmp_ = sub.add_parser("compare", help="与基线对比，不一致时退出码 1")
    cmp_.add_argument("--baseline", required=True)
    cmp_.add_argument("--current", required=True)
    ns = parser.parse_args(argv)

    if ns.cmd in ("capture", "baseline"):
        server = None
        if ns.mock:
            base, server = _start_mock()
        elif ns.base_url:
            base = ns.base_url.rstrip("/")
        else:
            parser.error("capture 需要 --mock 或 --base-url")
        try:
            fp = asyncio.run(capture(ns.workflow, base, headless=not ns.headed))
        finally:
            if server is not None:
                server.should_exit = True
        Path(ns.out).write_text(json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"指纹已写入 {ns.out}；safe={fp['safe']} workflow_ok={fp['invariants']['workflow_ok']}")
        return 0 if fp["safe"] and fp["invariants"]["workflow_ok"] else 2

    baseline = json.loads(Path(ns.baseline).read_text(encoding="utf-8"))
    current = json.loads(Path(ns.current).read_text(encoding="utf-8"))
    diffs = compare(baseline, current)
    if diffs:
        print("行为指纹不一致：")
        for line in diffs:
            print("  - " + line)
        return 1
    print("行为指纹一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
