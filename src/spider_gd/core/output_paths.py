"""任务级存储路径模板。

设计约定（2026-10-30 与用户确认）：

* 根目录由全局下载目录决定，模板只描述根目录以下的相对路径；
* 模板按**执行日期**展开，支持 ``{YYYY}`` / ``{MM}`` / ``{DD}`` /
  ``{YYYY-MM}`` / ``{YYYY-MM-DD}`` 等写法；
* 模板为空时文件直接落在下载根目录；
* 模板不能逃出根目录：拒绝盘符、绝对路径、``..`` 和 Windows 非法字符。

文件名不在这里配置。浏览器导出保留系统下载名；识别类文件沿用工作流 YAML
里的 ``save_as``。目录由下载根目录和任务存储结构决定。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

__all__ = [
    "PATH_TOKENS",
    "render_task_path",
    "resolve_task_output_dir",
    "choose_download_root",
    "sanitize_segment",
    "template_example",
]

#: 供前端/文档展示的变量，顺序即插入面板顺序。
PATH_TOKENS: tuple[tuple[str, str], ...] = (
    ("{SSID}", "当前连接的 WiFi 名称；未连接时为 未连接WiFi"),
    ("{YYYY}", "四位年份，如 2026"),
    ("{YY}", "两位年份，如 26"),
    ("{MM}", "两位月份，如 10"),
    ("{DD}", "两位日期，如 30"),
    ("{YYYY-MM}", "年月，如 2026-10"),
    ("{MM-DD}", "月日，如 10-30"),
    ("{YYYY-MM-DD}", "年月日，如 2026-10-30"),
)

_TOKEN_RE = re.compile(r"\{([^{}]+)\}")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ILLEGAL_RE = re.compile(r'[<>:"|?*]')
_MAX_SEGMENT = 120


def sanitize_segment(value: str, fallback: str = "") -> str:
    """把 SSID/文件夹名清洗成 Windows 可用的单级目录名。"""
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*]', "_", text).strip(" .")
    if not text:
        return fallback
    return text[:_MAX_SEGMENT]


def _token_values(when: date, ssid: str = "") -> dict[str, str]:
    ssid_label = sanitize_segment(ssid, "未连接WiFi")
    return {
        "SSID": ssid_label,
        "WIFI": ssid_label,
        "YYYY": when.strftime("%Y"),
        "YY": when.strftime("%y"),
        "MM": when.strftime("%m"),
        "DD": when.strftime("%d"),
        "YYYY-MM": when.strftime("%Y-%m"),
        "MM-DD": when.strftime("%m-%d"),
        "YYYY-MM-DD": when.strftime("%Y-%m-%d"),
    }


def render_task_path(template: str, when: date | None = None, ssid: str = "") -> Path:
    """把模板渲染成相对路径；非法模板抛 ``ValueError``（中文提示可直接展示）。"""
    text = str(template or "").strip()
    if not text:
        return Path()

    text = text.replace("\\", "/")
    if text.startswith("/"):
        raise ValueError("存储路径要相对根目录填写，不要以 / 或 \\ 开头")
    if _DRIVE_RE.match(text):
        raise ValueError("存储路径要相对根目录填写，不要带盘符（如 D:）")

    when = when or date.today()
    values = _token_values(when, ssid)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            available = ', '.join(key for key, _ in PATH_TOKENS)
            raise ValueError(f"不认识的变量 {{{key}}}，可用：{available}")
        return values[key]

    rendered = _TOKEN_RE.sub(replace, text)
    if "{" in rendered or "}" in rendered:
        raise ValueError("变量括号不完整，请检查 { 和 } 是否成对")

    parts: list[str] = []
    for raw_part in rendered.split("/"):
        part = raw_part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("存储路径不能包含 ..，避免写到根目录之外")
        if _ILLEGAL_RE.search(part):
            raise ValueError(f"文件夹名不能包含这些字符： < > : \" | ? *（{part}）")
        if part.endswith((".", " ")):
            raise ValueError(f"文件夹名不能以点或空格结尾（{part}）")
        if len(part) > _MAX_SEGMENT:
            raise ValueError(f"文件夹名太长（超过 {_MAX_SEGMENT} 个字符）")
        parts.append(part)

    return Path(*parts)


def resolve_task_output_dir(
    root: str | Path,
    template: str,
    when: date | None = None,
    ssid: str = "",
) -> Path:
    """根目录 + 模板 = 本次执行的落盘目录。"""
    base = Path(root) if str(root or "").strip() else Path(".")
    return base / render_task_path(template, when, ssid)


def choose_download_root(
    explicit: str, task: str, global_setting: str, fallback: str,
) -> Path:
    """Resolve the root once for manual, scheduled, and per-task runs."""
    return Path(next((value.strip() for value in (
        explicit, task, global_setting, fallback,
    ) if str(value or "").strip()), "."))


def template_example(template: str, when: date | None = None, ssid: str = "") -> str:
    """给前端/文档用：渲染失败的模板原样返回，避免预览接口 500。"""
    try:
        return render_task_path(template, when, ssid).as_posix()
    except ValueError:
        return str(template or "")
