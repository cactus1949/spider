"""任务级自定义文件名。

用户只填文件名主体和数据日期占位符。扩展名沿用原文件。
没启用、没填写或填得不合法时，调用方继续用工作流里的命名。
"""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

__all__ = [
    "EXAMPLE_DATE",
    "PLACEHOLDERS",
    "active_templates",
    "filename_suffix",
    "normalize_custom_filenames",
    "overwrite_file",
    "render_custom_filename",
    "templates_for_indicator",
    "validate_custom_filename",
]

EXAMPLE_DATE = date(2026, 11, 12)
PLACEHOLDERS = {
    "yyyy": "%Y",
    "yyyyMM": "%Y%m",
    "yyyyMMdd": "%Y%m%d",
    "MM": "%m",
    "dd": "%d",
    "yyyy-MM": "%Y-%m",
    "yyyy-MM-dd": "%Y-%m-%d",
}
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_ILLEGAL = set('\\/:*?"<>|')
_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_MAX_STEM = 180
_KNOWN_SUFFIXES = {
    ".xlsx", ".xls", ".csv", ".json", ".zip", ".txt", ".xml", ".html", ".pdf",
}


def validate_custom_filename(template: str) -> str:
    """检查用户填写的文件名主体。通过时返回空字符串。

    Args:
        template: 不含扩展名的文件名，可以带数据日期占位符。

    Returns:
        str: 给界面的错误说明。合法时为空字符串。

    Examples:
        >>> validate_custom_filename("现货-{yyyyMMdd}")
        ''
        >>> validate_custom_filename("现货.xlsx")
        '不要填写扩展名，扩展名会自动沿用原文件'
        >>> "不认识" in validate_custom_filename("现货-{date}")
        True
    """
    raw = str(template or "")
    text = raw.strip()
    if not text:
        return "文件名不能为空"
    if raw != text or text.endswith("."):
        return "文件名不能以空格或点开头或结尾"
    if any(char in _ILLEGAL or ord(char) < 32 for char in text):
        return '文件名不能包含 \\ / : * ? " < > |'
    if text.count("{") != text.count("}"):
        return "花括号要成对"
    if "{" in _PLACEHOLDER.sub("", text) or "}" in _PLACEHOLDER.sub("", text):
        return "花括号要成对"
    for match in _PLACEHOLDER.finditer(text):
        token = match.group(1)
        if token not in PLACEHOLDERS:
            shown = token or "空"
            return f"不认识的占位符 {{{shown}}}"
    if re.search(r"\.[A-Za-z0-9]{1,8}$", text):
        return "不要填写扩展名，扩展名会自动沿用原文件"
    try:
        rendered = _render_stem(text, EXAMPLE_DATE)
    except ValueError as exc:
        return str(exc)
    if not rendered or rendered in {".", ".."}:
        return "文件名不能为空"
    if rendered.endswith(" ") or rendered.endswith("."):
        return "文件名不能以空格或点结尾"
    if len(rendered) > _MAX_STEM:
        return "文件名过长"
    reserved = rendered.split(".", 1)[0].upper()
    if reserved in _RESERVED:
        return "不能使用 Windows 保留名"
    return ""


def render_custom_filename(template: str, when: date, suffix: str) -> str:
    """把自定义主体渲染成带扩展名的文件名。

    Args:
        template: 已通过校验的文件名主体。
        when: 数据日期。补数跨天时每一天各传各的日期。
        suffix: 原文件扩展名，带点，如 ``.xlsx``。

    Returns:
        str: 渲染后的文件名。

    Examples:
        >>> render_custom_filename("现货-{yyyyMMdd}", date(2026, 11, 12), ".xlsx")
        '现货-20261112.xlsx'
        >>> render_custom_filename("月报-{yyyy-MM}", date(2026, 11, 12), "csv")
        '月报-2026-11.csv'
    """
    error = validate_custom_filename(template)
    if error:
        raise ValueError(error)
    stem = _render_stem(str(template).strip(), when)
    extension = _normalize_suffix(suffix)
    return f"{stem}{extension}"


