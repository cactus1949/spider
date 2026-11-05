"""打包最近两天的诊断材料，并清掉包里的明文密钥。

压缩包给现场排查用：日志、执行记录、工作流、任务、失败截图和一份环境说明。
证书密码、账号密码、WiFi 密码、各类 Token 和 Webhook 不能以明文留下。
打包后再扫一遍，发现已知密钥的原文就替换掉。
"""

from __future__ import annotations

import json
import platform
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from spider_gd.gateway.execution_bundle import (
    _MAX_FILE_BYTES,
    _MAX_FILES,
    _MAX_TOTAL_BYTES,
    redact_secrets,
)

_TEXT_SUFFIXES = {".log", ".txt", ".json", ".yml", ".yaml", ".html", ".env", ".md"}
_SECRET_KEYS = (
    "certPassword",
    "accountPassword",
    "wifiPassword",
    "workshopUploadToken",
    "deliveryWecomWebhook",
    "deliveryServerchanSendKey",
    "deliveryPushplusToken",
    "deliveryRemoteUploadToken",
    "webhook",
    "alertWecomBackupWebhook",
    "password",
    "token",
    "secret",
)


def partial_mask(value: str) -> str:
    """账号一类名称只留首尾，中间打星。

    Args:
        value (str): 原始名称。

    Returns:
        str: 打星后的文本。空字符串仍为空。

    Examples:
        >>> partial_mask("zhangsan")
        'z******n'
        >>> partial_mask("ab")
        'a*'
        >>> partial_mask("")
        ''
    """
    text = str(value or "")
    if not text:
        return ""
    if len(text) == 1:
        return "*"
    if len(text) == 2:
        return text[0] + "*"
    return text[0] + ("*" * (len(text) - 2)) + text[-1]


def collect_secret_values(settings: dict[str, str], extra: list[str] | None = None) -> list[str]:
    """从设置和额外明文里收集需要从诊断包抹掉的密钥。

    短于 4 个字符的值不参与整包替换，避免误伤普通单词。掩码 ``******`` 忽略。

    Args:
        settings (dict[str, str]): 应用设置原文。
        extra (list[str] | None): 场站密码、.env、上传配置里另行取出的明文。

    Returns:
        list[str]: 去重后的密钥，长的在前，避免短密钥先替换把长密钥切碎。

    Examples:
        >>> collect_secret_values({"certPassword": "pin-7788", "siteUrl": "https://pm.gd.csg.cn"})
        ['pin-7788']
    """
    found: list[str] = []
    for key, value in settings.items():
        text = str(value or "").strip()
        lowered = key.lower()
        if text in {"", "******"} or len(text) < 4:
            continue
        if any(part.lower() in lowered for part in _SECRET_KEYS) or key in _SECRET_KEYS:
            found.append(text)
    for value in extra or []:
        text = str(value or "").strip()
        if text and text != "******" and len(text) >= 4:
            found.append(text)
    unique = list(dict.fromkeys(found))
    unique.sort(key=len, reverse=True)
    return unique


def redact_known_secrets(text: str, secrets: list[str]) -> str:
    """先按字段名脱敏，再把已知密钥的原文换成占位符。

    Args:
        text (str): 日志、yml 或 json 文本。
        secrets (list[str]): ``collect_secret_values`` 的结果。

    Returns:
        str: 替换后的文本。

    Examples:
        >>> redact_known_secrets("证书 pin-7788", ["pin-7788"])
        '证书 ***'
    """
    masked = redact_secrets(text)
    for secret in secrets:
        if secret and secret in masked:
            masked = masked.replace(secret, "***")
    return masked


def _tail_bytes(path: Path, limit: int) -> bytes:
    """读文件末尾，避免把几百兆日志整段塞进压缩包。

    Args:
        path (Path): 本地文件。
        limit (int): 最多读取的字节数。

    Returns:
        bytes: 文件末尾。文件比上限小就整份读入。

    Examples:
        >>> _tail_bytes(Path("/no/such"), 8)
        b''
    """
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    try:
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read(limit)
    except OSError:
        return b""


def _recent(path: Path, cutoff: datetime) -> bool:
    """文件修改时间是否不早于截止日期。

    Args:
        path (Path): 文件。
        cutoff (datetime): 本地时间下界。

    Returns:
        bool: 新文件或读不到时间时为 True，避免漏掉正在写的日志。

    Examples:
        >>> _recent(Path("/no/such"), datetime(2026, 11, 1))
        True
    """
    try:
        return datetime.fromtimestamp(path.stat().st_mtime) >= cutoff
    except OSError:
        return True


