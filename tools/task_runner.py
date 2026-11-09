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
from workflow_runner import WorkflowRunner, plan_download_files


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


def match_task_storage(indicators: list[str], tasks: list[dict] | None) -> dict | None:
    """选出最适合这批补数指标的定时任务，用来沿用它的存储路径。

    只考虑指标有交集的任务。覆盖的指标越多越优先；一样多时已启用的优先；
    仍相同则用最近创建的。不会改任务本身。

    Args:
        indicators (list[str]): 本次补数要跑的指标名。
        tasks (list[dict] | None): 已保存的任务配置。用到 indicators、enabled、created_at。

    Returns:
        dict | None: 命中的任务配置；没有交集时为 None。

    Examples:
        >>> tasks = [
        ...     {"name": "旧", "indicators": ["日前"], "enabled": True, "created_at": 1},
        ...     {"name": "新", "indicators": ["日前", "实时"], "enabled": True, "created_at": 2},
        ... ]
        >>> match_task_storage(["日前"], tasks)["name"]
        '新'
        >>> match_task_storage(["其他"], tasks) is None
        True
    """
    wanted = [str(item).strip() for item in indicators or [] if str(item).strip()]
    if not wanted or not tasks:
        return None
    best: dict | None = None
    best_key: tuple[int, int, float] | None = None
    for task in tasks:
        if not isinstance(task, dict):
            continue
        raw = task.get("indicators") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        owned = {str(item).strip() for item in raw if str(item).strip()}
        covered = sum(1 for name in wanted if name in owned)
        if covered <= 0:
            continue
        enabled = 1 if bool(task.get("enabled", True)) else 0
        try:
            created = float(task.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0
        key = (covered, enabled, created)
        if best_key is None or key > best_key:
            best_key = key
            best = task
    return best


def apply_catchup_storage(
    config: dict,
    tasks: list[dict] | None = None,
    *,
    path_template: str | None = None,
    path_folders: list | None = None,
    path_structure: str | None = None,
    download_dir: str | None = None,
    storage_overridden: bool = False,
) -> dict:
    """把这次补数的落盘路径写进配置。

    请求明确带了存储（``storage_overridden``，或任一 path_* 不是 None）时，用请求里的值。
    空的存储结构表示文件直接放在下载根目录，不再回退到任务。
    请求没带时，沿用 :func:`match_task_storage` 选中的任务：存储结构、旧模板、文件夹，
    以及任务单独指定的下载目录。不修改入参，也不写回定时任务。

    Args:
        config (dict): :func:`build_catchup_config` 的结果，至少含 indicators。
        tasks (list[dict] | None): 已保存任务，用来取默认路径。
        path_template (str | None): 旧版路径模板。None 表示请求没带。
        path_folders (list | None): 旧版文件夹映射。None 表示请求没带。
        path_structure (str | None): 存储结构文本。None 表示请求没带。
        download_dir (str | None): 本次下载根目录。空表示不单独指定。
        storage_overridden (bool): 为 True 时，即使路径字段为空也不回退到任务。

    Returns:
        dict: 带上 path_structure / path_template / path_folders，以及可能的下载目录。

    Examples:
        >>> cfg = {"indicators": ["日前"]}
        >>> tasks = [{
        ...     "indicators": ["日前"], "enabled": True, "created_at": 1,
        ...     "path_structure": "- [日前]", "path_template": "", "path_folders": [],
        ...     "download_dir": "/data/task", "download_dir_custom": True,
        ... }]
        >>> apply_catchup_storage(cfg, tasks)["path_structure"]
        '- [日前]'
        >>> apply_catchup_storage(cfg, tasks)["download_dir"]
        '/data/task'
        >>> apply_catchup_storage(
        ...     cfg, tasks, path_structure="", storage_overridden=True,
        ... )["path_structure"]
        ''
    """
    chosen = dict(config)
    explicit_dir = str(download_dir or "").strip()
    provided = (
        bool(storage_overridden)
        or path_structure is not None
        or path_template is not None
        or path_folders is not None
    )
    if provided:
        chosen["path_structure"] = "" if path_structure is None else str(path_structure)
        chosen["path_template"] = "" if path_template is None else str(path_template)
        chosen["path_folders"] = list(path_folders or [])
        chosen["download_dir"] = explicit_dir
        chosen["download_dir_custom"] = bool(explicit_dir)
        return chosen

    matched = match_task_storage(list(chosen.get("indicators") or []), tasks)
    if matched is None:
        return chosen
    chosen["path_structure"] = str(matched.get("path_structure") or "")
    chosen["path_template"] = str(matched.get("path_template") or "")
    folders = matched.get("path_folders") or []
    chosen["path_folders"] = list(folders) if isinstance(folders, list) else []
    task_dir = str(matched.get("download_dir") or "").strip()
    if matched.get("download_dir_custom") and task_dir:
        chosen["download_dir"] = task_dir
        chosen["download_dir_custom"] = True
    return chosen


def catchup_download_dir(config: dict) -> str:
    """取出补数这次要传给执行入口的下载根目录。

    配置里单独指定了目录时返回该目录。否则返回空字符串，执行入口再去用全局设置。

    Args:
        config (dict): :func:`apply_catchup_storage` 返回的配置。

    Returns:
        str: 下载根目录。空字符串表示沿用全局设置或数据目录。

    Examples:
        >>> catchup_download_dir({"download_dir": "/data", "download_dir_custom": True})
        '/data'
        >>> catchup_download_dir({"download_dir": "/data"})
        ''
    """
    if config.get("download_dir_custom"):
        return str(config.get("download_dir") or "").strip()
    return ""


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
                 station_queue: list[dict] | None = None):
        self.config = task_config
        self.name = task_config.get("name", "unnamed")
        self.login_name = task_config.get("login", "")
        self.stop_on_error = task_config.get("stop_on_error", True)
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
                    for index, item in enumerate(runs):
                        if self._stopped or session._cancel_requested or self._failed:
                            self._emit_skipped_download_plans(runs[index:], _emit)
                            break
                        indicator = item["indicator"]
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
                        await self._cleanup_between_indicators()
                        repeat_dates = dates if mode == "repeat" else None
                        anchor = dates[0] if dates else None
                        if mode == "month":
                            anchor = dates[-1] if dates else None
                        await self._run_indicator_shared(
                            indicator, step_idx, total_steps, _emit, anchor, repeat_dates,
                            query_month=item.get("query_month"),
                            output_dates=item.get("output_dates"),
                        )
                    if self._stopped or session._cancel_requested or self._failed or self._had_error:
                        if not self._task_error_emitted:
                            _emit("task_error", error=self._failure_reason or "用户中止")
                            self._task_error_emitted = True
                        break
                    if station_index == len(stations) - 1:
                        _emit("task_done", name=self.name)
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

    async def _run_indicator_shared(
        self, indicator_name: str, step_idx: int, total: int, _emit,
        target_date: date | None = None, repeat_dates: list[date] | None = None,
        query_month: str | None = None, output_dates: dict[str, list[date]] | None = None,
    ):
        """共享浏览器模式下执行单个指标。有循环日期时整份流程只走一遍，日期在循环里换。

        ``output_dates`` 是单个导出另配的日期（键是输出 key），循环里按天合并执行。
        """
        prepared = self._prepare_indicator(
            indicator_name, target_date, repeat_dates, query_month, output_dates,
        )
        if not prepared:
            self._record_failure(f"指标不存在: {indicator_name}", stop=self.stop_on_error)
            _emit("task_step", step=step_idx, total=total,
                  phase="indicator", name=indicator_name, status="error",
                  message=f"指标不存在: {indicator_name}")
            if self.stop_on_error:
                _emit("task_error", error=f"指标不存在: {indicator_name}")
                self._task_error_emitted = True
            return

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
                self._record_failure(reason, stop=self.stop_on_error)
                _emit("task_step", step=step_idx, total=total,
                      phase="indicator", name=indicator_name, status="error",
                      message=reason, target_date=prepared["target_text"])
                if self.stop_on_error:
                    _emit("task_error", error=f"{indicator_name} 失败: {reason}")
                    self._task_error_emitted = True
                return
            _emit("task_step", step=step_idx, total=total,
                  phase="indicator", name=indicator_name, status="done", target_date=prepared["target_text"])
        except Exception as exc:
            self._record_failure(str(exc), stop=self.stop_on_error)
            _emit("task_step", step=step_idx, total=total,
                  phase="indicator", name=indicator_name, status="error",
                  message=str(exc), target_date=prepared["target_text"])
            if self.stop_on_error:
                _emit("task_error", error=f"{indicator_name} 失败: {exc}")
                self._task_error_emitted = True

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