def filename_suffix(save_as: str, default: str = ".xlsx") -> str:
    """从工作流文件名里取出扩展名。

    Args:
        save_as: 步骤上的文件名模板。
        default: 模板里没有扩展名时使用的扩展名。

    Returns:
        str: 带点的扩展名。

    Examples:
        >>> filename_suffix("用电量_{date}.xlsx")
        '.xlsx'
        >>> filename_suffix("网站原名")
        '.xlsx'
    """
    leaf = str(save_as or "").replace("\\", "/").rsplit("/", 1)[-1]
    match = re.search(r"(\.[A-Za-z0-9]{1,8})$", leaf)
    if match and match.group(1).lower() in _KNOWN_SUFFIXES:
        return match.group(1)
    return _normalize_suffix(default)


def normalize_custom_filenames(raw: object) -> dict[str, dict[str, object]]:
    """把任务配置里的自定义命名收成稳定结构。

    旧配置没有这个字段、或字段不是对象时，返回空字典。
    开关关掉的项仍保留正文，但运行时不会使用。

    Args:
        raw: 数据库或请求里的原始值。

    Returns:
        dict[str, dict]: 键是 ``指标::输出key``。

    Examples:
        >>> normalize_custom_filenames(None)
        {}
        >>> normalize_custom_filenames({"日前::s0": {"enabled": True, "template": "  a-{yyyy} "}})
        {'日前::s0': {'enabled': True, 'template': 'a-{yyyy}'}}
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            import json
            raw = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        canonical = str(key or "").strip()
        if not canonical or not isinstance(value, dict):
            continue
        template = str(value.get("template") or "").strip()
        enabled = bool(value.get("enabled")) and bool(template)
        if not template and not enabled:
            continue
        result[canonical] = {"enabled": enabled, "template": template}
    return result


def active_templates(mapping: object) -> dict[str, str]:
    """只留下已启用且校验通过的模板。

    Args:
        mapping: :func:`normalize_custom_filenames` 的结果，或原始配置。

    Returns:
        dict[str, str]: 键是 ``指标::输出key``，值是文件名主体。

    Examples:
        >>> active_templates({"日前::s0": {"enabled": False, "template": "a-{yyyy}"}})
        {}
        >>> active_templates({"日前::s0": {"enabled": True, "template": "a-{yyyy}"}})
        {'日前::s0': 'a-{yyyy}'}
    """
    result: dict[str, str] = {}
    for key, item in normalize_custom_filenames(mapping).items():
        template = str(item.get("template") or "")
        if not item.get("enabled") or validate_custom_filename(template):
            continue
        result[key] = template
    return result


def templates_for_indicator(mapping: object, indicator: str) -> dict[str, str]:
    """取出一个指标下、按输出 key 索引的已启用模板。

    Args:
        mapping: 任务上的全部自定义命名。
        indicator: 指标 id。

    Returns:
        dict[str, str]: 键是输出 key，不是带指标前缀的完整标签。

    Examples:
        >>> templates_for_indicator(
        ...     {"日前::电量": {"enabled": True, "template": "电量-{yyyyMMdd}"}},
        ...     "日前",
        ... )
        {'电量': '电量-{yyyyMMdd}'}
        >>> templates_for_indicator({}, "日前")
        {}
    """
    prefix = f"{str(indicator or '').strip()}::"
    found: dict[str, str] = {}
    for key, template in active_templates(mapping).items():
        if key.startswith(prefix):
            found[key[len(prefix):]] = template
    return found


def overwrite_file(source: str | Path, dest: str | Path) -> None:
    """用新文件替换目标。目标已存在时直接覆盖，不另存版本。

    Args:
        source: 刚下载的临时文件。
        dest: 最终路径。

    Returns:
        None: 替换完成后返回。临时文件不再保留。

    Examples:
        >>> overwrite_file.__name__
        'overwrite_file'
    """
    source_path = Path(source)
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        dest_path.unlink()
    shutil.move(str(source_path), dest_path)


def _render_stem(template: str, when: date) -> str:
    """替换占位符。未知占位符在校验阶段已经拦住。"""

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        pattern = PLACEHOLDERS.get(token)
        if pattern is None:
            raise ValueError(f"不认识的占位符 {{{token or '空'}}}")
        return when.strftime(pattern)

    return _PLACEHOLDER.sub(replace, template)


def _normalize_suffix(suffix: str) -> str:
    text = str(suffix or "").strip()
    if not text:
        return ".xlsx"
    if not text.startswith("."):
        text = f".{text}"
    return text
