"""任务执行器：登录一次 → 顺序执行多个指标工作流（共享浏览器，避免反复重开）。

任务 YAML 格式:
    name: "日常采集"
    login: 系统登录
    stop_on_error: true
    indicators:
      - spot_price
      - disclosure
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import re as _re
import sys
from pathlib import Path

import yaml
from loguru import logger

_TOOLS_DIR = Path(__file__).resolve().parent
_WORKFLOWS_DIR = _TOOLS_DIR.parent / "workflows"

if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from spider_gd.core.dates import (
    apply_date_profile,
    day_loop_output_keys,
    expand_d_window,
    expand_m_window,
    expand_w_window,
    is_month_workflow,
    normalize_indicator_dates,
    normalize_indicator_months,
    normalize_indicator_windows,
    normalize_output_dates,
    normalize_target_dates,
    normalize_target_months,
    read_date_profile,
    read_output_windows,
)
from spider_gd.core.workflow_outputs import workflow_outputs
from workflow_runner import WorkflowExecutionError, WorkflowRunner, plan_download_files


def _sanitize_name(name: str) -> str:
    return _re.sub(r"[^\w\-.]", "_", name)


def _coerce_int(value, default: int = 0, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _coerce_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def output_slot_dirs(
    before: dict, after: dict, key_dirs: dict, indicator: str = "",
) -> dict:
    """存储结构按输出 key 配目录，运行时按步骤位置查；日期改写会挤动步骤，按输出顺序对齐。

    按导出另配的日期也用它从 key 换到步骤位置。
    """
    return {
        current.slot: key_dirs[original.key]
        for original, current in zip(
            workflow_outputs(before, indicator), workflow_outputs(after, indicator),
        )
        if original.key in key_dirs
    }


def plan_task_target_dates(
    *,
    backfill_days: int = 1,
    cover_days: int = 0,
    reference_date: str | date | None = None,
) -> list[date]:
    """Expand task window into business target dates.

    默认值 ``向前 1 天 / 向后 0 天`` 对应“只跑昨天”，与现有工作流中
    大量 ``pick_date: yesterday`` 的业务含义保持一致。
    """
    today = _coerce_date(reference_date) or date.today()
    before = _coerce_int(backfill_days, 1, 0, 365)
    after = _coerce_int(cover_days, 0, 0, 365)

    offsets: list[int] = []
    if before > 0:
        offsets.extend(range(-before, 0))
    if after > 0:
        offsets.extend(range(0, after))
    if not offsets:
        offsets.append(0)
    return [today + timedelta(days=offset) for offset in offsets]


_CATCHUP_MAX_DAYS = 62
_CATCHUP_MAX_INDICATORS = 30


def workflow_is_month_span(cfg: dict | None) -> bool:
    """按月指标整段只查询一次。显式 date_kind 优先。"""
    return is_month_workflow(cfg)


def plan_indicator_runs(
    indicators: list[str],
    target_dates: list[date],
    *,
    month_span_names: set[str],
) -> list[tuple[date, str]]:
    """按天指标逐日执行；上月整月指标只锚定最后一天跑一次。"""
    daily = [name for name in indicators if name not in month_span_names]
    monthly = [name for name in indicators if name in month_span_names]
    runs = [(target, name) for target in target_dates for name in daily]
    if monthly and target_dates:
        anchor = target_dates[-1]
        runs.extend((anchor, name) for name in monthly)
    return runs


def plan_workflow_runs(
    indicators: list[str],
    *,
    configs: dict[str, dict | None] | None = None,
    explicit_dates: list[date] | None = None,
    explicit_months: list[date] | None = None,
    indicator_windows: dict[str, tuple[int, int]] | None = None,
    indicator_dates: dict | None = None,
    indicator_months: dict | None = None,
    output_windows: dict | None = None,
    output_dates: dict | None = None,
    backfill_days: int = 1,
    cover_days: int = 0,
    reference_date: date | None = None,
) -> list[dict]:
    """决定每个指标跑哪些天或哪些月。

    某个指标自己的补数日期优先，全体共用的补数日期其次。
    任务里单独保存的窗口再次。按日窗口从 D 展开，按月窗口从 M 展开。
    旧任务没有逐指标窗口时，按日仍用原来的向前/向后天数，按月仍锚定运行日查上月。
    指标 YAML 里的默认窗口留给「直接执行这个指标、没有入参」的情况。

    按日指标里落在日期循环里的导出可以另配：定时用 ``output_windows``，
    补数据用 ``output_dates``（空列表 = 不补），键都是 ``指标::输出key``。
    定时没另配的导出，跟随指标 YAML 里导出步骤上的 start/end；都没写才用指标范围。
    和指标默认不同的导出记在 ``output_dates`` 里，键是输出 key。
    """
    reference = reference_date or date.today()
    windows = indicator_windows or {}
    explicit = normalize_target_dates(explicit_dates or [])
    months = normalize_target_months(explicit_months or [])
    named_dates = normalize_indicator_dates(indicator_dates)
    named_months = normalize_indicator_months(indicator_months)
    own_windows = normalize_indicator_windows(output_windows)
    own_dates = normalize_output_dates(output_dates)
    catchup = bool(named_dates or explicit or own_dates)
    configs = configs or {}
    planned: list[dict] = []
    for name in indicators:
        cfg = configs.get(name)
        profile = read_date_profile(cfg)
        if profile["kind"] == "month":
            if name in named_months:
                chosen = named_months[name]
            elif months:
                chosen = months
            elif name in windows:
                chosen = expand_m_window(windows[name][0], windows[name][1], reference)
            else:
                chosen = None
            if chosen:
                for month_start in chosen:
                    planned.append({
                        "indicator": name,
                        "mode": "month",
                        "dates": [month_start],
                        "query_month": month_start.strftime("%Y-%m"),
                    })
            else:
                anchor = explicit[-1] if explicit else reference
                planned.append({"indicator": name, "mode": "month", "dates": [anchor]})
            continue
        if profile["kind"] == "week":
            if name in named_dates:
                chosen = sorted({day - timedelta(days=day.weekday()) for day in named_dates[name]})
            elif explicit:
                chosen = sorted({day - timedelta(days=day.weekday()) for day in explicit})
            elif name in windows:
                chosen = expand_w_window(windows[name][0], windows[name][1], reference)
            else:
                chosen = expand_w_window(profile["start"], profile["end"], reference)
            for monday in chosen:
                planned.append({"indicator": name, "mode": "week", "dates": [monday]})
            continue
        if name in named_dates:
            dates = named_dates[name]
        elif explicit:
            dates = explicit
        elif own_dates:
            dates = []
        elif name in windows:
            dates = expand_d_window(windows[name][0], windows[name][1], reference)
        else:
            dates = plan_task_target_dates(
                backfill_days=backfill_days,
                cover_days=cover_days,
                reference_date=reference,
            )
        loop_cfg = apply_date_profile(cfg, "day") if isinstance(cfg, dict) else {}
        can_loop = profile["has_repeat"] or any(
            "." in spec.position for spec in workflow_outputs(loop_cfg, name)
        )
        per_output: dict[str, list[date]] = {}
        step_windows = read_output_windows(cfg, name) if can_loop else {}
        for key in day_loop_output_keys(cfg, name) if can_loop else []:
            canonical = f"{name}::{key}"
            if catchup:
                chosen = own_dates.get(canonical, dates)
            elif canonical in own_windows:
                chosen = expand_d_window(own_windows[canonical][0], own_windows[canonical][1], reference)
            elif key in step_windows:
                # 任务里「跟随」= 跟随指标里给这个导出写的范围
                chosen = expand_d_window(step_windows[key][0], step_windows[key][1], reference)
            else:
                chosen = dates
            if chosen != dates:
                per_output[key] = chosen
        if not dates and not any(per_output.values()):
            continue
        if can_loop:
            item = {"indicator": name, "mode": "repeat", "dates": dates}
            if per_output:
                item["output_dates"] = per_output
            planned.append(item)
        else:
            for current in dates:
                planned.append({"indicator": name, "mode": "replay", "dates": [current]})
    return planned


def build_catchup_config(
    *,
    login: str,
    indicators: list[str],
    target_dates=None,
    target_months=None,
    indicator_dates=None,
    indicator_months=None,
    output_dates=None,
) -> dict:
    """一次补数的任务配置。不写定时计划。

    传入逐指标日期或月份时，每个指标都要有自己的时间（或至少一个导出另选了日期）。
    没传时仍接受全体共用的一批日期或月份。
    """
    names: list[str] = []
    for item in indicators or []:
        text = str(item).strip()
        if text and text not in names:
            names.append(text)
    if not names:
        raise ValueError("请选择至少一个指标")
    if len(names) > _CATCHUP_MAX_INDICATORS:
        raise ValueError(f"一次最多选择 {_CATCHUP_MAX_INDICATORS} 个指标")
    named_dates = normalize_indicator_dates(indicator_dates)
    named_months = normalize_indicator_months(indicator_months)
    named_outputs = normalize_output_dates(output_dates)
    dates = normalize_target_dates(target_dates or [])
    months = normalize_target_months(target_months or [])
    config = {
        "name": "补数据",
        "login": str(login or "").strip(),
        "indicators": names,
        "stop_on_error": True,
        "_control_name": "catchup",
    }
    if named_dates or named_months or named_outputs:
        for name in names:
            own_dates = named_dates.get(name) or []
            own_months = named_months.get(name) or []
            output_days = [
                value for key, value in named_outputs.items()
                if key.split("::", 1)[0] == name
            ]
            if not own_dates and not own_months and not any(output_days):
                raise ValueError(f"请为「{name}」选择日期或月份")
            if any(len(value) > _CATCHUP_MAX_DAYS for value in [own_dates, *output_days]):
                raise ValueError(f"「{name}」一次最多补 {_CATCHUP_MAX_DAYS} 天")
            if len(own_months) > 24:
                raise ValueError(f"「{name}」一次最多补 24 个月")
        if named_dates or named_outputs:
            config["indicator_dates"] = {
                key: [item.isoformat() for item in value] for key, value in named_dates.items()
            }
        if named_outputs:
            config["output_dates"] = {
                key: [item.isoformat() for item in value] for key, value in named_outputs.items()
            }
        if named_months:
            config["indicator_months"] = {
                key: [item.strftime("%Y-%m") for item in value] for key, value in named_months.items()
            }
        return config
    if not dates and not months:
        raise ValueError("请选择日期或月份")
    if len(dates) > _CATCHUP_MAX_DAYS:
        raise ValueError(f"一次最多补 {_CATCHUP_MAX_DAYS} 天")
    if len(months) > 24:
        raise ValueError("一次最多补 24 个月")
    if dates:
        config["target_dates"] = [item.isoformat() for item in dates]
    if months:
        config["target_months"] = [item.strftime("%Y-%m") for item in months]
    return config


def _is_relative_date_value(value: object) -> bool:
    if value is None:
        return True
    text = str(value)
    return text in {"today", "yesterday"} or bool(_re.fullmatch(r"-\d+d", text))


def _resolve_relative_date_value(value: object, target: date) -> str:
    text = str(value or "today")
    if text == "today":
        return target.isoformat()
    if text == "yesterday":
        return (target - timedelta(days=1)).isoformat()
    if _re.fullmatch(r"-\d+d", text):
        return (target - timedelta(days=int(text[1:-1]))).isoformat()
    return text


def apply_target_date_to_workflow(cfg: dict, target: date | None) -> dict:
    """Return a copied workflow config pinned to one target date."""
    copied = deepcopy(cfg)
    if target is None:
        return copied

    copied["target_date"] = target.isoformat()
    for step in copied.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        action = step.get("action")
        if action == "pick_date" and _is_relative_date_value(step.get("value")):
            step["value"] = target.isoformat()
        elif action == "fill" and step.get("value") == "{target}":
            step["value"] = f"{target.month}.{target.day:02d}"
        elif action == "fill" and isinstance(step.get("value"), str) and "{target_ym}" in step.get("value"):
            step["value"] = step["value"].replace("{target_ym}", f"{target.year}年{target.month}月")
        elif action == "fill" and _is_relative_date_value(step.get("value")):
            fill_target = date.fromisoformat(_resolve_relative_date_value(step.get("value"), target))
            step["value"] = f"{fill_target.month}.{fill_target.day:02d}"
        elif action == "pick_date_range":
            for key in ("start_value", "end_value"):
                if _is_relative_date_value(step.get(key)):
                    step[key] = _resolve_relative_date_value(step.get(key), target)
    return copied


class TaskRunner:
    def __init__(self, task_config: dict, download_dir: str = "./downloads",
                 headless: bool = False, workflow_loader=None,
                 cert_password: str = "", account: str = "", account_password: str = "",
                 minimized: bool = False, indicator_dirs: dict[str, str] | None = None,
                 output_dirs: dict[str, dict[str, str]] | None = None,
                 station_queue: list[dict] | None = None,
                 exec_id: str = "", slot_store=None):
        self.config = task_config
        self.name = task_config.get("name", "unnamed")
        self.login_name = task_config.get("login", "")
        self.stop_on_error = task_config.get("stop_on_error", True)
        from task_retry import DEFAULT_INDICATOR_RETRY

        self.indicator_retry = _coerce_int(
            task_config.get("indicator_retry", DEFAULT_INDICATOR_RETRY),
            DEFAULT_INDICATOR_RETRY, 0, 10,
        )
        self._exec_id = exec_id or str(task_config.get("_exec_id") or "")
        self._slot_store = slot_store
        self._skip_slots = {str(item) for item in (task_config.get("_skip_slots") or [])}
        only = task_config.get("_only_slots")
        self._only_slots = {str(item) for item in only} if only else None
        self._resume_from = str(task_config.get("_resume_from") or "")
        self._gateway_reset_used = False
        self.indicators = task_config.get("indicators", [])
        self.download_dir = download_dir
        self.headless = headless
        self.minimized = minimized
        self._workflow_loader = workflow_loader  # (id: str) -> dict | None
        self._cert_password = cert_password
        self._account = account
        self._account_password = account_password
        self._station_queue = [dict(item) for item in (station_queue or [])]
        self.backfill_days = _coerce_int(task_config.get("backfill_days", 1), 1, 0, 365)
        self.cover_days = _coerce_int(task_config.get("cover_days", 0), 0, 0, 365)
        self.reference_date = _coerce_date(task_config.get("reference_date"))
        self.target_dates = normalize_target_dates(task_config.get("target_dates") or [])
        self.target_months = normalize_target_months(task_config.get("target_months") or [])
        self.indicator_dates = normalize_indicator_dates(task_config.get("indicator_dates"))
        self.indicator_months = normalize_indicator_months(task_config.get("indicator_months"))
        self.indicator_windows = normalize_indicator_windows(task_config.get("indicator_windows"))
        self.output_windows = normalize_indicator_windows(task_config.get("output_windows"))
        self.output_dates = normalize_output_dates(task_config.get("output_dates"))
        self.indicator_folders = self._normalize_indicator_folders(task_config.get("path_folders"))
        self.indicator_dirs = {
            str(name): str(path)
            for name, path in (indicator_dirs or {}).items()
            if str(name).strip() and str(path).strip()
        }
        self.output_dirs = {
            str(indicator): {
                str(key): str(path)
                for key, path in (mapping or {}).items()
                if str(path).strip()
            }
            for indicator, mapping in (output_dirs or {}).items()
        }

        self._on_event: callable = lambda e: None
        self._wait_continue: callable = None
        self._stopped = False
        self._failed = False
        self._had_error = False
        self._failure_reason = ""
        self._task_error_emitted = False
        self._storage_state_path = self._resolve_storage_state_path(Path(download_dir))
        self._login_done = False
        self._shared_session = None
        self._protected_pages: set = set()
        self._summary = {"success": 0, "failed": 0, "skipped": 0}

        # 共享浏览器状态
        self._browser = None
        self._context = None
        self._chrome_proc = None
        self._playwright = None

    @staticmethod
    def _normalize_indicator_folders(raw) -> dict[str, str]:
        """path_folders 树 → {指标: "披露文件/交易到时"}。

        兼容旧的一层结构 ``[{"name": "阳江", "indicators": [...]}]``；
        新结构带 ``children``，可嵌套任意层。
        """
        from spider_gd.core.output_paths import sanitize_segment

        mapping: dict[str, str] = {}

        def walk(items, prefix: list[str]) -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_name = str(item.get("name", "")).strip()
                parts = [
                    sanitize_segment(part)
                    for part in raw_name.split("/")
                    if part.strip()
                ]
                parts = [part for part in parts if part]
                if not parts:
                    continue
                path = [*prefix, *parts]
                for indicator in item.get("indicators", []) or []:
                    if isinstance(indicator, str):
                        mapping[indicator] = "/".join(path)
                walk(item.get("children", []) or [], path)

        walk(raw, [])
        return mapping

    def _indicator_output_dir(self, indicator_name: str) -> str:
        """指标级目录；有显式 indicator_dirs 时优先用它。"""
        if self.indicator_dirs:
            target = Path(self.indicator_dirs.get(indicator_name, self.download_dir))
            target.mkdir(parents=True, exist_ok=True)
            return str(target)

        folder_path = self.indicator_folders.get(indicator_name, "").strip()
        if not folder_path:
            return self.download_dir
        from spider_gd.core.output_paths import sanitize_segment

        parts = [sanitize_segment(part) for part in folder_path.split("/")]
        parts = [part for part in parts if part]
        if not parts:
            return self.download_dir
        target = Path(self.download_dir).joinpath(*parts)
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    @staticmethod
    def _resolve_storage_state_path(download_dir: Path) -> Path:
        """把会话状态放在下载根目录，避免按日目录切换后找不到。"""
        if (
            _re.fullmatch(r"\d{4}-\d{2}-\d{2}", download_dir.name)
            and _re.fullmatch(r"\d{4}-\d{2}", download_dir.parent.name)
        ):
            return download_dir.parent.parent / "storage_state.json"
        return download_dir / "storage_state.json"

    def _load_wf_cfg(self, wf_id: str) -> dict | None:
        if self._workflow_loader:
            cfg = self._workflow_loader(wf_id)
            if cfg:
                return cfg
        path = _WORKFLOWS_DIR / f"{_sanitize_name(wf_id)}.yml"
        if not path.exists():
            path = _WORKFLOWS_DIR / f"{wf_id}.yml"
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return None

    async def run(self, on_event=None, wait_continue=None, *, session=None):
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        session = session or get_shared_browser()
        async with session.execution(self.config.get("_control_name", self.name), on_event) as emit:
            self._shared_session = session
            self._on_event = emit
            self._wait_continue = session.wait_continue
            self._browser, self._context = session._browser, session.page.context
            self._shared_pages = [session.page]
            self._protected_pages = set(self._context.pages)
            self._current_page_idx = 0
            session._active_runner = self
            from task_retry import copy_resume_slots, slot_keys_for_item

            if self._slot_store is not None and self._exec_id and self._resume_from:
                copy_resume_slots(self._slot_store, self._resume_from, self._exec_id)
                self._skip_slots.update(self._slot_store.succeeded_slots(self._exec_id))
            configs = {name: self._load_wf_cfg(name) for name in self.indicators}
            runs = plan_workflow_runs(
                list(self.indicators),
                configs=configs,
                explicit_dates=self.target_dates or None,
                explicit_months=self.target_months or None,
                indicator_dates=self.indicator_dates or None,
                indicator_months=self.indicator_months or None,
                indicator_windows=self.indicator_windows,
                output_windows=self.output_windows,
                output_dates=self.output_dates or None,
                backfill_days=self.backfill_days,
                cover_days=self.cover_days,
                reference_date=self.reference_date,
            )
            total_steps = (1 if self.login_name else 0) + len(runs)
            step_idx = 0

            def _emit(event, **kwargs):
                emit({"event": event, **kwargs})

            if self._exec_id:
                _emit("run_context", exec_id=self._exec_id)
            plan_rows = []
            for item in runs:
                keys = slot_keys_for_item(item)
                plan_rows.append({
                    "indicator": item.get("indicator"),
                    "slot": keys[0] if keys else item.get("indicator"),
                    "slots": keys,
                    "target": self._item_target_text(item),
                    "status": "pending",
                })
            _emit("indicator_plan", items=plan_rows)

            stations = self._station_queue or [None]
            try:
                for station_index, station in enumerate(stations):
                    if self._stopped or self._failed:
                        break
                    step_idx = 0
                    if isinstance(station, dict):
                        self._use_station(station, station_index, len(stations))
                        if self._failed:
                            _emit("task_error", error=self._failure_reason or "场站准备失败")
                            self._task_error_emitted = True
                            break
                        if len(stations) > 1:
                            await self._return_to_login_page()
                            self._login_done = False
                    if self.login_name:
                        step_idx += 1
                        auth = await session.check_auth()
                        if auth["state"] == "authenticated":
                            self._login_done = True
                        elif auth["state"] == "unauthenticated":
                            # 任务配置了登录流程才自动登录。全局会话保持 reuse_only。
                            self._login_done = False
                        else:
                            self._login_done = await session.require_auth(allow_auto=False)
                        if not self._login_done:
                            await self._run_login_shared(step_idx, total_steps, _emit)
                            if not self._failed:
                                self._login_done = await session.require_auth(allow_auto=False)
                        else:
                            _emit("task_step", step=step_idx, total=total_steps, phase="login",
                                  name=self.login_name, status="done", message="复用当前业务登录态")
                    self._summary = {"success": 0, "failed": 0, "skipped": 0}
                    for index, item in enumerate(runs):
                        if self._stopped or session._cancel_requested or self._failed:
                            self._emit_skipped_download_plans(runs[index:], _emit)
                            self._mark_plan_skipped(runs[index:], _emit, "任务已停止")
                            break
                        keys = slot_keys_for_item(item)
                        skip_reason = self._skip_reason(keys)
                        if skip_reason:
                            self._summary["skipped"] += 1
                            if skip_reason == "已成功":
                                self._mark_slots(keys, "success")
                            _emit(
                                "indicator_status",
                                indicator=item.get("indicator"),
                                slot=keys[0] if keys else "",
                                slots=keys,
                                status="skipped",
                                retry=0,
                                message=skip_reason,
                            )
                            _emit(
                                "task_step", step=index + 1, total=len(runs), phase="indicator",
                                name=item.get("indicator"), status="skip", message=skip_reason,
                                target_date=self._item_target_text(item),
                            )
                            continue
                        mode = item["mode"]
                        dates = item["dates"]
                        if mode == "replay" and dates:
                            _emit(
                                "task_window",
                                date=dates[0].isoformat(),
                                index=runs.index(item) + 1,
                                total=len(runs),
                            )
                        step_idx += 1
                        outcome = await self._run_with_retries(item, step_idx, total_steps, _emit, keys)
                        if outcome == "fatal":
                            break
                    if self._stopped or session._cancel_requested or self._failed:
                        if not self._task_error_emitted:
                            _emit("task_error", error=self._failure_reason or "用户中止")
                            self._task_error_emitted = True
                        break
                    message = (
                        f"成功 {self._summary['success']} 项，失败 {self._summary['failed']} 项，"
                        f"跳过 {self._summary['skipped']} 项"
                    )
                    _emit("task_summary", **self._summary, message=message)
                    if station_index == len(stations) - 1:
                        _emit("task_done", name=self.name, **self._summary, message=message)
                    else:
                        station_name = station.get("name", "") if isinstance(station, dict) else ""
                        _emit(
                            "station_done",
                            name=station_name,
                            index=station_index + 1,
                            total=len(stations),
                            message=f"场站 {station_name} 的指标已完成，准备切换下一个场站",
                        )
                        self._login_done = False
            except Exception as exc:
                self._record_failure(str(exc))
                _emit("task_error", error=str(exc))
            finally:
                session._active_runner = None

    def _use_station(self, station: dict, index: int, total: int) -> None:
        """切换到指定场站的证书密码，并连接它的 WiFi。

        Args:
            station: 含 ``name``、``cert_password``、``wifi_ssid``、``wifi_password``。
            index: 从 0 开始的序号。
            total: 本次要执行的场站数。

        Returns:
            None: 结果通过事件送出。WiFi 连接失败时记为任务失败。

        Examples:
            >>> runner = TaskRunner({"name": "t"}, station_queue=[{"name": "阳江", "cert_password": "pin"}])
            >>> runner._use_station(runner._station_queue[0], 0, 1)
            >>> runner._cert_password
            'pin'
        """
        name = str(station.get("name") or "").strip()
        cert_password = str(station.get("cert_password") or "").strip()
        if cert_password:
            self._cert_password = cert_password
        self._on_event({
            "event": "station_start",
            "name": name,
            "index": index + 1,
            "total": total,
            "message": f"第 {index + 1}/{total} 个场站：{name}",
        })
        ssid = str(station.get("wifi_ssid") or "").strip()
        if not ssid:
            return
        from spider_gd.infrastructure.network.wifi_connector import WifiConnectOptions, WifiConnector

        result = WifiConnector().prepare(
            WifiConnectOptions(ssid=ssid, password=str(station.get("wifi_password") or ""))
        )
        self._on_event({
            "event": "station_wifi",
            "name": name,
            "ok": result.ok,
            "message": result.message,
        })
        if not result.ok:
            self._record_failure(result.message or f"连接 WiFi 失败：{ssid}")

    async def _return_to_login_page(self) -> None:
        """打开任务入口页，让下一场站重新走登录。

        Returns:
            None: 页面跳转失败时只记一条说明，不中断任务。

        Examples:
            >>> TaskRunner({"name": "t"})._shared_session is None
            True
        """
        session = self._shared_session
        if session is None or getattr(session, "page", None) is None:
            return
        url = str(self.config.get("start_page") or "https://pm.gd.csg.cn/portal/#/home")
        try:
            await session.page.goto(url, wait_until="domcontentloaded")
            self._on_event({
                "event": "station_switch",
                "message": "已回到登录页，准备登录下一个场站",
            })
        except Exception as exc:
            self._on_event({
                "event": "station_switch",
                "message": f"打开登录页失败：{exc}",
            })

    async def _cleanup_between_indicators(self):
        if self._shared_session is None:
            return
        root = self._shared_session.page
        for page in self._shared_pages:
            if page is not root and page not in self._protected_pages and not page.is_closed():
                await page.close()
        self._shared_pages = [root]
        self._current_page_idx = 0

    async def _execute_bound_workflow(self, runner):
        runner._shared_session = self._shared_session
        runner._protected_pages = self._protected_pages
        if self._shared_session is not None:
            self._shared_session._active_runner = runner
            self._context.on("page", runner._on_new_page)
        try:
            await runner._execute_steps(self._on_event, self._wait_continue, shared=True)
        finally:
            if self._shared_session is not None:
                self._context.remove_listener("page", runner._on_new_page)
                self._shared_session._active_runner = self

    async def _run_login_shared(self, step_idx: int, total: int, _emit):
        """共享浏览器模式下的登录：复用已有浏览器/context。"""
        login_cfg = self._load_wf_cfg(self.login_name)
        if not login_cfg:
            self._record_failure(f"登录工作流不存在: {self.login_name}")
            _emit("task_step", step=step_idx, total=total,
                  phase="login", name=self.login_name, status="error",
                  message=f"登录工作流不存在: {self.login_name}")
            _emit("task_error", error=f"登录工作流不存在: {self.login_name}")
            self._task_error_emitted = True
            return

        # 替换占位符（${CERT_PASSWORD} 等）
        import json as _json
        _raw = _json.dumps(login_cfg, ensure_ascii=False)
        for placeholder, val in [("${CERT_PASSWORD}", self._cert_password),
                                  ("${ACCOUNT}", self._account),
                                  ("${ACCOUNT_PASSWORD}", self._account_password)]:
            if val:
                _raw = _raw.replace(placeholder, val)
        login_cfg = _json.loads(_raw)

        # 执行登录工作流（共享浏览器）—— 每次都重新登录，不依赖 storage_state
        _emit("task_step", step=step_idx, total=total,
              phase="login", name=self.login_name, status="running",
              message="正在登录...")

        runner = WorkflowRunner(
            login_cfg,
            download_dir=self.download_dir,
            headless=self.headless,
            minimized=self.minimized,
        )
        runner.browser = self._browser
        runner.context = self._context
        runner.chrome_proc = None
        runner.pages = self._shared_pages
        runner._current_page_idx = self._current_page_idx

        try:
            await self._execute_bound_workflow(runner)
            # 同步 WorkflowRunner 修改过的 pages
            self._shared_pages = runner.pages
            self._current_page_idx = runner._current_page_idx
            if runner.failed:
                reason = runner.failure_reason or "登录工作流失败"
                self._record_failure(reason)
                _emit("task_step", step=step_idx, total=total,
                      phase="login", name=self.login_name, status="error",
                      message=reason)
                _emit("task_error", error=reason)
                self._task_error_emitted = True
                return
            self._login_done = True
            if self._shared_session is None:
                await self._save_storage_state()
            _emit("task_step", step=step_idx, total=total,
                  phase="login", name=self.login_name, status="done")
        except Exception as exc:
            self._record_failure(str(exc))
            _emit("task_step", step=step_idx, total=total,
                  phase="login", name=self.login_name, status="error",
                  message=str(exc))
            _emit("task_error", error=str(exc))
            self._task_error_emitted = True

    async def _save_storage_state(self) -> None:
        try:
            self._storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=str(self._storage_state_path))
            logger.info("  已保存登录状态: {}", self._storage_state_path)
        except Exception as exc:
            logger.debug("  保存登录状态失败: {}", exc)

    def _prepare_indicator(
        self, indicator_name: str,
        target_date: date | None,
        repeat_dates: list[date] | None,
        query_month: str | None,
        output_dates: dict[str, list[date]] | None,
    ) -> dict | None:
        """整理指标这次的配置、目录和预期下载文件。

        Args:
            indicator_name: 指标 id。
            target_date: 非循环执行锚定的日期。
            repeat_dates: 按天循环的日期。
            query_month: 按月指标的月份。
            output_dates: 单个导出另配的日期。

        Returns:
            dict | None: 跑指标和列预期文件要用的参数。指标不存在时为 None。
        """
        cfg = self._load_wf_cfg(indicator_name)
        if not cfg:
            return None
        own_dates = {key: list(value) for key, value in (output_dates or {}).items()}
        every_day = sorted({*(repeat_dates or []), *(day for value in own_dates.values() for day in value)})
        looping = every_day or None
        source_cfg = cfg
        profile = read_date_profile(cfg)
        if profile["kind"] == "week" and not any(
            isinstance(step, dict) and step.get("action") == "select_calendar_week"
            for step in cfg.get("steps", [])
        ):
            cfg = apply_date_profile(cfg, "week", profile["start"], profile["end"])
        if looping and not read_date_profile(cfg)["has_repeat"]:
            cfg = apply_date_profile(cfg, "day")
        output_dir_map = output_slot_dirs(
            source_cfg, cfg, self.output_dirs.get(indicator_name, {}), indicator_name,
        )
        output_repeat_dates = output_slot_dirs(source_cfg, cfg, own_dates, indicator_name)
        if query_month:
            target_text = query_month
            running_message = f"{query_month} 整月"
        elif looping:
            target_text = f"{looping[0].isoformat()} 至 {looping[-1].isoformat()}"
            running_message = f"按日期循环，共 {len(looping)} 天"
        else:
            target_text = target_date.isoformat() if target_date else ""
            running_message = "执行中..."
        if not looping:
            cfg = apply_target_date_to_workflow(cfg, target_date)
        download_dir = self._indicator_output_dir(indicator_name)
        runner_target = looping[0] if looping else target_date
        runner_repeat = list(repeat_dates or []) if output_repeat_dates else looping
        cfg = {**cfg, "_control_name": indicator_name, "steps": list(cfg.get("steps", []))}
        files = plan_download_files(
            cfg,
            indicator=indicator_name,
            download_dir=Path(download_dir),
            output_dirs=output_dir_map,
            repeat_dates=runner_repeat,
            output_repeat_dates=output_repeat_dates,
            target_date=runner_target if isinstance(runner_target, date) else None,
            query_month=query_month,
        )
        return {
            "cfg": cfg,
            "download_dir": download_dir,
            "target_date": runner_target,
            "repeat_dates": runner_repeat,
            "query_month": query_month,
            "output_dirs": output_dir_map,
            "output_repeat_dates": output_repeat_dates,
            "target_text": target_text,
            "running_message": running_message,
            "needs_login": cfg.get("needs_login", True),
            "files": files,
        }

    def _emit_skipped_download_plans(self, items: list[dict], _emit) -> None:
        """把还没开始跑的指标预期文件发出来。

        Args:
            items: ``plan_workflow_runs`` 剩下的条目。
            _emit: 任务事件回调。
        """
        for item in items:
            dates = list(item.get("dates") or [])
            mode = item.get("mode")
            repeat_dates = dates if mode == "repeat" else None
            anchor = dates[-1] if mode == "month" and dates else (dates[0] if dates else None)
            prepared = self._prepare_indicator(
                str(item.get("indicator") or ""),
                anchor,
                repeat_dates,
                item.get("query_month"),
                item.get("output_dates"),
            )
            if not prepared:
                continue
            for planned in prepared["files"]:
                _emit("file_expected", **planned)

    @staticmethod
    def _item_target_text(item: dict) -> str:
        """给进度列表用的日期说明。

        Args:
            item (dict): 计划条目。

        Returns:
            str: 单日、日期范围或月份。没有日期时为空。

        Examples:
            >>> TaskRunner._item_target_text({"mode": "month", "query_month": "2026-10", "dates": []})
            '2026-10'
        """
        dates = list(item.get("dates") or [])
        if item.get("query_month"):
            return str(item["query_month"])
        if not dates:
            return ""
        if len(dates) == 1:
            return dates[0].isoformat()
        return f"{dates[0].isoformat()} 至 {dates[-1].isoformat()}"

    def _skip_reason(self, keys: list[str]) -> str:
        """判断这条指标要不要跳过。

        Args:
            keys (list[str]): 这条计划覆盖的「指标 × 日期」键。

        Returns:
            str: 跳过原因。不跳过时为空字符串。

        Examples:
            >>> runner = TaskRunner({"name": "t", "_skip_slots": ["a\\t2026-11-01"]})
            >>> runner._skip_reason(["a\\t2026-11-01"])
            '已成功'
        """
        if not keys:
            return ""
        if self._only_slots is not None and not any(key in self._only_slots for key in keys):
            return "不在本次续跑范围"
        if all(key in self._skip_slots for key in keys):
            return "已成功"
        return ""

    def _mark_slots(self, keys: list[str], status: str) -> None:
        """把一组日期键写成同一种结果。

        Args:
            keys (list[str]): ``指标\\t日期``。
            status (str): ``success`` / ``failed`` / ``skipped``。

        Returns:
            None: 没有执行 id 或状态库时只更新内存里的成功集合。

        Examples:
            >>> runner = TaskRunner({"name": "t"})
            >>> runner._mark_slots(["a\\t2026-11-01"], "success")
            >>> "a\\t2026-11-01" in runner._skip_slots
            True
        """
        if status == "success":
            self._skip_slots.update(keys)
        if not self._exec_id or self._slot_store is None:
            return
        for key in keys:
            indicator = key.split("\t", 1)[0]
            try:
                self._slot_store.mark_indicator_slot(self._exec_id, indicator, key, status)
            except Exception as exc:
                logger.warning("记录指标结果失败: {}", exc)

    def _mark_plan_skipped(self, items: list[dict], _emit, message: str) -> None:
        """把还没跑的计划条目标成跳过。

        Args:
            items (list[dict]): 剩下的计划条目。
            _emit: 任务事件回调。
            message (str): 跳过原因。

        Returns:
            None: 只发事件，不改数据库里的成功记录。

        Examples:
            >>> TaskRunner({"name": "t"})._summary["skipped"]
            0
        """
        from task_retry import slot_keys_for_item

        for item in items:
            keys = slot_keys_for_item(item)
            self._summary["skipped"] += 1
            _emit(
                "indicator_status",
                indicator=item.get("indicator"),
                slot=keys[0] if keys else "",
                slots=keys,
                status="skipped",
                retry=0,
                message=message,
            )

    async def _run_with_retries(self, item: dict, step_idx: int, total: int, _emit, keys: list[str]) -> str:
        """按失败类型重试单个指标。

        可重试错误会先关掉多余标签、确认登录，再强制打开工作台。
        HTTP 400 整次任务只允许这样复位一次。第二次 400 停止整个任务。

        Args:
            item (dict): 计划条目。
            step_idx (int): 展示用步骤号。
            total (int): 步骤总数。
            _emit: 任务事件回调。
            keys (list[str]): 这条计划的日期键。

        Returns:
            str: ``ok``、``failed`` 或 ``fatal``。

        Examples:
            >>> TaskRunner({"name": "t", "indicator_retry": 3}).indicator_retry
            3
        """
        from task_retry import FailureKind, classify_failure

        indicator = str(item.get("indicator") or "")
        attempt = 0
        force_reset = False
        target_text = self._item_target_text(item)
        while True:
            if force_reset:
                try:
                    await self._reset_workbench(_emit)
                except Exception as exc:
                    reason = f"回到工作台失败：{exc}"
                    self._summary["failed"] += 1
                    self._mark_slots(keys, "failed")
                    self._record_failure(reason, stop=True)
                    _emit(
                        "indicator_status", indicator=indicator, slot=keys[0] if keys else "",
                        slots=keys, status="failed", retry=attempt, message=reason,
                    )
                    _emit("task_error", error=reason)
                    self._task_error_emitted = True
                    return "fatal"
                if self._failed:
                    self._summary["failed"] += 1
                    return "fatal"
                force_reset = False
            _emit(
                "indicator_status", indicator=indicator, slot=keys[0] if keys else "",
                slots=keys, status="running", retry=attempt,
                message="执行中..." if attempt == 0 else f"第 {attempt} 次重试",
                target=target_text,
            )
            dates = list(item.get("dates") or [])
            mode = item.get("mode")
            repeat_dates = dates if mode == "repeat" else None
            anchor = dates[-1] if mode == "month" and dates else (dates[0] if dates else None)
            failed, reason = await self._run_indicator_shared(
                indicator, step_idx, total, _emit, anchor, repeat_dates,
                query_month=item.get("query_month"),
                output_dates=item.get("output_dates"),
            )
            if not failed:
                self._summary["success"] += 1
                self._mark_slots(keys, "success")
                _emit(
                    "indicator_status", indicator=indicator, slot=keys[0] if keys else "",
                    slots=keys, status="success", retry=attempt, message="成功", target=target_text,
                )
                return "ok"
            kind = classify_failure(reason)
            if kind is FailureKind.GATEWAY_400:
                if self._gateway_reset_used:
                    message = "同一任务再次出现 HTTP 400（瑞数拦截），已停止以免被封禁。请稍后再运行。"
                    self._summary["failed"] += 1
                    self._mark_slots(keys, "failed")
                    self._record_failure(message, stop=True)
                    _emit(
                        "indicator_status", indicator=indicator, slot=keys[0] if keys else "",
                        slots=keys, status="failed", retry=attempt, message=message,
                    )
                    _emit("task_step", step=step_idx, total=total, phase="indicator",
                          name=indicator, status="error", message=message, target_date=target_text)
                    _emit("task_error", error=message)
                    self._task_error_emitted = True
                    return "fatal"
                self._gateway_reset_used = True
                force_reset = True
                _emit(
                    "indicator_status", indicator=indicator, slot=keys[0] if keys else "",
                    slots=keys, status="running", retry=attempt,
                    message="出现 HTTP 400，回到工作台后只再试一次",
                )
                continue
            if kind is FailureKind.FATAL:
                self._summary["failed"] += 1
                self._mark_slots(keys, "failed")
                self._record_failure(reason, stop=True)
                _emit(
                    "indicator_status", indicator=indicator, slot=keys[0] if keys else "",
                    slots=keys, status="failed", retry=attempt, message=reason,
                )
                _emit("task_error", error=f"{indicator} 失败: {reason}")
                self._task_error_emitted = True
                return "fatal"
            if kind is FailureKind.RETRYABLE and attempt < self.indicator_retry:
                attempt += 1
                force_reset = True
                _emit(
                    "indicator_status", indicator=indicator, slot=keys[0] if keys else "",
                    slots=keys, status="running", retry=attempt,
                    message=f"第 {attempt} 次重试：{reason}",
                )
                continue
            self._summary["failed"] += 1
            self._mark_slots(keys, "failed")
            self._record_failure(reason, stop=False)
            _emit(
                "indicator_status", indicator=indicator, slot=keys[0] if keys else "",
                slots=keys, status="failed", retry=attempt, message=reason,
            )
            return "failed"

    async def _reset_workbench(self, _emit) -> None:
        """关掉多余标签，必要时重新登录，再强制打开工作台。

        重试时忽略指标 YAML 里的 ``skip_if_loaded``：必须真的 ``goto`` 一次，
        让被瑞数打过标的文档换成仍保持登录的工作台。

        Args:
            _emit: 任务事件回调。

        Returns:
            None: 回到工作台后返回。登录失败时由登录流程把任务标成失败。

        Raises:
            WorkflowExecutionError: 打开工作台或等待标记超时。

        Examples:
            >>> TaskRunner({"name": "t"})._shared_session is None
            True
        """
        import asyncio
        import time as _time

        from task_retry import DEFAULT_WORKBENCH_URL

        await self._cleanup_between_indicators()
        session = self._shared_session
        if session is not None:
            auth = await session.check_auth()
            if auth.get("state") != "authenticated":
                if self.login_name:
                    self._login_done = False
                    await self._run_login_shared(0, 0, _emit)
                    if self._failed:
                        return
                else:
                    raise WorkflowExecutionError("登录已失效，且任务没有配置登录流程")
        page = session.page if session is not None else None
        if page is None:
            raise WorkflowExecutionError("没有可复位的页面")
        url = str(self.config.get("workbench_url") or DEFAULT_WORKBENCH_URL)
        _emit("page_recovery", message=f"正在回到工作台：{url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        deadline = _time.monotonic() + 8
        while _time.monotonic() < deadline:
            current = ""
            try:
                current = page.url or ""
            except Exception:
                current = ""
            if "powerEnterprise" in current:
                return
            try:
                found = await page.evaluate(
                    "() => !!(document.body && document.body.innerText && document.body.innerText.includes('工作台'))"
                )
            except Exception:
                found = False
            if found:
                return
            await asyncio.sleep(0.25)
        raise WorkflowExecutionError("复位后未看到工作台")

    async def _run_indicator_shared(
        self, indicator_name: str, step_idx: int, total: int, _emit,
        target_date: date | None = None, repeat_dates: list[date] | None = None,
        query_month: str | None = None, output_dates: dict[str, list[date]] | None = None,
    ) -> tuple[bool, str]:
        """共享浏览器模式下执行单个指标。有循环日期时整份流程只走一遍，日期在循环里换。

        这里不决定要不要停掉整个任务，只返回这次成没成。调用方按失败原因重试或继续。

        Args:
            indicator_name (str): 指标 id。
            step_idx (int): 展示用步骤号。
            total (int): 步骤总数。
            _emit: 任务事件回调。
            target_date (date | None): 锚定日期。
            repeat_dates (list[date] | None): 按天循环的日期。
            query_month (str | None): 按月指标的月份。
            output_dates (dict[str, list[date]] | None): 单个导出另配的日期。

        Returns:
            tuple[bool, str]: ``(是否失败, 原因)``。成功时原因为空。

        Examples:
            >>> TaskRunner({"name": "t"}).indicator_retry
            3
        """
        prepared = self._prepare_indicator(
            indicator_name, target_date, repeat_dates, query_month, output_dates,
        )
        if not prepared:
            reason = f"指标不存在: {indicator_name}"
            _emit("task_step", step=step_idx, total=total,
                  phase="indicator", name=indicator_name, status="error",
                  message=reason)
            return True, reason

        _emit("task_step", step=step_idx, total=total,
              phase="indicator", name=indicator_name, status="running",
              message=prepared["running_message"], target_date=prepared["target_text"])

        if self._shared_session is not None and prepared["needs_login"]:
            # 指标未配置登录流程时，未登录只暂停，不转去自动登录。
            self._login_done = await self._shared_session.require_auth(allow_auto=False)

        runner = WorkflowRunner(
            prepared["cfg"],
            download_dir=prepared["download_dir"],
            headless=self.headless,
            minimized=self.minimized,
            target_date=prepared["target_date"],
            repeat_dates=prepared["repeat_dates"],
            query_month=prepared["query_month"],
            output_dirs=prepared["output_dirs"],
            output_repeat_dates=prepared["output_repeat_dates"],
        )
        # 注入共享浏览器状态
        runner.browser = self._browser
        runner.context = self._context
        runner.chrome_proc = None
        runner.pages = self._shared_pages
        runner._current_page_idx = self._current_page_idx

        try:
            await self._execute_bound_workflow(runner)
            # 同步修改过的 pages
            self._shared_pages = runner.pages
            self._current_page_idx = runner._current_page_idx
            if runner.failed:
                reason = runner.failure_reason or f"{indicator_name} 工作流失败"
                _emit("task_step", step=step_idx, total=total,
                      phase="indicator", name=indicator_name, status="error",
                      message=reason, target_date=prepared["target_text"])
                return True, reason
            _emit("task_step", step=step_idx, total=total,
                  phase="indicator", name=indicator_name, status="done", target_date=prepared["target_text"])
            return False, ""
        except Exception as exc:
            _emit("task_step", step=step_idx, total=total,
                  phase="indicator", name=indicator_name, status="error",
                  message=str(exc), target_date=prepared["target_text"])
            return True, str(exc)

    def _on_new_page(self, new_page) -> None:
        logger.info("  检测到新标签页 #{}", len(self._shared_pages))
        self._shared_pages.append(new_page)
        self._current_page_idx = len(self._shared_pages) - 1

    def _record_failure(self, reason: str, *, stop: bool = True) -> None:
        self._had_error = True
        if not self._failure_reason:
            self._failure_reason = reason
        if stop:
            self._failed = True
            self._stopped = True