def _walk_named(root: Path, cutoff: datetime, names: set[str]) -> list[Path]:
    """在目录里找最近的失败现场和诊断日志。

    Args:
        root (Path): 下载目录或数据目录。
        cutoff (datetime): 只收这个时间之后改过的文件。
        names (set[str]): 目录名，例如 ``_failures``。

    Returns:
        list[Path]: 命中的文件，按修改时间新的在前。

    Examples:
        >>> _walk_named(Path("/no/such"), datetime(2026, 11, 1), {"_failures"})
        []
    """
    if not root.exists():
        return []
    found: list[Path] = []
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            in_diag_dir = path.parent.name in names or any(part in names for part in path.parts)
            named = path.name.startswith(("login_diagnostics_", "diagnostics_"))
            if not in_diag_dir and not named:
                continue
            interesting = named or path.suffix.lower() in {".png", ".html", ".log", ".txt"}
            if interesting and _recent(path, cutoff):
                found.append(path)
    except OSError:
        return found
    found.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    return found


def mask_settings(settings: dict[str, str], secrets: list[str]) -> dict[str, str]:
    """给环境说明用的设置副本：密钥整段打星，账号名只留首尾。

    Args:
        settings (dict[str, str]): 原始设置。
        secrets (list[str]): 已知明文密钥，用来清掉嵌在 JSON 里的值。

    Returns:
        dict[str, str]: 可以写进 ``env.json`` 的设置。

    Examples:
        >>> mask_settings({"account": "alice", "certPassword": "pin-7788"}, ["pin-7788"])["account"]
        'a***e'
    """
    masked: dict[str, str] = {}
    for key, value in settings.items():
        text = redact_known_secrets(str(value or ""), secrets)
        lowered = key.lower()
        if any(part.lower() in lowered for part in _SECRET_KEYS) or key in _SECRET_KEYS:
            text = "***" if text else ""
        elif key in {"account", "accountName"} and text:
            text = partial_mask(text)
        masked[key] = text
    return masked


def _env_snapshot(ctx_bits: dict[str, Any]) -> dict[str, Any]:
    """组装环境说明。采集失败的项写成错误字符串，不让整包失败。

    Args:
        ctx_bits (dict[str, Any]): 调用方已经准备好的路径、版本和设置。

    Returns:
        dict[str, Any]: ``env.json`` 的内容。

    Examples:
        >>> isinstance(_env_snapshot({"install_path": "/opt/app"})["os"], str)
        True
    """
    versions: dict[str, str] = {"python": sys.version.split()[0]}
    try:
        import importlib.metadata as metadata

        versions["playwright"] = metadata.version("playwright")
    except Exception:
        versions["playwright"] = "未知"
    versions["chromium"] = "未知"
    screen = "未知"
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            screen = f"{user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}"
        except Exception as exc:
            screen = f"读取失败: {exc}"
    return {
        "os": platform.platform(),
        "app_version": ctx_bits.get("app_version", ""),
        "python": versions["python"],
        "playwright": versions["playwright"],
        "chromium": versions["chromium"],
        "screen": screen,
        "install_path": ctx_bits.get("install_path", ""),
        "data_path": ctx_bits.get("data_path", ""),
        "sqlite_path": ctx_bits.get("sqlite_path", ""),
        "user_home": ctx_bits.get("user_home", ""),
        "disk_free_bytes": ctx_bits.get("disk_free_bytes", 0),
        "network": ctx_bits.get("network", {}),
        "wifi_ssid": ctx_bits.get("wifi_ssid", ""),
        "ukey": ctx_bits.get("ukey", {}),
        "settings": ctx_bits.get("settings", {}),
    }


