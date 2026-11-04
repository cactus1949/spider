"""每个输出步骤可以落到自己的目录。"""

from __future__ import annotations

from pathlib import Path
from datetime import date

import pytest
from loguru import logger
from workflow_runner import WorkflowRunner


@pytest.mark.unit
def test_active_output_dir_overrides_download_dir(tmp_path: Path):
    base = tmp_path / "base"
    target = tmp_path / "base" / "披露文件"
    runner = WorkflowRunner(
        {"name": "x", "steps": []},
        download_dir=str(base),
        output_dirs={"s3": str(target)},
    )

    runner._active_output_dir = runner.output_dirs["s3"]
    resolved = runner._resolve_save_path("文件({date}).xlsx")

    assert resolved.parent == target
    assert resolved.name.startswith("文件(")


@pytest.mark.unit
def test_no_active_output_dir_uses_task_base(tmp_path: Path):
    runner = WorkflowRunner({"name": "x", "steps": []}, download_dir=str(tmp_path / "base"))
    runner._active_output_dir = ""

    resolved = runner._resolve_save_path("x.xlsx")

    assert resolved == tmp_path / "base" / "x.xlsx"


@pytest.mark.unit
def test_save_as_directory_is_ignored_with_warning(tmp_path: Path):
    target = tmp_path / "下载" / "日前"
    runner = WorkflowRunner(
        {"name": "x", "steps": []}, download_dir=str(tmp_path / "下载"),
        output_dirs={"s3": str(target)},
    )
    runner._active_output_dir = runner.output_dirs["s3"]
    messages: list[str] = []
    sink = logger.add(lambda msg: messages.append(str(msg)), level="WARNING")
    try:
        resolved = runner._resolve_save_path(
            "/现货分时分类型出清电量/日前/现货分时分类型出清电量{date}.xlsx"
        )
    finally:
        logger.remove(sink)

    assert resolved.parent == target
    assert resolved.name.startswith("现货分时分类型出清电量")
    assert any("save_as" in message and "目录" in message for message in messages)


@pytest.mark.unit
def test_export_renames_with_save_as(tmp_path: Path):
    runner = WorkflowRunner(
        {"name": "x", "steps": []}, download_dir=str(tmp_path), target_date="2026-10-29",
    )
    assert runner._export_path("出清电量_日前_{date}.xlsx", "现货分时分类型出清电量.xlsx") == (
        tmp_path / "出清电量_日前_2026-10-29.xlsx"
    )


@pytest.mark.unit
def test_export_without_save_as_keeps_site_name(tmp_path: Path):
    runner = WorkflowRunner({"name": "x", "steps": []}, download_dir=str(tmp_path))
    assert runner._export_path("", "现货分时分类型出清电量.xlsx") == (
        tmp_path / "现货分时分类型出清电量.xlsx"
    )


@pytest.mark.unit
def test_export_extension_follows_browser_download(tmp_path: Path):
    runner = WorkflowRunner(
        {"name": "x", "steps": []}, download_dir=str(tmp_path), target_date="2026-10-29",
    )
    messages: list[str] = []
    sink = logger.add(lambda msg: messages.append(str(msg)), level="WARNING")
    try:
        path = runner._export_path("结算单_{date}.xlsx", "20260901-20260930结算单.zip")
    finally:
        logger.remove(sink)
    assert path == tmp_path / "结算单_2026-10-29.zip"
    assert any("扩展名" in message for message in messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeat_uses_nested_output_directory(tmp_path: Path):
    target = tmp_path / "配置文件夹"
    runner = WorkflowRunner(
        {"name": "x", "steps": []}, download_dir=str(tmp_path),
        repeat_dates=[date(2026, 10, 29)], output_dirs={"s0.1": str(target)},
    )
    runner._executing_step_index = 0
    seen = []

    async def record(step):
        seen.append((step["action"], runner._active_output_dir))

    runner._exec_step = record
    await runner._do_repeat({"action": "repeat", "steps": [
        {"action": "pick_date", "value": "{target}"},
        {"action": "export", "save_as": "x.xlsx"},
    ]})
    assert seen == [("pick_date", ""), ("export", str(target))]
