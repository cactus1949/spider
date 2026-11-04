"""日期窗口。D 是运行当天，区间用 D-2、D、D+1 这种偏移表示。"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date, timedelta

import yaml

from spider_gd.core.workflow_outputs import OUTPUT_ACTIONS, workflow_outputs

_MONTH_SPAN_VALUES = {"previous_month_start", "previous_month_end"}
_WINDOW_MIN = -31
_WINDOW_MAX = 14
_PAGE_CLICK_MARKERS = ("el-menu", "el-submenu")


def format_d_offset(offset: int) -> str:
    value = int(offset)
    if value == 0:
        return "D"
    if value > 0:
        return f"D+{value}"
    return f"D{value}"


def coerce_window(start, end, default: int = -1) -> tuple[int, int]:
    """把开始、结束收成合法偏移。结束早于开始时对调。"""
    def _one(value, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = fallback
        return max(_WINDOW_MIN, min(_WINDOW_MAX, parsed))

    left = _one(start, default)
    right = _one(end, default)
    if right < left:
        left, right = right, left
    return left, right


def expand_d_window(start: int, end: int, reference: date) -> list[date]:
    """展开闭区间。D-2 到 D 在 2026-10-29 是 27、28、29 三天。"""
    left, right = coerce_window(start, end)
    return [reference + timedelta(days=offset) for offset in range(left, right + 1)]


_MONTH_WINDOW_MIN = -12
_MONTH_WINDOW_MAX = 0


def format_m_offset(offset: int) -> str:
    value = int(offset)
    if value == 0:
        return "M"
    if value > 0:
        return f"M+{value}"
    return f"M{value}"


def coerce_month_window(start, end, default: int = -1) -> tuple[int, int]:
    """月份窗口。M 是运行当天所在月，M-1 是上一个整月。"""
    def _one(value, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = fallback
        return max(_MONTH_WINDOW_MIN, min(_MONTH_WINDOW_MAX, parsed))

    left = _one(start, default)
    right = _one(end, default)
    if right < left:
        left, right = right, left
    return left, right


def shift_month(anchor: date, offset: int) -> date:
    """从某月 1 日再偏移若干月。负偏移是往前。"""
    index = anchor.year * 12 + (anchor.month - 1) + int(offset)
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, 1)


def expand_m_window(start: int, end: int, reference: date) -> list[date]:
    """展开月份闭区间。2026-10-29 的 M-1 到 M-1 是 2026-09-01。"""
    left, right = coerce_month_window(start, end)
    anchor = reference.replace(day=1)
    return [shift_month(anchor, offset) for offset in range(left, right + 1)]


_WEEK_WINDOW_MIN = -52
_WEEK_WINDOW_MAX = 12


def coerce_week_window(start, end, default: int = 0) -> tuple[int, int]:
    def one(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(_WEEK_WINDOW_MIN, min(_WEEK_WINDOW_MAX, parsed))

    left, right = one(start), one(end)
    return (left, right) if left <= right else (right, left)


def expand_w_window(start: int, end: int, reference: date) -> list[date]:
    """W is the Monday of the current ISO week; each week runs once."""
    left, right = coerce_week_window(start, end)
    monday = reference - timedelta(days=reference.weekday())
    return [monday + timedelta(weeks=offset) for offset in range(left, right + 1)]


def weeks_of_month(month_day: date) -> list[date]:
    """与目标月相交的所有周的周一（含月初/月末跨月周），按时间顺序。

    按月选、按周执行时用：周视图一次只读一周，一个月要翻 4~6 次周。
    """
    first = month_day.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    monday = first - timedelta(days=first.weekday())
    weeks: list[date] = []
    while monday <= last:
        weeks.append(monday)
        monday += timedelta(days=7)
    return weeks


def normalize_target_months(values) -> list[date]:
    """月份入参。接受 YYYY-MM 或任意该月中的一天，结果都落到当月 1 日。"""
    if values is None:
        return []
    if isinstance(values, (str, date)):
        values = [values]
    parsed: list[date] = []
    seen: set[date] = set()
    for item in values:
        if item is None or item == "":
            continue
        if isinstance(item, date):
            current = item.replace(day=1)
        else:
            text = str(item).strip()
            try:
                if re.fullmatch(r"\d{4}-\d{2}", text):
                    year, month = text.split("-")
                    current = date(int(year), int(month), 1)
                else:
                    current = date.fromisoformat(text).replace(day=1)
            except ValueError as exc:
                raise ValueError(f"月份格式应为 YYYY-MM：{text}") from exc
        if current not in seen:
            seen.add(current)
            parsed.append(current)
    parsed.sort()
    return parsed


def normalize_target_dates(values) -> list[date]:
    """去重并按从早到晚排列。空值跳过，非法日期直接报错。"""
    if values is None:
        return []
    if isinstance(values, (str, date)):
        values = [values]
    parsed: list[date] = []
    seen: set[date] = set()
    for item in values:
        if item is None or item == "":
            continue
        if isinstance(item, date):
            current = item
        else:
            text = str(item).strip()
            try:
                current = date.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(f"日期格式应为 YYYY-MM-DD：{text}") from exc
        if current not in seen:
            seen.add(current)
            parsed.append(current)
    parsed.sort()
    return parsed


def _normalize_named_periods(raw, normalizer, *, keep_empty: bool = False) -> dict[str, list[date]]:
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    named: dict[str, list[date]] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        periods = normalizer(value or [])
        if periods or keep_empty:
            named[name] = periods
    return named


def normalize_output_dates(raw) -> dict[str, list[date]]:
    """补数据时单个导出另选的日期，键是 ``指标::输出key``。空列表表示「不补」。"""
    return _normalize_named_periods(raw, normalize_target_dates, keep_empty=True)


def normalize_indicator_dates(raw) -> dict[str, list[date]]:
    """补数据时每个指标自己的日期。空表示这个指标没单独指定。"""
    return _normalize_named_periods(raw, normalize_target_dates)


def normalize_indicator_months(raw) -> dict[str, list[date]]:
    """补数据时每个指标自己的月份。结果落到每月 1 日。"""
    return _normalize_named_periods(raw, normalize_target_months)


def normalize_indicator_windows(raw) -> dict[str, tuple[int, int]]:
    """任务上每个指标自己的日期窗口。空表示还没单独配过。"""
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    windows: dict[str, tuple[int, int]] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        if isinstance(value, dict):
            start, end = coerce_window(value.get("start"), value.get("end"))
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            start, end = coerce_window(value[0], value[1])
        else:
            continue
        windows[name] = (start, end)
    return windows


def is_relative_day(value) -> bool:
    """能被某一天替换的日期。上月起止不是。"""
    if value is None:
        return True
    text = str(value).strip()
    if text in {"", "today", "yesterday", "{target}", "target"}:
        return True
    return bool(re.fullmatch(r"-\d+d", text))


def _pick_values(steps) -> list[str]:
    values: list[str] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        if step.get("action") == "pick_date":
            values.append(str(step.get("value") or ""))
        elif step.get("action") == "repeat":
            values.extend(_pick_values(step.get("steps")))
    return values


def is_month_workflow(cfg: dict | None) -> bool:
    """按月指标整段只查一次。显式 date_kind 优先于步骤猜测。"""
    if not isinstance(cfg, dict):
        return False
    declared = str(cfg.get("date_kind") or "")
    if declared == "month":
        return True
    if declared == "day":
        return False
    values = _pick_values(cfg.get("steps"))
    return bool(values) and all(value in _MONTH_SPAN_VALUES for value in values)


def read_date_profile(cfg: dict | None) -> dict:
    """读出指标上的日/月和默认窗口。没写窗口时不把它当成已经配置。"""
    if not isinstance(cfg, dict):
        return {"kind": "day", "start": -1, "end": -1, "has_repeat": False, "has_window": False}
    declared = str(cfg.get("date_kind") or "")
    steps = cfg.get("steps") or []
    legacy_week = (
        declared not in {"day", "month"}
        and any(isinstance(step, dict) and step.get("action") == "extract_calendar" for step in steps)
        and any(isinstance(step, dict) and step.get("action") == "click"
                and "周" in str(step.get("selector") or "") for step in steps)
    )
    kind = "week" if declared == "week" or legacy_week else "month" if is_month_workflow(cfg) else "day"
    coerce = coerce_week_window if kind == "week" else coerce_month_window if kind == "month" else coerce_window
    start, end = (0, 0) if kind == "week" else (-1, -1)
    has_window = False
    has_repeat = False
    window = cfg.get("date_window") if isinstance(cfg.get("date_window"), dict) else None
    if window is not None:
        start, end = coerce(window.get("start", start), window.get("end", end))
        has_window = True
    for step in cfg.get("steps") or []:
        if isinstance(step, dict) and step.get("action") == "repeat":
            has_repeat = True
            if "start" in step or "end" in step:
                start, end = coerce(step.get("start", start), step.get("end", end))
                has_window = True
            break
    return {
        "kind": kind,
        "start": start,
        "end": end,
        "has_repeat": has_repeat,
        "has_window": has_window,
    }


def _as_target_pick(step: dict) -> dict:
    copied = deepcopy(step)
    if copied.get("action") == "pick_date" and is_relative_day(copied.get("value")):
        copied["value"] = "{target}"
    return copied


def _is_page_change(step: dict) -> bool:
    action = step.get("action")
    if action in {"navigate", "login", "ensure_login", "switch_tab", "close_tab", "close_other_tabs", "repeat"}:
        return True
    if action != "click":
        return False
    if str(step.get("expect") or "") in {"new_tab", "navigation", "navigate"}:
        return True
    selector = str(step.get("selector") or "")
    return any(marker in selector for marker in _PAGE_CLICK_MARKERS)


def _next_meaningful(steps: list, index: int) -> dict | None:
    while index < len(steps):
        step = steps[index]
        if isinstance(step, dict) and step.get("action") != "wait":
            return step
        index += 1
    return None


def _ends_date_group(steps: list, index: int) -> bool:
    step = steps[index]
    if not isinstance(step, dict):
        return True
    if _is_page_change(step) or step.get("action") == "pick_date":
        return True
    if step.get("action") == "click":
        nxt = _next_meaningful(steps, index + 1)
        if isinstance(nxt, dict) and nxt.get("action") == "pick_date":
            return True
    return False


def _wrap_day_loops(steps: list, start: int, end: int) -> list:
    """把同一页上的选日期和随后的导出收成循环。换页点击留在循环外面。"""
    out: list = []
    index = 0
    while index < len(steps):
        step = steps[index]
        if not isinstance(step, dict):
            out.append(step)
            index += 1
            continue
        if step.get("action") == "repeat":
            updated = deepcopy(step)
            updated["start"] = start
            updated["end"] = end
            updated["steps"] = [
                _as_target_pick(item) if isinstance(item, dict) else item
                for item in (updated.get("steps") or [])
            ]
            out.append(updated)
            index += 1
            continue
        if step.get("action") == "pick_date" and is_relative_day(step.get("value")):
            group = [_as_target_pick(step)]
            index += 1
            while index < len(steps) and not _ends_date_group(steps, index):
                group.append(deepcopy(steps[index]))
                index += 1
            out.append({"action": "repeat", "start": start, "end": end, "steps": group})
            continue
        out.append(deepcopy(step))
        index += 1
    return out


def _unwrap_repeats(steps: list) -> list:
    """拆掉循环。导出上单独写的范围只对按日循环有意义，一起去掉。"""
    out: list = []
    for step in steps or []:
        if isinstance(step, dict) and step.get("action") == "repeat":
            out.extend(deepcopy(step.get("steps") or []))
        else:
            out.append(deepcopy(step))
    for step in out:
        if isinstance(step, dict) and step.get("action") in OUTPUT_ACTIONS:
            step.pop("start", None)
            step.pop("end", None)
    return out


def apply_date_profile(cfg: dict, kind: str, start: int = -1, end: int = -1) -> dict:
    """写上日/月默认窗口，并把按日的选日期导出收进循环。"""
    copied = deepcopy(cfg) if isinstance(cfg, dict) else {}
    steps = list(copied.get("steps") or [])
    if kind != "week":
        steps = [step for step in steps if not (
            isinstance(step, dict) and step.get("action") == "select_calendar_week"
        )]
    ordered: dict = {}
    if kind == "week":
        left, right = coerce_week_window(start, end)
        if not any(isinstance(step, dict) and step.get("action") == "extract_calendar" for step in steps) or not any(
            isinstance(step, dict) and step.get("action") == "click"
            and "周" in str(step.get("selector") or "") for step in steps
        ):
            raise ValueError("按周需要交易日历的周视图步骤")
        ordered["date_kind"] = "week"
        ordered["date_window"] = {"start": left, "end": right}
        copied.pop("date_window", None)
        copied.pop("date_kind", None)
        for key, value in copied.items():
            ordered[key] = value
        result_steps = []
        for step in _unwrap_repeats(steps):
            if isinstance(step, dict) and step.get("action") == "select_calendar_week":
                continue
            if isinstance(step, dict) and step.get("action") == "extract_calendar":
                result_steps.append({"action": "select_calendar_week"})
                step = {**step, "month_mode": "week", "include_other": True}
            result_steps.append(step)
        ordered["steps"] = result_steps
        return ordered
    if kind == "month":
        left, right = coerce_month_window(start, end)
        ordered["date_kind"] = "month"
        ordered["date_window"] = {"start": left, "end": right}
        copied.pop("date_window", None)
        copied.pop("date_kind", None)
        for key, value in copied.items():
            ordered[key] = value
        ordered["steps"] = _unwrap_repeats(steps)
        return ordered
    left, right = coerce_window(start, end)
    ordered["date_kind"] = "day"
    ordered["date_window"] = {"start": left, "end": right}
    copied.pop("date_kind", None)
    copied.pop("date_window", None)
    for key, value in copied.items():
        ordered[key] = value
    ordered["steps"] = _wrap_day_loops(steps, left, right)
    return ordered


def render_workflow_yaml(original: str, cfg: dict) -> str:
    """保留文件开头的注释，其余按整理后的配置输出。"""
    header: list[str] = []
    for line in (original or "").splitlines():
        if line.startswith("#"):
            header.append(line)
            continue
        if line.strip():
            break
        if header:
            header.append(line)
    dumped = yaml.safe_dump(
        cfg,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )
    if header:
        return "\n".join(header).rstrip() + "\n" + dumped
    return dumped


def resolve_repeat_dates(step: dict | None, config: dict | None, override, today: date) -> list[date]:
    """有入参就用入参。没有就用循环上的默认窗口，从运行当天展开。"""
    if override is not None:
        return list(override)
    window = None
    if isinstance(step, dict) and ("start" in step or "end" in step):
        window = step
    elif isinstance(config, dict) and isinstance(config.get("date_window"), dict):
        window = config["date_window"]
    window = window or {}
    if isinstance(config, dict) and str(config.get("date_kind") or "") == "month":
        start, end = coerce_month_window(window.get("start", -1), window.get("end", -1))
        return expand_m_window(start, end, today)
    start, end = coerce_window(window.get("start", -1), window.get("end", -1))
    return expand_d_window(start, end, today)


def plan_repeat_rounds(
    nested: list,
    prefix: str,
    default_dates: list[date],
    output_dates: dict[str, list[date]] | None,
) -> list[tuple[date, list[int]]]:
    """一个日期循环里各导出可以有自己的日期，返回每天要跑的步骤下标。

    ``output_dates`` 的键是循环里的步骤位置 ``{prefix}.{下标}``，没列出的导出用默认日期。
    同一天的导出合在一轮里，只选一次日期。每轮从头重放到当天最后一个要导出的步骤，
    中间的非导出步骤全跑（共用的「查询」不能漏），当天不导的导出跳过。
    当天包含循环里最后一个导出时，尾部步骤照常跑。
    """
    everything = list(range(len(nested)))
    outputs = [
        index for index, step in enumerate(nested)
        if isinstance(step, dict) and step.get("action") in OUTPUT_ACTIONS
    ]
    own = {
        index: list(output_dates[f"{prefix}.{index}"])
        for index in outputs
        if output_dates and f"{prefix}.{index}" in output_dates
    }
    if not own:
        return [(current, everything) for current in default_dates]

    def dates_for(index: int) -> list[date]:
        return own.get(index, default_dates)

    days = sorted({current for index in outputs for current in dates_for(index)})
    rounds: list[tuple[date, list[int]]] = []
    for current in days:
        wanted = {index for index in outputs if current in dates_for(index)}
        last = len(nested) - 1 if outputs[-1] in wanted else max(wanted)
        rounds.append((current, [
            index for index in everything[: last + 1]
            if index not in outputs or index in wanted
        ]))
    return rounds


def day_loop_output_keys(cfg: dict | None, indicator: str = "") -> list[str]:
    """按日跑时会落在日期循环里的导出（原始 key）。只有它们能单独配范围 / 日期。"""
    if not isinstance(cfg, dict) or read_date_profile(cfg)["kind"] != "day":
        return []
    looped = apply_date_profile(cfg, "day")
    return [
        original.key
        for original, current in zip(workflow_outputs(cfg, indicator), workflow_outputs(looped, indicator))
        if "." in current.position
    ]


def _step_at(steps: list, position: str) -> dict | None:
    """按 s4 / s4.6 找到步骤。"""
    current: object = {"steps": steps}
    for part in position[1:].split("."):
        items = current.get("steps") if isinstance(current, dict) else None
        if not isinstance(items, list) or not part.isdigit() or int(part) >= len(items):
            return None
        current = items[int(part)]
    return current if isinstance(current, dict) else None


def _has_window(step: dict | None) -> bool:
    return isinstance(step, dict) and ("start" in step or "end" in step)


def read_output_windows(cfg: dict | None, indicator: str = "") -> dict[str, tuple[int, int]]:
    """指标里给单个导出写的默认范围（导出步骤上的 start/end），只认日期循环里的导出。"""
    looped = set(day_loop_output_keys(cfg, indicator))
    if not looped:
        return {}
    steps = cfg.get("steps") or []
    result: dict[str, tuple[int, int]] = {}
    for spec in workflow_outputs(cfg, indicator):
        step = _step_at(steps, spec.position)
        if spec.key in looped and _has_window(step):
            result[spec.key] = coerce_window(step.get("start", -1), step.get("end", -1))
    return result


def set_output_windows(cfg: dict, windows: dict[str, tuple[int, int] | None]) -> dict:
    """把导出的默认范围写到导出步骤上。None 表示跟随指标，去掉 start/end。"""
    updated = deepcopy(cfg)
    steps = updated.get("steps") or []
    for spec in workflow_outputs(updated, ""):
        if spec.key not in windows:
            continue
        step = _step_at(steps, spec.position)
        if step is None:
            continue
        window = windows[spec.key]
        if window is None:
            step.pop("start", None)
            step.pop("end", None)
        else:
            step["start"], step["end"] = coerce_window(window[0], window[1])
    return updated


def step_output_dates(nested: list, prefix: str, today: date) -> dict[str, list[date]]:
    """循环里自己写了 start/end 的导出，从运行当天展开成日期。键是 ``{prefix}.{下标}``。"""
    result: dict[str, list[date]] = {}
    for index, step in enumerate(nested):
        if isinstance(step, dict) and step.get("action") in OUTPUT_ACTIONS and _has_window(step):
            start, end = coerce_window(step.get("start", -1), step.get("end", -1))
            result[f"{prefix}.{index}"] = expand_d_window(start, end, today)
    return result


def pin_step_dates(step: dict, target: date) -> dict:
    """这一轮的选日期就是这一天。yesterday 不再相对目标日再减一天。"""
    copied = deepcopy(step)
    if copied.get("action") == "pick_date" and is_relative_day(copied.get("value")):
        copied["value"] = target.isoformat()
    return copied
