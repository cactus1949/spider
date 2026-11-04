"""工作流产物清单：一个指标多个 export 要全部列出来。"""

from __future__ import annotations

import pytest

from spider_gd.core.workflow_outputs import output_specs_or_fallback, workflow_outputs


@pytest.mark.unit
def test_workflow_outputs_lists_every_export_and_extract():
    cfg = {
        "steps": [
            {"action": "navigate", "url": "https://example"},
            {"action": "export", "selector": "x", "save_as": "负荷({date}).xlsx"},
            {"action": "wait", "ms": 1000},
            {"action": "export", "selector": "y", "save_as": "出清({date}).xlsx"},
            {
                "action": "extract_calendar",
                "selector": ".calendar",
                "save_as": "交易日历{month_iso}.csv",
            },
        ]
    }

    specs = workflow_outputs(cfg, "指标A")

    # 有 save_as 时以文件名模板作为稳定 key；position 才是步骤位置。
    assert [spec.key for spec in specs] == [
        "负荷({date}).xlsx",
        "出清({date}).xlsx",
        "交易日历{month_iso}.csv",
    ]
    assert [spec.position for spec in specs] == ["s1", "s3", "s4"]
    assert all(spec.indicator == "指标A" for spec in specs)
    assert specs[0].label == "负荷({date}).xlsx"


@pytest.mark.unit
def test_output_key_survives_step_insertion():
    before = {"steps": [
        {"action": "export", "save_as": "日前.xlsx"},
        {"action": "export", "save_as": "实时.xlsx"},
    ]}
    after = {"steps": [
        {"action": "wait", "ms": 500},
        {"action": "export", "save_as": "日前.xlsx"},
        {"action": "wait", "ms": 500},
        {"action": "export", "save_as": "实时.xlsx"},
    ]}
    keys_before = [spec.key for spec in workflow_outputs(before, "A")]
    keys_after = [spec.key for spec in workflow_outputs(after, "A")]
    assert keys_before == keys_after == ["日前.xlsx", "实时.xlsx"]


@pytest.mark.unit
def test_save_as_directory_is_not_part_of_key():
    specs = workflow_outputs({"steps": [
        {"action": "export", "save_as": "/出清电量/日前/出清{date}.xlsx"},
    ]}, "A")
    assert specs[0].key == "出清{date}.xlsx"
    assert specs[0].label == "出清{date}.xlsx"


@pytest.mark.unit
def test_duplicate_save_as_falls_back_to_position():
    specs = workflow_outputs({"steps": [
        {"action": "export", "save_as": "同名.xlsx"},
        {"action": "export", "save_as": "同名.xlsx"},
        {"action": "export", "save_as": "独立.xlsx"},
    ]}, "A")
    assert [spec.key for spec in specs] == ["s0", "s1", "独立.xlsx"]
    assert [spec.duplicate for spec in specs] == [True, True, False]


@pytest.mark.unit
def test_step_name_tells_same_save_as_apart():
    specs = workflow_outputs({"steps": [
        {"action": "export", "save_as": "出清{date}.xlsx", "name": "日前出清"},
        {"action": "export", "save_as": "出清{date}.xlsx", "name": "实时出清"},
    ]}, "A")
    assert [spec.key for spec in specs] == ["日前出清", "实时出清"]
    assert [spec.label for spec in specs] == ["日前出清", "实时出清"]
    assert [spec.duplicate for spec in specs] == [False, False]


@pytest.mark.unit
def test_name_wins_over_save_as_and_survives_rename_of_file():
    before = workflow_outputs({"steps": [{"action": "export", "save_as": "旧.xlsx", "name": "日前"}]}, "A")
    after = workflow_outputs({"steps": [{"action": "export", "save_as": "新.xlsx", "name": "日前"}]}, "A")
    assert before[0].key == after[0].key == "日前"


@pytest.mark.unit
def test_duplicate_names_fall_back_to_position():
    specs = workflow_outputs({"steps": [
        {"action": "export", "save_as": "a.xlsx", "name": "同名"},
        {"action": "export", "save_as": "b.xlsx", "name": "同名"},
    ]}, "A")
    assert [spec.key for spec in specs] == ["s0", "s1"]
    assert [spec.duplicate for spec in specs] == [True, True]


@pytest.mark.unit
def test_export_without_save_as_is_still_an_output():
    specs = workflow_outputs({"steps": [{"action": "export", "selector": "button"}]}, "A")
    assert len(specs) == 1
    assert specs[0].key == "s0"
    assert specs[0].label == "网站原名"


@pytest.mark.unit
def test_fallback_for_workflow_without_outputs():
    specs = output_specs_or_fallback({"steps": [{"action": "click"}]}, "A")
    assert len(specs) == 1
    assert specs[0].key == ""
    assert specs[0].label == "A"


@pytest.mark.unit
def test_report_download_step_is_an_output():
    specs = workflow_outputs(
        {"steps": [{"action": "download_notice_reports"}]},
        "运行日报",
    )
    assert len(specs) == 1
    assert specs[0].key == "s0"
    assert specs[0].label == "运行日报附件"
    assert specs[0].action == "download_notice_reports"


@pytest.mark.unit
def test_repeat_nested_exports_are_individual_outputs():
    cfg = {"steps": [{"action": "repeat", "steps": [
        {"action": "pick_date", "value": "{target}"},
        {"action": "export", "save_as": "A({date}).xlsx"},
        {"action": "export", "save_as": "B({date}).xlsx"},
    ]}]}
    specs = workflow_outputs(cfg, "指标")
    assert [spec.key for spec in specs] == ["A({date}).xlsx", "B({date}).xlsx"]
    assert [spec.position for spec in specs] == ["s0.1", "s0.2"]
