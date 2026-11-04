"""把一次执行或一次 UKey 检测收成离线 zip。

执行包只放这一次任务或指标：步骤日志、这段时间的应用日志、用到的工作流，
以及失败时留下的页面 HTML、截图和诊断。UKey 包只放钥匙、证书和绑定关系。
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "build_execution_zip",
    "build_ukey_zip",
    "in_run_window",
    "parse_local_timestamp",
    "read_log_window",
    "redact_secrets",
    "select_runtime_artifacts",
    "slice_log_window",
    "ukey_binding_rows",
]

_SECRET_VALUE = re.compile(
    r"(?i)((?:password|passwd|token|secret|sendkey|openid|cookie)\s*[:=]\s*)(\S+)"
)
_DIAG_DIR_NAMES = {"_failures", "_diagnostics"}
_ARTIFACT_SUFFIXES = {".log", ".txt", ".json", ".jsonl", ".png", ".yml", ".yaml", ".html"}
_SKIP_DIRS = {".git", "node_modules", "__pycache__", "执行日志", "诊断包"}
_BEFORE = timedelta(minutes=2)
_AFTER = timedelta(minutes=10)
_MAX_WALK_ENTRIES = 5000
_MAX_DEPTH = 5
_MAX_FILES = 40
_FALLBACK_FILES = 15
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 40 * 1024 * 1024


def redact_secrets(text: str) -> str:
    """把口令、Token 一类字段的值换成占位符。

    Args:
        text (str): 原始文本，通常是工作流 YAML 或配置片段。

    Returns:
        str: 脱敏后的文本。键名保留，值变成 ``***``。

    Examples:
        >>> redact_secrets("password: hunter2")
        'password: ***'
        >>> redact_secrets("name: 交易日历")
        'name: 交易日历'
    """
    return _SECRET_VALUE.sub(r"\1***", text)


def parse_local_timestamp(value: str) -> datetime | None:
    """解析执行日志里的本地时间。

    Args:
        value (str): ``YYYY-MM-DD HH:MM:SS``。其它写法返回 None。

    Returns:
        datetime | None: 解析成功的本地时间；失败时为 None。

    Examples:
        >>> parse_local_timestamp("2026-11-03 14:05:06")
        datetime.datetime(2026, 11, 3, 14, 5, 6)
        >>> parse_local_timestamp("不是时间") is None
        True
    """
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def in_run_window(mtime: datetime, start: datetime | None, end: datetime | None) -> bool:
    """判断文件修改时间是否落在这次执行附近。

    开始前 2 分钟到结束后 10 分钟都算在内，截图和诊断文件常常比日志晚几秒写完。

    Args:
        mtime (datetime): 文件修改时间，本地时间。
        start (datetime | None): 执行开始。空表示不限制下界。
        end (datetime | None): 执行结束。空表示不限制上界。

    Returns:
        bool: 落在窗口内时为 True。

    Examples:
        >>> start = datetime(2026, 11, 3, 10, 0, 0)
        >>> end = datetime(2026, 11, 3, 10, 5, 0)
        >>> in_run_window(datetime(2026, 11, 3, 10, 2, 0), start, end)
        True
        >>> in_run_window(datetime(2026, 11, 3, 9, 0, 0), start, end)
        False
    """
    started = start is None or mtime >= start - _BEFORE
    ended = end is None or mtime <= end + _AFTER
    return started and ended


def slice_log_window(text: str, start: datetime | None, end: datetime | None) -> str:
    """只保留落在这次执行附近的应用日志。

    没有时间戳的续行跟着上一条带时间的日志走。开始前 2 分钟到结束后 10 分钟都算。

    Args:
        text (str): ``app.log`` 文本。
        start (datetime | None): 执行开始。
        end (datetime | None): 执行结束。

    Returns:
        str: 这次执行对应的日志。没有对应行时返回说明。

    Examples:
        >>> line_old = "2026-11-03 09:00:00.000 | INFO | a:b:1 | t | 旧的"
        >>> line_new = "2026-11-03 10:01:00.000 | INFO | a:b:1 | t | 这次"
        >>> raw = "\\n".join([line_old, line_new])
        >>> start = datetime(2026, 11, 3, 10, 0, 0)
        >>> end = datetime(2026, 11, 3, 10, 5, 0)
        >>> window = slice_log_window(raw, start, end)
        >>> "这次" in window and "旧的" not in window
        True
    """
    if start is None and end is None:
        return "没有这次执行的起止时间，未从 app.log 截取。"
    stamp = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    kept: list[str] = []
    include = False
    for line in text.splitlines():
        matched = stamp.match(line)
        if matched:
            try:
                moment = datetime.strptime(matched.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                include = False
            else:
                include = in_run_window(moment, start, end)
        if include:
            kept.append(line)
    if not kept:
        return "这次执行的时间范围内，app.log 没有对应行。"
    return "\n".join(kept)


def read_log_window(
    path: Path,
    start: datetime | None,
    end: datetime | None,
    *,
    max_bytes: int = 8 * 1024 * 1024,
) -> str:
    """从日志文件末尾读取，并只留下这次执行的时间窗。

    Args:
        path (Path): ``app.log`` 路径。
        start (datetime | None): 执行开始。
        end (datetime | None): 执行结束。
        max_bytes (int): 从文件末尾最多读取的字节数。

    Returns:
        str: 时间窗内的日志，或说明为什么没有。

    Examples:
        >>> start = datetime(2026, 11, 3, 10, 0, 0)
        >>> "不存在" in read_log_window(Path("/no/such/spider-gd-app-log"), start, start)
        True
    """
    if start is None and end is None:
        return "没有这次执行的起止时间，未从 app.log 截取。"
    if not path.is_file():
        return f"日志文件不存在: {path}"
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
        text = raw.decode("utf-8", errors="replace")
    except OSError as exc:
        return f"读取日志失败: {exc}"
    return slice_log_window(text, start, end)


def ukey_binding_rows(stations_json: str) -> list[dict[str, str]]:
    """从场站配置里只取出和 UKey 对应有关的字段。

    Args:
        stations_json (str): 场站 JSON。密码字段会被丢掉。

    Returns:
        list[dict[str, str]]: 每项含名称、设备编号和单位关键字。

    Examples:
        >>> rows = ukey_binding_rows(
        ...     '[{"name":"阳江","deviceId":"SN","orgName":"阳江","wifiPassword":"secret"}]'
        ... )
        >>> rows == [{"name": "阳江", "deviceId": "SN", "orgName": "阳江"}]
        True
    """
    try:
        parsed = json.loads(stations_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    rows: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        rows.append({
            "name": str(item.get("name") or ""),
            "deviceId": str(item.get("deviceId") or item.get("device_id") or ""),
            "orgName": str(item.get("orgName") or item.get("org_name") or ""),
        })
    return rows


def _diagnostic_dirs(root: Path) -> list[Path]:
    """找出下载目录下有限深度内的失败现场目录。

    Args:
        root (Path): 下载根目录。

    Returns:
        list[Path]: ``_failures`` 与 ``_diagnostics`` 目录。
    """
    found: list[Path] = []
    if not root.is_dir():
        return found
    stack = [(root, 0)]
    seen = 0
    while stack:
        current, depth = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            seen += 1
            if seen > _MAX_WALK_ENTRIES:
                return found
            if not child.is_dir() or child.name in _SKIP_DIRS:
                continue
            if child.name in _DIAG_DIR_NAMES:
                found.append(child)
                continue
            if depth < _MAX_DEPTH:
                stack.append((child, depth + 1))
    return found


def select_runtime_artifacts(
    root: Path,
    start: datetime | None,
    end: datetime | None,
) -> tuple[list[Path], list[str]]:
    """挑选这次执行附近的失败截图和诊断文件。

    时间窗里没有文件时，改附最近的一小批，避免时钟对不上就什么都没有。
    单个文件和总大小都有上限，超出的只在说明里记名字。

    Args:
        root (Path): 下载根目录。
        start (datetime | None): 执行开始。
        end (datetime | None): 执行结束。

    Returns:
        tuple[list[Path], list[str]]: 要打进包的文件，以及被跳过或回退的说明。

    Examples:
        >>> missing = Path("/no/such/spider-gd-downloads")
        >>> files, notes = select_runtime_artifacts(missing, None, None)
        >>> files
        []
        >>> notes[0].startswith("下载目录不存在")
        True
    """
    if not root.is_dir():
        return [], ["下载目录不存在，没有附带失败截图和页面诊断。"]
    root_resolved = root.resolve()
    candidates: list[tuple[datetime, Path]] = []
    for folder in _diagnostic_dirs(root):
        try:
            entries = list(folder.rglob("*"))
        except OSError:
            continue
        for path in entries:
            if not path.is_file() or path.suffix.lower() not in _ARTIFACT_SUFFIXES:
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root_resolved)
                mtime = datetime.fromtimestamp(resolved.stat().st_mtime)
            except (OSError, ValueError):
                continue
            candidates.append((mtime, resolved))
    in_window = [path for mtime, path in candidates if in_run_window(mtime, start, end)]
    notes: list[str] = []
    if in_window:
        chosen = in_window
    else:
        newest = sorted(candidates, key=lambda item: item[0], reverse=True)
        chosen = [path for _mtime, path in newest][:_FALLBACK_FILES]
        if chosen:
            notes.append("这次执行的时间范围内没有诊断文件，已附上最近的失败现场文件。")
    chosen.sort()
    selected: list[Path] = []
    total = 0
    for path in chosen:
        if len(selected) >= _MAX_FILES:
            notes.append(f"已达到 { _MAX_FILES } 个现场文件上限，其余未打包。")
            break
        try:
            size = path.stat().st_size
        except OSError:
            notes.append(f"读不到文件，已跳过: {path.name}")
            continue
        if size > _MAX_FILE_BYTES or total + size > _MAX_TOTAL_BYTES:
            notes.append(f"文件过大，已跳过: {path.name} ({size} bytes)")
            continue
        selected.append(path)
        total += size
    return selected, notes


def _readme(summary: dict[str, Any], names: list[str], notes: list[str]) -> str:
    """写给打开压缩包的人看的目录说明。

    Args:
        summary (dict[str, Any]): 执行摘要。
        names (list[str]): 包内文件名。
        notes (list[str]): 没能装进去的原因。

    Returns:
        str: 纯文本说明。
    """
    lines = [
        "这份压缩包是一次执行的离线诊断材料。",
        "不需要再连现场机器，把包发回来即可对照下面几项。",
        "",
        f"名称: {summary.get('name', '')}",
        f"状态: {summary.get('status', '')}",
        f"原因: {summary.get('reason') or '-'}",
        f"开始: {summary.get('created_at', '')}",
        f"更新: {summary.get('updated_at', '')}",
        "",
        "包内文件:",
        "- execution_log.txt：这一次执行的步骤日志",
        "- app_log_window.txt：app.log 里落在这次起止时间附近的行",
        "- summary.json：这次执行的状态、原因和装入了哪些文件",
        "- workflows/：这次用到的工作流，口令已打码",
        "- task_config.json：如果这是一次任务，附上该任务配置",
        "- runtime/：这次失败或暂停时留下的页面 HTML、截图、页面诊断和浏览器动作",
        "",
        "成功跑完、没有报错的页面不会逐页保存 HTML。",
        "",
        "实际装入:",
        *[f"- {name}" for name in names],
    ]
    if notes:
        lines.extend(["", "未装入或回退:", *[f"- {note}" for note in notes]])
    return "\n".join(lines) + "\n"


def build_execution_zip(
    dest: Path,
    *,
    execution_text: str,
    app_log_text: str,
    summary: dict[str, Any],
    workflows: dict[str, str] | None = None,
    task_config: dict[str, Any] | None = None,
    runtime_files: list[tuple[str, Path]] | None = None,
    notes: list[str] | None = None,
) -> None:
    """把一次任务或指标执行写成一个 zip。

    只放这一次执行的日志、用到的工作流，以及失败时留下的页面现场。
    不放整机设置、健康检查或双网卡诊断。

    Args:
        dest (Path): 目标 zip 路径。父目录不存在时会创建。
        execution_text (str): 这次执行的步骤日志。
        app_log_text (str): 这次时间窗内的应用日志。
        summary (dict[str, Any]): 这次执行的摘要。口令应已脱敏。
        workflows (dict[str, str] | None): 文件名到 YAML 正文。None 表示没有对应工作流。
        task_config (dict[str, Any] | None): 这次任务的配置。指标单跑时为 None。
        runtime_files (list[tuple[str, Path]] | None): ``(包内路径, 本地文件)``。
        notes (list[str] | None): 选择现场文件时的说明。

    Returns:
        None: 结果写在 ``dest``。

    Raises:
        OSError: 目标目录或 zip 无法写入时。

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as folder:
        ...     target = Path(folder) / "diag.zip"
        ...     build_execution_zip(
        ...         target,
        ...         execution_text="步骤失败",
        ...         app_log_text="这次的日志",
        ...         summary={"name": "样例", "status": "failed", "reason": ""},
        ...         workflows={"交易日历.yml": "name: 交易日历\\n"},
        ...     )
        ...     with zipfile.ZipFile(target) as archive:
        ...         names = archive.namelist()
        >>> "execution_log.txt" in names and "workflows/交易日历.yml" in names
        True
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    packed = ["README.txt", "execution_log.txt", "app_log_window.txt"]
    payload: list[tuple[str, str | Path]] = [
        ("execution_log.txt", execution_text),
        ("app_log_window.txt", app_log_text),
    ]
    for filename, workflow_text in (workflows or {}).items():
        if not str(workflow_text or "").strip():
            continue
        arcname = f"workflows/{filename}"
        payload.append((arcname, redact_secrets(workflow_text)))
        packed.append(arcname)
    if task_config is not None:
        rendered = json.dumps(task_config, ensure_ascii=False, indent=2)
        payload.append(("task_config.json", redact_secrets(rendered)))
        packed.append("task_config.json")
    runtime_notes = list(notes or [])
    for arcname, path in runtime_files or []:
        if path.is_file():
            payload.append((arcname, path))
            packed.append(arcname)
        else:
            runtime_notes.append(f"现场文件已消失，未打包: {arcname}")
    packed.append("summary.json")
    summary = {
        **summary,
        "bundle_files": packed,
        "bundle_notes": runtime_notes,
    }
    readme = _readme(summary, packed, runtime_notes)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme)
        for arcname, item in payload:
            if isinstance(item, Path):
                archive.write(item, arcname=arcname)
            else:
                archive.writestr(arcname, item)
        archive.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))


