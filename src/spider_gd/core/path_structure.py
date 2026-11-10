"""任务存储结构：YAML 风格的缩进大纲。

用户配置示例::

    - {wifi}
      - {YYYY-MM-DD}
        - 披露文件
          - [实时节点电价查询导出]
          - [交易日历]
      - 阳江
        - [场站-日前交易结果发电侧导出]

规则：

* 每行一个节点，以 ``-`` 开头，缩进表示层级；
* ``[指标名]`` 是文件标签，其他是文件夹；
* ``{wifi}`` / ``{SSID}`` 解析为执行时的 WiFi 名；
* ``{YYYY}`` ``{YY}`` ``{MM}`` ``{DD}`` ``{YYYY-MM}`` ``{MM-DD}``
  ``{YYYY-MM-DD}`` 按执行日期展开；
* ``{wifi}`` 可以省略，此时就是完全自定义的目录结构；
* 文件标签必须对应任务里的指标名（工作流 ID），不能重复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from spider_gd.core.output_paths import render_task_path
from spider_gd.core.workflow_outputs import OutputSpec, save_as_filename

__all__ = [
    "StructureNode",
    "StructureResult",
    "build_structure_dirs",
    "common_prefix",
    "parse_structure",
    "resolve_structure",
    "save_as_conflicts",
    "structure_indicator_scope",
    "structure_warnings",
]


@dataclass
class StructureNode:
    name: str
    kind: str  # "folder" | "file"
    line: int = 0
    children: list[StructureNode] = field(default_factory=list)


@dataclass
class StructureResult:
    #: 键是 "指标::输出key"，值是该文件的文件夹层级。
    assignments: dict[str, list[str]]
    base_parts: list[str]
    unassigned: list[str]
    #: 结构里引用了、但当前任务/工作流里已经不存在的标签（如 save_as 改名）。
    #: 不阻塞运行：对应文件落到基础目录，由调用方告警。
    invalid: list[str] = field(default_factory=list)


def parse_structure(text: str) -> list[StructureNode]:
    """把缩进大纲解析成节点树；错误信息带行号。"""
    roots: list[StructureNode] = []
    stack: list[tuple[int, StructureNode]] = []

    for lineno, raw in enumerate(str(text or "").splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        if not content.startswith("-"):
            raise ValueError(f"第 {lineno} 行：每行要以 - 开头")
        token = content[1:].strip()
        if not token:
            raise ValueError(f"第 {lineno} 行：- 后面不能为空")

        starts = token.startswith("[")
        ends = token.endswith("]")
        if starts != ends:
            raise ValueError(f"第 {lineno} 行：文件标签要写成 [指标名]，方括号要成对")
        is_file = starts and ends
        name = token[1:-1].strip() if is_file else token
        if not name:
            raise ValueError(f"第 {lineno} 行：名称不能为空")
        if not is_file and ("[" in name or "]" in name):
            raise ValueError(f"第 {lineno} 行：文件夹名不能包含 [ 或 ]")

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if stack and stack[-1][1].kind == "file":
            raise ValueError(f"第 {lineno} 行：文件标签下面不能再有内容")

        node = StructureNode(
            name=name,
            kind="file" if is_file else "folder",
            line=lineno,
        )
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((indent, node))

    return roots


def common_prefix(paths: list[list[str]]) -> list[str]:
    if not paths:
        return []
    prefix = list(paths[0])
    for path in paths[1:]:
        index = 0
        while index < len(prefix) and index < len(path) and prefix[index] == path[index]:
            index += 1
        prefix = prefix[:index]
        if not prefix:
            break
    return prefix


def _folder_parts(name: str, ssid: str, when: date) -> list[str]:
    normalized = str(name or "").replace("{wifi}", "{SSID}").replace("{WIFI}", "{SSID}")
    rendered = render_task_path(normalized, when, ssid)
    parts = list(rendered.parts)
    if not parts:
        raise ValueError(f"文件夹名解析后为空：{name}")
    return parts


def _specs_for(
    indicator: str,
    outputs: dict[str, list[OutputSpec]] | None,
) -> list[OutputSpec]:
    if outputs is not None:
        return outputs.get(indicator, [])
    return [OutputSpec("", indicator, -1, "", "", indicator)]


def resolve_structure(
    text: str,
    indicators: list[str],
    *,
    outputs: dict[str, list[OutputSpec]] | None = None,
    ssid: str = "",
    when: date | None = None,
) -> StructureResult:
    """解析并展开占位符，返回 指标::输出key → 文件夹层级。"""
    nodes = parse_structure(text)
    known = set(indicators)
    assignments: dict[str, list[str]] = {}
    unknown: list[str] = []
    duplicated: list[str] = []

    def assign(spec: OutputSpec, prefix: list[str]) -> None:
        canonical = f"{spec.indicator}::{spec.key}"
        if canonical in assignments:
            duplicated.append(canonical)
            return
        assignments[canonical] = list(prefix)

    def walk(items: list[StructureNode], prefix: list[str]) -> None:
        for node in items:
            if node.kind == "folder":
                parts = _folder_parts(node.name, ssid, when or date.today())
                walk(node.children, [*prefix, *parts])
                continue
            indicator, separator, key = node.name.rpartition("::")
            if not separator:
                indicator, key = node.name, ""
            if indicator not in known:
                unknown.append(node.name)
                continue
            specs = _specs_for(indicator, outputs)
            if key:
                matched = [spec for spec in specs if spec.key == key]
                if not matched and len(specs) == 1:
                    # A date-profile edit can insert a step and shift sN.
                    # The only output remains unambiguous for an old task.
                    matched = specs
                if not matched:
                    unknown.append(node.name)
                    continue
                assign(matched[0], prefix)
            else:
                for spec in specs:
                    assign(spec, prefix)

    walk(nodes, [])

    if duplicated:
        raise ValueError(f"同一个文件在结构里出现了多次：{', '.join(duplicated)}")

    assigned = set(assignments)
    unassigned: list[str] = []
    for indicator in indicators:
        for spec in _specs_for(indicator, outputs):
            canonical = f"{indicator}::{spec.key}"
            if canonical not in assigned:
                unassigned.append(canonical)
    return StructureResult(
        assignments=assignments,
        base_parts=common_prefix(list(assignments.values())),
        unassigned=unassigned,
        invalid=unknown,
    )


def build_structure_dirs(
    root: str | Path,
    result: StructureResult,
) -> tuple[str, dict[str, str]]:
    """返回 (任务基础目录, 指标目录映射)。"""
    base_path = Path(root)
    base = base_path.joinpath(*result.base_parts) if result.base_parts else base_path
    dirs = {
        output_key: str(base_path.joinpath(*parts))
        for output_key, parts in result.assignments.items()
    }
    return str(base), dirs


def save_as_conflicts(
    result: StructureResult,
    outputs: dict[str, list[OutputSpec]] | None = None,
) -> list[str]:
    """同名 save_as 落到同一个目录才会互相覆盖；分别放进不同文件夹没问题。

    按实际文件名判断，给步骤起了 name 也不会让文件名不同。
    未放进结构的文件落到基础目录。返回冲突的 ``指标::输出key``。
    """
    groups: dict[tuple[str, tuple[str, ...], str], list[str]] = {}
    for indicator, specs in (outputs or {}).items():
        for spec in specs:
            filename = save_as_filename(spec.save_as)
            if not filename:
                continue
            canonical = f"{indicator}::{spec.key}"
            folder = tuple(result.assignments.get(canonical, result.base_parts))
            groups.setdefault((indicator, folder, filename), []).append(canonical)
    return [canonical for group in groups.values() if len(group) > 1 for canonical in group]


def structure_indicator_scope(selected: list[str], stored: list[str] | None = None) -> list[str]:
    """解析存储结构时要用的指标名单。

    补数据只跑选中的指标，结构文本却通常来自整张定时任务。
    只用选中名单去对标签，会把这次没选的指标报成已经改名。

    Args:
        selected: 这次实际要跑的指标。
        stored: 结构所属任务上的全部指标。为空时只用 ``selected``。

    Returns:
        list[str]: 去重后的指标名，选中的排在前面。

    Examples:
        >>> structure_indicator_scope(["日清算"], ["日清算", "交易日历"])
        ['日清算', '交易日历']
        >>> structure_indicator_scope(["日清算"], None)
        ['日清算']
    """
    names: list[str] = []
    for source in (selected, stored or []):
        for item in source:
            text = str(item).strip()
            if text and text not in names:
                names.append(text)
    return names


def structure_warnings(
    result: StructureResult,
    outputs: dict[str, list[OutputSpec]] | None = None,
    *,
    indicators: list[str] | None = None,
) -> list[str]:
    """运行时要告诉用户的存储结构问题；都不阻塞任务。

    Args:
        result: :func:`resolve_structure` 的结果。
        outputs: 各指标的输出定义，用来检查同名文件是否落在同一目录。
        indicators: 这次解析用的指标名单。标签的指标不在其中时，说明它不属于本次任务。

    Returns:
        list[str]: 告警句子。同一次失效只出现一句。

    Examples:
        >>> from spider_gd.core.path_structure import resolve_structure
        >>> result = resolve_structure("- [不存在]", ["日清算"])
        >>> "不在当前任务" in structure_warnings(result, indicators=["日清算"])[0]
        True
    """
    known = {str(item) for item in (indicators or [])}
    lines = []
    seen: set[str] = set()
    for tag in result.invalid:
        if tag in seen:
            continue
        seen.add(tag)
        name = tag.split("::", 1)[0]
        if known and name not in known:
            lines.append(
                f"存储结构标签 [{tag}] 不在当前任务的指标里，该文件落到基础目录"
            )
        else:
            lines.append(
                f"存储结构标签 [{tag}] 已失效（输出名或 save_as 已改），该文件落到基础目录"
            )
    conflicts = set(save_as_conflicts(result, outputs))
    for indicator, specs in (outputs or {}).items():
        names = sorted({
            save_as_filename(spec.save_as) for spec in specs
            if f"{indicator}::{spec.key}" in conflicts
        })
        for name in names:
            lines.append(f"{indicator} 里同名文件 {name} 放在同一个目录，会互相覆盖；放进不同文件夹或改 save_as")
    return lines
