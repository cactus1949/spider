"""自定义文件名：占位符、校验、优先级和缺省回退。"""

from datetime import date
from pathlib import Path

import pytest

from spider_gd.core.custom_filename import (
    active_templates,
    filename_suffix,
    normalize_custom_filenames,
    overwrite_file,
    render_custom_filename,
    templates_for_indicator,
    validate_custom_filename,
)
from workflow_runner import WorkflowRunner, plan_download_files


def test_placeholder_tokens_match_as_a_whole():
    when = date(2026, 1, 2)
    assert render_custom_filename("{yyyy-MM-dd}", when, ".xlsx") == "2026-01-02.xlsx"
    assert render_custom_filename("{yyyy}", when, ".xlsx") == "2026.xlsx"
    assert render_custom_filename("{yyyy}-{yyyy-MM-dd}", when, ".xlsx") == "2026-2026-01-02.xlsx"
    assert "成对" in validate_custom_filename("电量-{yyyy")
    assert "成对" in validate_custom_filename("电量-yyyy}")
    assert "成对" in validate_custom_filename("{yyyy{MM}}")
    assert "不认识" in validate_custom_filename("电量-{week}")


def test_filename_edges():
    assert "保留名" in validate_custom_filename("lpt9")
    assert "保留名" in validate_custom_filename("AUX.{yyyy}")
    assert "空格或点" in validate_custom_filename("电量.")
    assert "空格或点" in validate_custom_filename(" 电量")
    assert "不能包含" in validate_custom_filename("电量\n日前")
    assert validate_custom_filename("a" * 180) == ""
    assert validate_custom_filename("a" * 181) == "文件名过长"


def test_placeholders_render_with_the_business_date():
    when = date(2026, 11, 12)
    assert render_custom_filename("现货分时分类型出清电量-日前-{yyyyMMdd}", when, ".xlsx") == (
        "现货分时分类型出清电量-日前-20261112.xlsx"
    )
    assert render_custom_filename("{yyyy}-{MM}-{dd}", when, ".csv") == "2026-11-12.csv"
    assert render_custom_filename("{yyyyMM}-{yyyy-MM}-{yyyy-MM-dd}", when, ".xlsx") == (
        "202611-2026-11-2026-11-12.xlsx"
    )


def test_validation_rejects_bad_names():
    assert validate_custom_filename("  ") == "文件名不能为空"
    assert "扩展名" in validate_custom_filename("电量.xlsx")
    assert "保留名" in validate_custom_filename("CON")
    assert "保留名" in validate_custom_filename("com1")
    assert "空格或点" in validate_custom_filename("电量 ")
    assert "空格或点" in validate_custom_filename("电量.")
    assert "不能包含" in validate_custom_filename("电量/日前")
    assert "不能包含" in validate_custom_filename('电量:1')
    assert "成对" in validate_custom_filename("电量-{yyyyMMdd")
    assert "成对" in validate_custom_filename("电量-{yyyyMMdd}}")
    assert "不认识" in validate_custom_filename("电量-{date}")
    assert "不认识" in validate_custom_filename("电量-{}")
    assert validate_custom_filename("电量-{yyyyMMdd}") == ""


def test_missing_or_disabled_custom_name_falls_back():
    assert normalize_custom_filenames(None) == {}
    assert normalize_custom_filenames("not-json") == {}
    assert active_templates({"日前::s0": {"enabled": False, "template": "a-{yyyy}"}}) == {}
    assert active_templates({"日前::s0": {"enabled": True, "template": "a.xlsx"}}) == {}
    assert templates_for_indicator(
        {"实时::s1": {"enabled": True, "template": "实时-{yyyyMMdd}"}},
        "日前",
    ) == {}


def test_custom_name_outranks_yaml_and_missing_keeps_yaml(tmp_path: Path):
    when = date(2026, 11, 12)
    custom = WorkflowRunner(
        {"name": "n", "steps": []},
        download_dir=str(tmp_path),
        target_date=when,
        custom_filenames={"s0": "现货-{yyyyMMdd}"},
    )
    custom._executing_step_index = 0
    renamed = custom._export_path("原名_{date}.xlsx", "站点下载.xlsx")
    assert renamed.name == "现货-20261112.xlsx"
    assert custom._overwrite_current is True

    plain = WorkflowRunner(
        {"name": "n", "steps": []},
        download_dir=str(tmp_path / "plain"),
        target_date=when,
    )
    plain._executing_step_index = 0
    kept = plain._export_path("原名_{date}.xlsx", "站点下载.xlsx")
    assert kept.name == "原名_2026-11-12.xlsx"
    assert plain._overwrite_current is False
    assert filename_suffix("原名_{date}.xlsx") == ".xlsx"