def build_diagnostics_zip(
    dest: Path,
    *,
    settings: dict[str, str],
    store: Any = None,
    log_dir: Path | None = None,
    download_dirs: list[Path] | None = None,
    workflow_dirs: list[Path] | None = None,
    tasks_dir: Path | None = None,
    install_path: str = "",
    data_path: str = "",
    sqlite_path: str = "",
    user_home: str = "",
    app_version: str = "",
    extra_secrets: list[str] | None = None,
    network: dict[str, Any] | None = None,
    wifi_ssid: str = "",
    ukey: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """把最近两天的诊断材料写成 zip，并清掉已知密钥的明文。

    Args:
        dest (Path): 目标 zip。父目录不存在时会创建。
        settings (dict[str, str]): 应用设置原文，只用于挑密钥和写打星后的副本。
        store: 状态库，用来导出执行日志和工作流。可以为空。
        log_dir (Path | None): 日志目录。
        download_dirs (list[Path] | None): 可能含 ``_failures`` 的目录。
        workflow_dirs (list[Path] | None): 磁盘上的工作流目录。
        tasks_dir (Path | None): 任务 YAML 目录。
        install_path (str): 安装目录。
        data_path (str): 数据目录。
        sqlite_path (str): 数据库路径。
        user_home (str): 用户数据目录。
        app_version (str): 桌面端版本。
        extra_secrets (list[str] | None): 设置以外的明文密钥。
        network (dict[str, Any] | None): 网卡信息。
        wifi_ssid (str): 当前 WiFi 名称，不含密码。
        ukey (dict[str, Any] | None): UKey 检测结果。
        now (datetime | None): 当前时间，测试可注入。

    Returns:
        list[str]: 打包说明。

    Raises:
        OSError: 目标 zip 无法写入时。

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as folder:
        ...     target = Path(folder) / "diag.zip"
        ...     notes = build_diagnostics_zip(
        ...         target, settings={"certPassword": "pin-7788", "siteUrl": "https://x"})
        ...     with zipfile.ZipFile(target) as archive:
        ...         env = archive.read("env.json").decode("utf-8")
        >>> "pin-7788" not in env and "env.json" in archive.namelist()
        True
    """
    moment = now or datetime.now()
    cutoff = moment - timedelta(days=2)
    secrets = collect_secret_values(settings, extra_secrets)
    notes: list[str] = []
    payload: list[tuple[str, bytes]] = []
    total = 0

    def add_bytes(arcname: str, data: bytes) -> None:
        nonlocal total
        if any(name == arcname for name, _blob in payload):
            return
        if len(payload) >= _MAX_FILES:
            notes.append(f"已达到 {_MAX_FILES} 个文件上限，跳过 {arcname}")
            return
        blob = data
        if len(blob) > _MAX_FILE_BYTES:
            blob = blob[-_MAX_FILE_BYTES:]
            notes.append(f"{arcname} 超过单文件上限，只保留末尾。")
        if total + len(blob) > _MAX_TOTAL_BYTES:
            notes.append(f"已达到总大小上限，跳过 {arcname}")
            return
        payload.append((arcname, blob))
        total += len(blob)

    def add_text(arcname: str, text: str) -> None:
        cleaned = redact_known_secrets(text, secrets)
        add_bytes(arcname, cleaned.encode("utf-8"))

    if log_dir and log_dir.exists():
        for path in sorted(log_dir.rglob("*")):
            if not path.is_file():
                continue
            keep = path.name in {"app.log", "error.log"} or _recent(path, cutoff)
            if not keep:
                continue
            suffix = path.suffix.lower()
            if suffix not in {".log", ".txt", ".zip", ""} and "log" not in path.name:
                continue
            relative = path.relative_to(log_dir).as_posix()
            add_bytes(f"logs/{relative}", _tail_bytes(path, _MAX_FILE_BYTES))

    if store is not None and hasattr(store, "list_execution_logs"):
        start = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        try:
            logs = store.list_execution_logs(page=1, page_size=200, start_time=start)
        except Exception as exc:
            logs = []
            notes.append(f"导出执行日志失败: {exc}")
        for item in logs:
            body = (
                f"exec_id: {item.exec_id}\nname: {item.name}\ntype: {item.type}\n"
                f"status: {item.status}\nreason: {item.reason}\n\n{item.detail_log or ''}"
            )
            add_text(f"execution_logs/{item.exec_id}.txt", body)

    if store is not None and hasattr(store, "list_workflow_configs"):
        try:
            for cfg in store.list_workflow_configs():
                name = str(cfg.get("id") or cfg.get("name") or "workflow")
                add_text(f"workflows/{name}.yml", str(cfg.get("yaml_text") or ""))
        except Exception as exc:
            notes.append(f"导出工作流失败: {exc}")

    for folder in workflow_dirs or []:
        if not folder or not folder.exists():
            continue
        for path in sorted(folder.glob("*.yml")):
            add_text(f"workflows/{path.name}", path.read_text(encoding="utf-8", errors="replace"))

    if tasks_dir and tasks_dir.exists():
        for path in sorted(tasks_dir.glob("*.yml")):
            add_text(f"tasks/{path.name}", path.read_text(encoding="utf-8", errors="replace"))
    if store is not None and hasattr(store, "list_task_configs"):
        try:
            rendered = json.dumps(store.list_task_configs(), ensure_ascii=False, indent=2)
            add_text("tasks/task_configs.json", rendered)
        except Exception as exc:
            notes.append(f"导出任务配置失败: {exc}")

    shot_index = 0
    for root in download_dirs or []:
        for path in _walk_named(root, cutoff, {"_failures", "login_diagnostics_", "diagnostics_"}):
            shot_index += 1
            try:
                relative = path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                relative = path.name
            add_bytes(f"failures/{shot_index:02d}_{relative}", _tail_bytes(path, _MAX_FILE_BYTES))

    try:
        disk_free = __import__("shutil").disk_usage(data_path or install_path or ".").free
    except OSError:
        disk_free = 0
    env_doc = _env_snapshot({
        "app_version": app_version,
        "install_path": install_path,
        "data_path": data_path,
        "sqlite_path": sqlite_path,
        "user_home": user_home,
        "disk_free_bytes": disk_free,
        "network": network or {},
        "wifi_ssid": wifi_ssid,
        "ukey": ukey or {},
        "settings": mask_settings(settings, secrets),
    })
    add_text("env.json", json.dumps(env_doc, ensure_ascii=False, indent=2))
    readme = "诊断包已脱敏。若仍发现密钥，请不要外传并联系维护人员。\n" + "\n".join(notes)
    add_text("README.txt", readme)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, blob in payload:
            archive.writestr(arcname, blob)
    _strip_plaintext_secrets(dest, secrets)
    return notes


def _strip_plaintext_secrets(dest: Path, secrets: list[str]) -> None:
    """打包后再扫一遍，把残留的明文密钥替换掉。

    文本文件直接替换。二进制里如果出现密钥原文，整份丢掉并在 README 里说明，
    避免改坏图片。

    Args:
        dest (Path): 已经写好的 zip。
        secrets (list[str]): 要查找的明文。

    Returns:
        None: 原地覆盖 zip。没有密钥时不动内容。

    Examples:
        >>> _strip_plaintext_secrets(Path("/no/such.zip"), []) is None
        True
    """
    usable = [item for item in secrets if item]
    if not usable or not dest.is_file():
        return
    replaced = False
    notes: list[str] = []
    rewritten: list[tuple[str, bytes]] = []
    readme = ""
    with zipfile.ZipFile(dest, "r") as source:
        for info in source.infolist():
            data = source.read(info.filename)
            suffix = Path(info.filename).suffix.lower()
            text_like = suffix in _TEXT_SUFFIXES or info.filename.endswith("README.txt")
            if text_like:
                try:
                    text = data.decode("utf-8")
                except UnicodeError:
                    text = ""
                if text:
                    cleaned = redact_known_secrets(text, usable)
                    if cleaned != text:
                        replaced = True
                    blob = cleaned.encode("utf-8")
                    if info.filename == "README.txt":
                        readme = cleaned
                    rewritten.append((info.filename, blob))
                    continue
            leaked = any(secret.encode("utf-8") in data for secret in usable)
            if leaked:
                replaced = True
                notes.append(f"已移除仍含密钥原文的二进制文件: {info.filename}")
                continue
            rewritten.append((info.filename, data))
    if notes:
        replaced = True
        extra = readme + "\n" + "\n".join(notes)
        rewritten = [(name, blob) for name, blob in rewritten if name != "README.txt"]
        rewritten.append(("README.txt", extra.encode("utf-8")))
    if not replaced:
        return
    output = dest.with_suffix(".scrub.zip")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, blob in rewritten:
            target.writestr(name, blob)
    output.replace(dest)


def _example_archive_names() -> bool:
    """给上面的 doctest一个稳定的名字检查。

    Returns:
        bool: 恒为 True。占位，避免把 zip 句柄带出示例。

    Examples:
        >>> _example_archive_names()
        True
    """
    return True
