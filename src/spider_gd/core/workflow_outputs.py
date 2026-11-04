"""工作流产物清单：一个指标可能导出多个文件。

前端存储结构用 ``[指标::输出key]`` 标记一个具体文件；运行时按 key 把每个
export/extract 步骤落到它自己的目录。

输出 key 优先用步骤上的 ``name``（只用来认出是哪次下载，不决定文件名），
其次是 save_as 的文件名模板（增删步骤不会错位）；都没有或同一工作流里重名时，
才退回步骤位置 ``s<序号>``。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import PureWindowsPath

__all__ = [
    "OUTPUT_ACTIONS",
    "OutputSpec",
    "output_specs_or_fallback",
    "outputs_map",
    "save_as_filename",
    "workflow_outputs",
]

OUTPUT_ACTIONS = {
    "export",
    "extract",
    "extract_table",
    "extract_echarts",
    "extract_calendar",
    "extract_calendar_month_weeks",
    "download_notice_reports",
}


@dataclass(frozen=True)
class OutputSpec:
    key: str          # name > save_as 文件名模板；没有或重名时为 s0 / s3.1 ...
    indicator: str
    step_index: int
    action: str
    save_as: str
    label: str
    position: str = ""       # 步骤位置 s0 / s3.1，运行时按它定位当前步骤
    duplicate: bool = False  # 同一工作流里 name / save_as 文件名重复，需要补 name 区分

    @property
    def slot(self) -> str:
        return self.position or self.key


def save_as_filename(save_as: str) -> str:
    """save_as 只管文件名；历史写法里的目录部分一律丢弃。"""
    return PureWindowsPath(str(save_as or "").strip().replace("/", "\\")).name


def workflow_outputs(cfg: dict, indicator: str) -> list[OutputSpec]:
    """列出一个工作流所有会产出文件的步骤。"""
    steps = cfg.get("steps") if isinstance(cfg, dict) else None
    if not isinstance(steps, list):
        return []
    found: list[tuple[str, int, str, str, str]] = []

    def visit(items: list, prefix: str = "") -> None:
        for index, step in enumerate(items):
            if not isinstance(step, dict):
                continue
            position = f"{prefix}.{index}" if prefix else f"s{index}"
            action = str(step.get("action") or "")
            if action == "repeat":
                visit(step.get("steps") or [], position)
                continue
            if action not in OUTPUT_ACTIONS:
                continue
            found.append((
                position, index, action,
                str(step.get("save_as") or "").strip(),
                str(step.get("name") or "").strip(),
            ))

    visit(steps)
    counts = Counter(name or save_as_filename(save_as) for *_, save_as, name in found)
    result: list[OutputSpec] = []
    for position, index, action, save_as, name in found:
        identity = name or save_as_filename(save_as)
        duplicate = bool(identity) and counts[identity] > 1
        if identity:
            label = identity
        elif action == "export":
            label = "网站原名"
        elif action == "download_notice_reports":
            label = "运行日报附件"
        else:
            label = f"提取结果（第 {index + 1} 步）"
        result.append(OutputSpec(
            key=identity if identity and not duplicate else position,
            indicator=indicator, step_index=index,
            action=action, save_as=save_as, label=label,
            position=position, duplicate=duplicate,
        ))
    return result


def output_specs_or_fallback(cfg: dict, indicator: str) -> list[OutputSpec]:
    """工作流没有可识别产物时，仍给它一个占位输出，避免旧配置直接失效。"""
    specs = workflow_outputs(cfg, indicator)
    if specs:
        return specs
    return [OutputSpec(
        key="",
        indicator=indicator,
        step_index=-1,
        action="",
        save_as="",
        label=indicator,
    )]


def outputs_map(workflows: dict[str, dict]) -> dict[str, list[OutputSpec]]:
    return {
        indicator: workflow_outputs(cfg or {}, indicator)
        for indicator, cfg in workflows.items()
    }
