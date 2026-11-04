"""TaskRunner 指标级子目录：path_folders 映射到实际 download_dir。"""

from __future__ import annotations

from pathlib import Path

import pytest
from task_runner import TaskRunner


@pytest.mark.unit
def test_indicator_output_dir_uses_assigned_folder(tmp_path: Path):
    runner = TaskRunner(
        {
            "name": "路径任务",
            "indicators": ["A", "B"],
            "path_folders": [
                {"name": "阳江", "indicators": ["A"]},
                {"name": "披露信息", "indicators": ["B"]},
            ],
        },
        download_dir=str(tmp_path / "CSG-UKEY" / "2026-10-30"),
    )

    a_dir = Path(runner._indicator_output_dir("A"))
    b_dir = Path(runner._indicator_output_dir("B"))
    assert a_dir == tmp_path / "CSG-UKEY" / "2026-10-30" / "阳江"
    assert b_dir.name == "披露信息"
    assert a_dir.is_dir()
    assert b_dir.is_dir()


@pytest.mark.unit
def test_indicator_output_dir_falls_back_to_base_and_sanitizes(tmp_path: Path):
    runner = TaskRunner(
        {
            "name": "路径任务",
            "indicators": ["A", "B"],
            "path_folders": [{"name": "A:B", "indicators": ["A"]}],
        },
        download_dir=str(tmp_path / "base"),
    )

    assert Path(runner._indicator_output_dir("B")) == tmp_path / "base"
    assert Path(runner._indicator_output_dir("A")).name == "A_B"


@pytest.mark.unit
def test_indicator_output_dir_supports_nested_folders(tmp_path: Path):
    runner = TaskRunner(
        {
            "name": "路径任务",
            "indicators": ["A"],
            "path_folders": [
                {
                    "name": "披露文件",
                    "children": [
                        {"name": "交易到时", "indicators": ["A"]},
                    ],
                }
            ],
        },
        download_dir=str(tmp_path / "CSG-UKEY"),
    )

    expected = tmp_path / "CSG-UKEY" / "披露文件" / "交易到时"
    assert Path(runner._indicator_output_dir("A")) == expected


@pytest.mark.unit
def test_indicator_output_dir_supports_slash_path(tmp_path: Path):
    runner = TaskRunner(
        {
            "name": "路径任务",
            "indicators": ["A"],
            "path_folders": [{"name": "披露信息/交易到时", "indicators": ["A"]}],
        },
        download_dir=str(tmp_path / "CSG-UKEY"),
    )

    expected = tmp_path / "CSG-UKEY" / "披露信息" / "交易到时"
    assert Path(runner._indicator_output_dir("A")) == expected


@pytest.mark.unit
def test_indicator_dirs_override_folder_tree(tmp_path: Path):
    a_dir = tmp_path / "CSG-UKEY" / "披露文件"
    runner = TaskRunner(
        {
            "name": "结构任务",
            "indicators": ["A", "B"],
            "path_folders": [{"name": "旧目录", "indicators": ["A"]}],
        },
        download_dir=str(tmp_path / "CSG-UKEY"),
        indicator_dirs={"A": str(a_dir)},
    )

    assert Path(runner._indicator_output_dir("A")) == a_dir
    assert Path(runner._indicator_output_dir("B")) == tmp_path / "CSG-UKEY"
    assert a_dir.is_dir()