def test_export_path_uses_each_business_day(tmp_path: Path):
    runner = WorkflowRunner(
        {"name": "n", "steps": []},
        download_dir=str(tmp_path),
        custom_filenames={"s0": "日前-{yyyyMMdd}"},
    )
    runner._executing_step_index = 0
    runner.target_date = date(2026, 11, 11)
    first = runner._export_path("原名_{date}.xlsx", "站点.xlsx")
    runner.target_date = date(2026, 11, 12)
    second = runner._export_path("原名_{date}.xlsx", "站点.xlsx")
    assert first.name == "日前-20261111.xlsx"
    assert second.name == "日前-20261112.xlsx"
    assert first.parent == second.parent == tmp_path


def test_each_catchup_day_uses_its_own_date(tmp_path: Path):
    files = plan_download_files(
        {"steps": [{"action": "repeat", "steps": [
            {"action": "export", "save_as": "原名_{date}.xlsx", "name": "日前"},
        ]}]},
        indicator="现货",
        download_dir=tmp_path,
        repeat_dates=[date(2026, 11, 11), date(2026, 11, 12)],
        custom_filenames={"s0.0": "日前-{yyyyMMdd}"},
        today=date(2026, 11, 12),
    )
    assert [item["name"] for item in files] == ["日前-20261111.xlsx", "日前-20261112.xlsx"]


def test_catchup_inherits_task_custom_names_until_this_run_overrides_them():
    from task_runner import apply_catchup_storage

    tasks = [{
        "indicators": ["日前"],
        "enabled": True,
        "created_at": 1,
        "custom_filenames": {
            "日前::电量": {"enabled": True, "template": "电量-{yyyyMMdd}"},
        },
    }]
    inherited = apply_catchup_storage({"indicators": ["日前"]}, tasks)
    assert inherited["custom_filenames"]["日前::电量"]["template"] == "电量-{yyyyMMdd}"
    replaced = apply_catchup_storage(
        {"indicators": ["日前"]},
        tasks,
        storage_overridden=True,
        path_structure="",
        custom_filenames={},
    )
    assert replaced["custom_filenames"] == {}


def test_duplicate_custom_name_overwrites_only_the_target_file(tmp_path: Path):
    dest = tmp_path / "日前-20261112.xlsx"
    sibling = tmp_path / "其他.xlsx"
    folder = tmp_path / "目录.xlsx"
    sibling.write_bytes(b"keep")
    folder.mkdir()
    (folder / "inside.txt").write_text("stay", encoding="utf-8")
    first = tmp_path / "first.tmp"
    second = tmp_path / "second.tmp"
    first.write_bytes(b"old")
    second.write_bytes(b"new")
    overwrite_file(first, dest)
    overwrite_file(second, dest)
    assert dest.read_bytes() == b"new"
    assert sibling.read_bytes() == b"keep"
    assert (folder / "inside.txt").read_text(encoding="utf-8") == "stay"
    assert not first.exists()
    assert not second.exists()
    assert list(tmp_path.glob("_versions/*")) == []
    missing = tmp_path / "missing.tmp"
    with pytest.raises(FileNotFoundError):
        overwrite_file(missing, dest)
    assert dest.read_bytes() == b"new"
    with pytest.raises(IsADirectoryError):
        overwrite_file(sibling, folder)
    assert (folder / "inside.txt").exists()


def test_old_task_database_gains_empty_custom_names(tmp_path: Path):
    import sqlite3

    from spider_gd.infrastructure.storage.database import TaskStateStore

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE task_configs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            login TEXT NOT NULL DEFAULT '',
            indicators TEXT NOT NULL DEFAULT '[]',
            repeat TEXT NOT NULL DEFAULT 'daily',
            time TEXT NOT NULL DEFAULT '09:00',
            weekdays TEXT NOT NULL DEFAULT '[]',
            backfill_days INTEGER NOT NULL DEFAULT 1,
            cover_days INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            remark TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO task_configs (id, name, indicators, created_at, updated_at) "
        "VALUES ('old', '旧任务', '[\"日前\"]', 1, 1)"
    )
    conn.commit()
    conn.close()

    store = TaskStateStore(db_path)
    loaded = store.get_task_config("old")
    assert loaded is not None
    assert loaded["name"] == "旧任务"
    assert loaded["indicators"] == ["日前"]
    assert loaded["custom_filenames"] == {}
    store.close()

    again = TaskStateStore(db_path)
    assert again.get_task_config("old")["custom_filenames"] == {}
    again.close()
