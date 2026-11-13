"""自定义文件名：占位符、校验、优先级和缺省回退。"""

from datetime import date
from pathlib import Path

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


def test_duplicate_custom_name_overwrites_the_old_file(tmp_path: Path):
    dest = tmp_path / "日前-20261112.xlsx"
    first = tmp_path / "first.tmp"
    second = tmp_path / "second.tmp"
    first.write_bytes(b"old")
    second.write_bytes(b"new")
    overwrite_file(first, dest)
    overwrite_file(second, dest)
    assert dest.read_bytes() == b"new"
    assert not first.exists()
    assert not second.exists()
    assert list(tmp_path.glob("_versions/*")) == []
