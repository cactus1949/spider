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


@pytest.fixture
def beta_home(tmp_path, monkeypatch):
    home = tmp_path / "beta"
    home.mkdir()
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    return home


@pytest.mark.parametrize("kind", ["explicit", "folder_link", "fallback", "empty_folder"])
def test_beta_indicator_directory_rejects_escape_before_mkdir(beta_home, tmp_path, kind):
    outside = tmp_path / "outside"
    runner = TaskRunner({"name": "paths"}, download_dir=str(beta_home))
    if kind == "explicit":
        runner.indicator_dirs = {"A": str(outside / "new")}
    elif kind == "folder_link":
        (beta_home / "linked").symlink_to(outside, target_is_directory=True)
        runner.indicator_folders = {"A": "linked/new"}
    else:
        runner.download_dir = str(outside)
        if kind == "empty_folder":
            runner.indicator_folders = {"A": "///"}
    with pytest.raises(ValueError, match="测试版"):
        runner._indicator_output_dir("A")
    assert not outside.exists()


@pytest.mark.parametrize("daily", [False, True])
def test_beta_storage_state_resolution_rejects_external_leaf(beta_home, tmp_path, daily):
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"keep")
    leaf = beta_home / "storage_state.json"
    leaf.symlink_to(outside)
    download = beta_home / "2026-11" / "2026-11-12" if daily else beta_home
    with pytest.raises(ValueError, match="测试版"):
        TaskRunner({"name": "paths"}, download_dir=str(download))
    assert leaf.is_symlink()
    assert outside.read_bytes() == b"keep"


class StorageContext:
    async def storage_state(self, *, path):
        Path(path).write_text('{"cookies": [], "origins": []}', encoding="utf-8")


@pytest.mark.parametrize("daily", [False, True])
async def test_beta_storage_state_is_readable_in_download_root(beta_home, daily):
    import json

    download = beta_home / "2026-11" / "2026-11-12" if daily else beta_home
    runner = TaskRunner({"name": "paths"}, download_dir=str(download))
    runner._context = StorageContext()
    await runner._save_storage_state()
    assert runner._storage_state_path == beta_home / "storage_state.json"
    assert json.loads(runner._storage_state_path.read_text()) == {"cookies": [], "origins": []}


@pytest.mark.parametrize("escape", ["leaf", "directory", "external"])
async def test_beta_storage_state_rechecks_actual_leaf_before_writing(beta_home, tmp_path, escape):
    runner = TaskRunner({"name": "paths"}, download_dir=str(beta_home))
    runner._context = StorageContext()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "storage_state.json"
    sentinel.write_bytes(b"keep")
    if escape == "leaf":
        leaf = runner._storage_state_path
        leaf.symlink_to(sentinel)
    elif escape == "directory":
        link = beta_home / "linked"
        link.symlink_to(outside, target_is_directory=True)
        runner._storage_state_path = link / "storage_state.json"
    else:
        runner._storage_state_path = sentinel
    await runner._save_storage_state()
    assert sentinel.read_bytes() == b"keep"
    assert sorted(p.name for p in outside.iterdir()) == ["storage_state.json"]
    if escape == "leaf":
        assert leaf.is_symlink()


async def test_beta_station_directory_rejects_symlink_before_mkdir(beta_home, tmp_path):
    runner = TaskRunner({"name": "paths"}, download_dir=str(beta_home))
    outside = tmp_path / "outside"
    link = beta_home / "station"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="测试版"):
        await runner._use_station({"name": "station"}, 0, 2)
    assert not outside.exists()
    assert link.is_symlink()


async def test_beta_indicator_and_station_directories_succeed_in_home(beta_home):
    runner = TaskRunner({"name": "paths"}, download_dir=str(beta_home))
    assert await runner._use_station({"name": "station"}, 0, 2) == "ok"
    runner.indicator_folders = {"A": "reports"}
    assert Path(runner._indicator_output_dir("A")) == beta_home / "station" / "reports"
    assert (beta_home / "station" / "reports").is_dir()
