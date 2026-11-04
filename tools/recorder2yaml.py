#!/usr/bin/env python3
"""Chrome DevTools Recorder JSON → spider-gd 工作流 YAML 转换器。

用法:
    # 1. 在 Chrome DevTools → Recorder 面板录制操作
    # 2. 导出为 JSON 文件
    # 3. 转换为 YAML:
    python tools/recorder2yaml.py recording.json -o workflows/my_workflow.yml

也支持 Playwright Codegen 记录的手动脚本（通过注释提取步骤）:
    npx playwright codegen --target=python https://pm.gd.csg.cn
    # 复制终端输出的 Python 代码保存为 .py 文件
    python tools/recorder2yaml.py recording.py -o workflows/my_workflow.yml

转换规则:
    - navigate  → action: navigate
    - click     → action: click（自动检测 expect: new_tab）
    - change    → action: fill / pick_date
    - keyDown   → 合并到上一步（如 Enter 用于日期确认）
    - waitForElement → action: wait
    - scroll    → 暂跳过（页面自动滚动）
    - hover     → action: hover（可选）

智能检测:
    - 点击含「导出」文本 → action: export
    - 选择器含 date-picker / datepicker → action: pick_date
    - 连续两个 change 在 date-picker 上 → action: pick_date_range
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

# ----- selector 优先级: 文本 > aria > css -----

_CSS_PATTERN = re.compile(r"^(?:\.|#|[a-z]|\[|button|input|div|span|table|tr|td|th)")


def _pick_selector(selectors: list[list[str]]) -> str:
    """从一组候选中择出最优选择器。"""
    flat: list[str] = []
    for group in selectors:
        flat.extend(group)

    # 1. 文本选择器最稳定
    for s in flat:
        if s.startswith("text=") and len(s) < 80:
            return s

    # 2. aria 选择器
    for s in flat:
        if s.startswith("aria/") and len(s) < 80:
            return s

    # 3. CSS 选择器
    for s in flat:
        if _CSS_PATTERN.match(s) and len(s) < 120:
            return s

    # fallback
    return flat[0] if flat else ""


def _looks_like_export(sel: str, value: str = "") -> bool:
    """判断是否点击的是导出按钮。"""
    patterns = [
        "导出", "export", "download", "下载", ".el-button--primary",
        "button:has-text('导出')",
    ]
    for p in patterns:
        if p in sel or p in value:
            return True
    return False


def _looks_like_date_picker(sel: str) -> bool:
    return any(k in sel.lower() for k in ["date-picker", "datepicker", "el-date-editor"])


def _looks_like_query(sel: str) -> bool:
    return any(k in sel for k in ["查询", "query", "search", "搜索"])


# ----- Chrome DevTools Recorder JSON → steps -----

def convert_devtools_recording(data: dict) -> list[dict]:
    """转换 Chrome DevTools Recorder JSON 到 spider-gd 步骤列表。"""
    steps: list[dict] = []
    raw_steps: list[dict] = data.get("steps", [])

    # 第一遍：navigate 类步骤直接作为 login（如果是第一个）
    #         后续 navigate 作为 navigate
    has_login = False

    for i, raw in enumerate(raw_steps):
        rtype = raw.get("type", "")
        selectors = raw.get("selectors", [[]])
        sel = _pick_selector(selectors)
        value = raw.get("value", "")
        url = raw.get("url", "")
        target = raw.get("target", "main")
        asserted_events = raw.get("assertedEvents", [])

        # --- navigate ---
        if rtype == "navigate":
            if not has_login and i == 0:
                steps.append({"action": "login", "url": url})
                has_login = True
            else:
                expect_new_tab = any(
                    e.get("type") == "navigation" for e in asserted_events
                )
                step: dict = {"action": "navigate", "url": url}
                if expect_new_tab:
                    step["expect"] = "new_tab"
                steps.append(step)
            continue

        # --- click ---
        if rtype == "click":
            # 检测是否为导出按钮
            if _looks_like_export(sel, value):
                steps.append({
                    "action": "export",
                    "selector": sel,
                    "save_as": "export_{date}.xlsx",
                })
                continue

            # 检测是否为查询按钮（标记一下，后续可能配合 export）
            if _looks_like_query(sel):
                steps.append({"action": "click", "selector": sel})
                # 后续如果有 waitForElement，说明查询触发了刷新
                continue

            # 普通点击：检测是否触发新标签页
            expect_new_tab = any(
                e.get("type") == "navigation" for e in asserted_events
            ) or target == "new_tab"
            step = {"action": "click", "selector": sel}
            if expect_new_tab:
                step["expect"] = "new_tab"
            steps.append(step)
            continue

        # --- change（输入框填写） ---
        if rtype == "change":
            if _looks_like_date_picker(sel):
                # 检查下一步是否也是 change（可能是日期范围）
                next_raw = raw_steps[i + 1] if i + 1 < len(raw_steps) else None
                if next_raw and next_raw.get("type") == "change" and _looks_like_date_picker(
                    _pick_selector(next_raw.get("selectors", [[]]))
                ):
                    # 日期范围（两个连续的 change 在 date-picker 上）
                    # 跳过，由下一步合并处理
                    continue

                # 单日期（检查前面是否已经有 start 了）
                prev_step = steps[-1] if steps else None
                if prev_step and prev_step.get("action") == "pick_date_range_pending":
                    # 这是 end_date
                    prev_step["action"] = "pick_date_range"
                    prev_step["end_selector"] = sel
                    prev_step["end_value"] = value
                    continue
                else:
                    steps.append({
                        "action": "pick_date",
                        "selector": sel,
                        "value": value or "yesterday",
                    })
            else:
                steps.append({"action": "fill", "selector": sel, "value": value})
            continue

        # --- keyDown (Enter/Escape) ---
        if rtype == "keyDown" or rtype == "keyboardPress":
            key = raw.get("key", "")
            if key == "Enter":
                # Merge with previous step (typically date fill confirmed by Enter)
                # No separate step needed in YAML — pick_date/fill already press Enter
                pass
            elif key == "Escape":
                # Modal dismiss
                steps.append({"action": "dismiss_modal"})
            continue

        # --- waitForElement ---
        if rtype == "waitForElement":
            steps.append({
                "action": "wait",
                "selector": sel,
                "timeout": 15000,
            })
            continue

        # --- scroll ---
        if rtype == "scroll":
            # 通常不需要显式 scroll，跳过
            continue

        # --- hover ---
        if rtype == "hover":
            steps.append({"action": "hover", "selector": sel})
            continue

    return steps


# ----- Playwright Python script → steps (简单正则提取) -----

_PW_GOTO = re.compile(r"page\.goto\('([^']+)'\)")
_PW_CLICK = re.compile(r"page\.(?:locator|get_by_text|get_by_role)\(['\"]([^'\"]+)['\"]\).*?\.click\(\)")
_PW_FILL = re.compile(r"page\.(?:locator|get_by_label|get_by_placeholder)\(['\"]([^'\"]+)['\"]\).*?\.(?:fill|press_sequentially)\(['\"]([^'\"]*)['\"]\)")
_PW_WAIT = re.compile(r"page\.wait_for_(?:selector|timeout)\(['\"]?([^'\"]+)['\"]?")


def convert_playwright_script(code: str) -> list[dict]:
    """从 Playwright Python 录制脚本中提取步骤。"""
    steps: list[dict] = []
    lines = code.split("\n")

    for line in lines:
        line = line.strip()

        m = _PW_GOTO.search(line)
        if m:
            url = m.group(1)
            if not steps:
                steps.append({"action": "login", "url": url})
            else:
                steps.append({"action": "navigate", "url": url})
            continue

        m = _PW_CLICK.search(line)
        if m:
            sel = m.group(1)
            if _looks_like_export(sel):
                steps.append({"action": "export", "selector": sel, "save_as": "export_{date}.xlsx"})
            else:
                steps.append({"action": "click", "selector": sel})
            continue

        m = _PW_FILL.search(line)
        if m:
            loc = m.group(1)
            val = m.group(2)
            if _looks_like_date_picker(loc):
                steps.append({"action": "pick_date", "selector": loc, "value": val or "yesterday"})
            else:
                steps.append({"action": "fill", "selector": loc, "value": val})
            continue

        m = _PW_WAIT.search(line)
        if m:
            steps.append({"action": "wait", "selector": m.group(1), "timeout": 15000})
            continue

    return steps


# ----- 后处理：智能合并 -----

def postprocess(steps: list[dict]) -> list[dict]:
    """后处理：合并连续相关步骤，推断缺失 action。"""
    if not steps:
        return steps

    merged: list[dict] = []

    for i, step in enumerate(steps):
        action = step.get("action", "")

        # 合并连续的 pick_date → pick_date_range
        if action == "pick_date":
            next_step = steps[i + 1] if i + 1 < len(steps) else None
            if next_step and next_step.get("action") == "pick_date":
                # 两个连续的 pick_date → pick_date_range
                merged.append({
                    "action": "pick_date_range",
                    "start_selector": step.get("selector", ".el-date-picker input:nth-child(1)"),
                    "end_selector": next_step.get("selector", ".el-date-picker input:nth-child(2)"),
                    "start_value": step.get("value", "-7d"),
                    "end_value": next_step.get("value", "yesterday"),
                })
                # 标记 next 已消费
                steps[i + 1] = {"action": "_consumed"}
                continue

        if action == "_consumed":
            continue

        # click(query) + wait → 加上如果后面是 export，保持不变
        merged.append(step)

    return merged


# ----- 主转换 -----

def convert_to_yaml(steps: list[dict], name: str = "录制工作流", description: str = "") -> str:
    """将 step 列表转成 YAML 字符串。"""
    import yaml

    workflow: dict[str, Any] = {
        "name": name,
        "description": description or f"从录制自动生成 — {date.today().isoformat()}",
        "steps": steps,
    }

    # 使用自定义 representer 输出更友好的 YAML
    class _StepDumper(yaml.Dumper):
        pass

    def _dict_representer(dumper, data):
        return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=False)

    _StepDumper.add_representer(dict, _dict_representer)

    return yaml.dump(
        workflow,
        Dumper=_StepDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def detect_format(filepath: Path) -> str:
    """检测输入文件格式。"""
    suffix = filepath.suffix.lower()
    if suffix == ".json":
        return "devtools"
    if suffix == ".py":
        return "playwright"
    # 尝试读取内容判断
    content = filepath.read_text(encoding="utf-8")[:500]
    if content.strip().startswith("{"):
        return "devtools"
    if "page.goto" in content or "page.locator" in content:
        return "playwright"
    return "unknown"


def main():
    parser = argparse.ArgumentParser(
        description="Chrome DevTools Recorder / Playwright → spider-gd 工作流 YAML 转换器"
    )
    parser.add_argument("input", help="录制 JSON/PY 文件路径")
    parser.add_argument("-o", "--output", required=True, help="输出 YAML 文件路径")
    parser.add_argument("-n", "--name", default="录制工作流", help="工作流名称")
    parser.add_argument("-d", "--description", default="", help="工作流描述")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在 {input_path}", file=sys.stderr)
        sys.exit(1)

    fmt = detect_format(input_path)
    content = input_path.read_text(encoding="utf-8")

    if fmt == "devtools":
        data = json.loads(content)
        steps = convert_devtools_recording(data)
    elif fmt == "playwright":
        steps = convert_playwright_script(content)
    else:
        print(f"错误: 无法识别文件格式 {input_path}, 支持 .json (DevTools Recorder) 或 .py (Playwright codegen)", file=sys.stderr)
        sys.exit(1)

    steps = postprocess(steps)

    if not steps:
        print("警告: 未提取到任何步骤", file=sys.stderr)
        sys.exit(1)

    yaml_text = convert_to_yaml(steps, name=args.name, description=args.description)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_text, encoding="utf-8")

    print(f"✅ 转换完成: {len(steps)} 个步骤")
    for i, s in enumerate(steps):
        action = s.get("action", "?")
        sel = s.get("selector", s.get("url", ""))
        print(f"   {i+1}. {action}: {sel}")
    print(f"💾 已保存 → {output_path}")

    # 额外提示
    print("\n📝 后续可能需要手动调整:")
    print("   - 把日期值从录制时的具体日期替换为 yesterday / -7d 等相对值")
    print("   - 把 export 的 save_as 改为有意义的名字，如 信息披露_{date}.xlsx")
    print("   - 添加 screenshot / extract 步骤补充数据采集")
    print("   - 微调 wait 的 timeout 值")


if __name__ == "__main__":
    main()