def build_ukey_zip(
    dest: Path,
    *,
    report: dict[str, Any],
    raw_usb: str,
    raw_certificates: str,
    bindings: list[dict[str, str]] | None = None,
) -> None:
    """把一次 UKey 检测收成只含钥匙资料的 zip。

    Args:
        dest (Path): 目标 zip 路径。父目录不存在时会创建。
        report (dict[str, Any]): 设备、证书和中文结论。不含私钥。
        raw_usb (str): 系统列出 USB 的原文。
        raw_certificates (str): 证书库查询原文。没有私钥。
        bindings (list[dict[str, str]] | None): 场站绑定的设备编号和单位关键字。

    Returns:
        None: 结果写在 ``dest``。

    Raises:
        OSError: 目标目录或 zip 无法写入时。

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as folder:
        ...     target = Path(folder) / "ukey.zip"
        ...     build_ukey_zip(
        ...         target,
        ...         report={"summary": "发现 1 把飞天 UKey。", "ukeys": [], "error": ""},
        ...         raw_usb="VID_096E",
        ...         raw_certificates="",
        ...     )
        ...     with zipfile.ZipFile(target) as archive:
        ...         text = archive.read("summary.txt").decode("utf-8")
        ...         usb = archive.read("raw_usb.txt").decode("utf-8")
        >>> "飞天 UKey" in text and "096E" in usb
        True
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    summary = str(report.get("summary") or "").strip() or "没有检测结论。"
    safe_report = json.loads(redact_secrets(json.dumps(report, ensure_ascii=False)))
    binding_rows = bindings or []
    readme = "\n".join([
        "这份压缩包只包含这一次 UKey 检测的资料。",
        "没有应用日志，也没有网络或整机设置。",
        "",
        "包内文件:",
        "- summary.txt：中文结论",
        "- report.json：外部 USB、飞天 UKey、证书单位和指纹",
        "- raw_usb.txt：系统列出 USB 的原文",
        "- raw_certificates.txt：证书库查询原文，没有私钥",
        "- bindings.json：场站绑定的设备编号和单位关键字",
        "",
        "结论:",
        summary,
        "",
    ])
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme)
        archive.writestr("summary.txt", summary + "\n")
        archive.writestr("report.json", json.dumps(safe_report, ensure_ascii=False, indent=2))
        archive.writestr("raw_usb.txt", raw_usb or "没有 USB 原文。")
        archive.writestr("raw_certificates.txt", raw_certificates or "没有证书原文。")
        archive.writestr(
            "bindings.json",
            json.dumps(binding_rows, ensure_ascii=False, indent=2),
        )
