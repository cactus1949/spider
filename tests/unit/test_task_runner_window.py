"""TaskRunner date-window expansion tests."""

from __future__ import annotations

from datetime import date

import pytest
from pathlib import Path

import yaml
from spider_gd.core.dates import (
    apply_date_profile,
    expand_w_window,
    pin_step_dates,
    read_date_profile,
    resolve_repeat_dates,
)
from task_runner import (
    apply_target_date_to_workflow,
    build_catchup_config,
    normalize_target_dates,
    output_slot_dirs,
    plan_indicator_runs,
    plan_task_target_dates,
    plan_workflow_runs,
    workflow_is_month_span,
)
from workflow_runner import WorkflowRunner

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_default_task_window_runs_yesterday_only():
    dates = plan_task_target_dates(reference_date="2026-06-30")

    assert [d.isoformat() for d in dates] == ["2026-06-29"]


@pytest.mark.unit
def test_task_window_expands_from_oldest_to_newest():
    dates = plan_task_target_dates(
        backfill_days=3,
        cover_days=2,
        reference_date="2026-06-30",
    )

    assert [d.isoformat() for d in dates] == [
        "2026-06-27",
        "2026-06-28",
        "2026-06-29",
        "2026-06-30",
        "2026-07-01",
    ]


@pytest.mark.unit
def test_apply_target_date_rewrites_relative_pick_date_without_mutating_source():
    cfg = {
        "name": "窗口测试",
        "steps": [
            {"action": "pick_date", "value": "yesterday"},
            {"action": "fill", "selector": "#noticeTitle", "value": "yesterday"},
            {"action": "pick_date_range", "start_value": "-7d", "end_value": "yesterday"},
            {"action": "pick_date", "value": "2026-01-01"},
        ],
    }

    rewritten = apply_target_date_to_workflow(cfg, date(2026, 6, 29))

    assert rewritten["target_date"] == "2026-06-29"
    assert rewritten["steps"][0]["value"] == "2026-06-29"
    assert rewritten["steps"][1]["value"] == "6.28"
    assert rewritten["steps"][2]["start_value"] == "2026-06-22"
    assert rewritten["steps"][2]["end_value"] == "2026-06-28"
    assert rewritten["steps"][3]["value"] == "2026-01-01"
    assert cfg["steps"][0]["value"] == "yesterday"


@pytest.mark.unit
def test_workflow_runner_target_date_controls_save_placeholders(tmp_path):
    runner = WorkflowRunner(
        {"name": "保存路径测试", "steps": []},
        download_dir=str(tmp_path),
        target_date="2026-06-29",
    )

    resolved = runner._resolve_save_path("市场用电信息({date})_{date_compact}.xlsx")

    assert resolved == tmp_path / "市场用电信息(2026-06-29)_20260629.xlsx"


@pytest.mark.parametrize(("base", "first", "last"), [
    ("2026-10-10", "2026-09-01", "2026-09-30"),
    ("2026-01-10", "2025-12-01", "2025-12-31"),
    ("2024-03-10", "2024-02-01", "2024-02-29"),
])
def test_previous_month_dates_are_not_replaced_by_task_window(tmp_path, base, first, last):
    cfg = {"steps": [
        {"action": "pick_date", "value": "previous_month_start"},
        {"action": "pick_date", "value": "previous_month_end"},
    ]}
    target = date.fromisoformat(base)
    config = apply_target_date_to_workflow(cfg, target)
    runner = WorkflowRunner(config, download_dir=str(tmp_path), target_date=target)
    assert runner._parse_date_value(config["steps"][0]["value"]).isoformat() == first
    assert runner._parse_date_value(config["steps"][1]["value"]).isoformat() == last
    assert runner._resolve_save_path("结算({previous_month}).xlsx").name == (
        f"结算({last[:7].replace('-', '')}).xlsx"
    )


def test_normalize_target_dates_sorts_and_dedupes():
    dates = normalize_target_dates(["2026-10-03", "2026-10-01", "2026-10-03", ""])

    assert [item.isoformat() for item in dates] == ["2026-10-01", "2026-10-03"]


@pytest.mark.unit
def test_task_range_loops_date_export_without_replaying_navigation():
    cfg = {"steps": [
        {"action": "navigate", "url": "https://example"},
        {"action": "pick_date", "value": "yesterday"},
        {"action": "export", "save_as": "report_{date}.xlsx"},
    ]}
    planned = plan_workflow_runs(
        ["日报"], configs={"日报": cfg},
        indicator_windows={"日报": (-2, -1)}, reference_date=date(2026, 10, 30),
    )
    assert len(planned) == 1
    assert planned[0]["mode"] == "repeat"
    assert [day.isoformat() for day in planned[0]["dates"]] == ["2026-10-28", "2026-10-29"]


@pytest.mark.unit
def test_week_window_uses_monday_anchors_and_one_run_per_week():
    assert expand_w_window(-1, 0, date(2026, 10, 30)) == [
        date(2026, 10, 19), date(2026, 10, 26),
    ]
    cfg = {"date_kind": "week", "steps": [{"action": "extract_calendar"}]}
    planned = plan_workflow_runs(
        ["交易日历"], configs={"交易日历": cfg},
        indicator_windows={"交易日历": (-1, 0)}, reference_date=date(2026, 10, 30),
    )
    assert [item["mode"] for item in planned] == ["week", "week"]
    assert [item["dates"][0] for item in planned] == [date(2026, 10, 19), date(2026, 10, 26)]


@pytest.mark.unit
def test_week_profile_inserts_calendar_week_navigation():
    cfg = {"steps": [{"action": "click", "selector": "周"}, {"action": "extract_calendar"}]}
    updated = apply_date_profile(cfg, "week", -1, 0)
    assert updated["date_kind"] == "week"
    assert [step["action"] for step in updated["steps"]] == [
        "click", "select_calendar_week", "extract_calendar",
    ]


def test_normalize_target_dates_rejects_bad_text():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        normalize_target_dates(["10月1日"])


def test_plan_indicator_runs_repeats_daily_indicators_and_runs_month_span_once():
    dates = normalize_target_dates(["2026-10-02", "2026-10-01"])
    runs = plan_indicator_runs(
        ["日前", "日清"],
        dates,
        month_span_names={"日清"},
    )

    assert runs == [
        (dates[0], "日前"),
        (dates[1], "日前"),
        (dates[1], "日清"),
    ]


def test_month_span_detects_previous_month_pick_dates_only():
    assert workflow_is_month_span({"steps": [
        {"action": "pick_date", "value": "previous_month_start"},
        {"action": "pick_date", "value": "previous_month_end"},
    ]})
    assert not workflow_is_month_span({"steps": [{"action": "pick_date", "value": "yesterday"}]})


def test_build_catchup_config_uses_explicit_dates_without_schedule():
    cfg = build_catchup_config(
        login="登陆流程测试",
        indicators=[" 日前 ", "日前", "实际"],
        target_dates=["2026-10-25", "2026-10-20"],
    )

    assert cfg["login"] == "登陆流程测试"
    assert cfg["indicators"] == ["日前", "实际"]
    assert cfg["target_dates"] == ["2026-10-20", "2026-10-25"]
    assert "repeat" not in cfg
    assert "backfill_days" not in cfg


def test_build_catchup_config_requires_indicator_and_date():
    with pytest.raises(ValueError, match="指标"):
        build_catchup_config(login="", indicators=[], target_dates=["2026-10-01"])
    with pytest.raises(ValueError, match="日期或月份"):
        build_catchup_config(login="", indicators=["日前"], target_dates=[])


def test_build_catchup_config_accepts_months_without_days():
    cfg = build_catchup_config(
        login="",
        indicators=["日清"],
        target_months=["2026-09", "2026-08"],
    )

    assert cfg["target_months"] == ["2026-08", "2026-09"]
    assert "target_dates" not in cfg


def test_explicit_months_run_once_per_month_and_query_that_month(tmp_path):
    month_cfg = {"date_kind": "month", "steps": [
        {"action": "pick_date", "value": "previous_month_start"},
        {"action": "pick_date", "value": "previous_month_end"},
    ]}
    day_cfg = {"steps": [{"action": "pick_date", "value": "yesterday"}]}
    planned = plan_workflow_runs(
        ["日清", "日前"],
        configs={"日清": month_cfg, "日前": day_cfg},
        explicit_dates=[date(2026, 10, 3), date(2026, 10, 1)],
        explicit_months=[date(2026, 9, 1), date(2026, 8, 15)],
    )

    assert [item["query_month"] for item in planned if item["mode"] == "month"] == ["2026-08", "2026-09"]
    assert [item["dates"][0].isoformat() for item in planned if item["mode"] == "replay"] == [
        "2026-10-01",
        "2026-10-03",
    ]
    runner = WorkflowRunner(
        month_cfg,
        download_dir=str(tmp_path),
        target_date="2026-08-01",
        query_month="2026-08",
    )
    assert runner._parse_date_value("previous_month_start").isoformat() == "2026-08-01"
    assert runner._parse_date_value("previous_month_end").isoformat() == "2026-08-31"
    assert runner._resolve_save_path("结算({previous_month}).xlsx").name == "结算(202608).xlsx"


def test_repeat_default_expands_inclusive_d_window():
    dates = resolve_repeat_dates(
        {"action": "repeat", "start": -2, "end": 0},
        {},
        None,
        date(2026, 10, 29),
    )

    assert [item.isoformat() for item in dates] == ["2026-10-27", "2026-10-28", "2026-10-29"]


def test_repeat_override_replaces_default_window():
    dates = resolve_repeat_dates(
        {"action": "repeat", "start": -2, "end": 0},
        {},
        [date(2026, 10, 1)],
        date(2026, 10, 29),
    )

    assert dates == [date(2026, 10, 1)]


def test_pinned_pick_date_is_the_loop_day_itself(tmp_path):
    pinned = pin_step_dates({"action": "pick_date", "value": "yesterday"}, date(2026, 10, 27))
    runner = WorkflowRunner(
        {"name": "循环日期", "steps": []},
        download_dir=str(tmp_path),
        target_date="2026-10-27",
    )

    assert pinned["value"] == "2026-10-27"
    assert runner._parse_date_value(pinned["value"]).isoformat() == "2026-10-27"
    assert runner._parse_date_value("{target}").isoformat() == "2026-10-27"


def test_apply_date_profile_wraps_each_day_group_and_keeps_page_clicks_outside():
    cfg = yaml.safe_load((_ROOT / "workflows/yml/现货交易-完整采集(含披露中心).yml").read_text(encoding="utf-8"))
    updated = apply_date_profile(cfg, "day", -2, 0)
    repeats = [step for step in updated["steps"] if step.get("action") == "repeat"]

    assert updated["date_window"] == {"start": -2, "end": 0}
    assert len(repeats) == 3
    first = repeats[0]["steps"]
    assert any("日前" in str(step.get("selector") or "") for step in first)
    assert not any("披露" in str(step.get("selector") or "") for step in first)
    assert repeats[1]["steps"][0]["value"] == "{target}"
    assert not any("实际" in str(step.get("selector") or "") for step in repeats[1]["steps"])


def test_apply_date_profile_leaves_month_workflow_unlooped():
    cfg = yaml.safe_load((_ROOT / "workflows/yml/结算管理-日清结果查询及结算单导出.yml").read_text(encoding="utf-8"))
    updated = apply_date_profile(cfg, "month", -2, 0)

    assert updated["date_kind"] == "month"
    assert updated["date_window"] == {"start": -2, "end": 0}
    assert read_date_profile(updated)["start"] == -2
    assert read_date_profile(updated)["end"] == 0
    assert not any(step.get("action") == "repeat" for step in updated["steps"])
    assert workflow_is_month_span(updated)


def test_task_window_overrides_indicator_default_and_explicit_dates_override_both():
    cfg = {
        "date_kind": "day",
        "date_window": {"start": -5, "end": 2},
        "steps": [{"action": "repeat", "start": -5, "end": 2, "steps": [{"action": "pick_date", "value": "{target}"}]}],
    }
    reference = date(2026, 10, 29)
    from_task = plan_workflow_runs(
        ["日前"],
        configs={"日前": cfg},
        indicator_windows={"日前": (-2, 0)},
        reference_date=reference,
    )
    from_dates = plan_workflow_runs(
        ["日前"],
        configs={"日前": cfg},
        explicit_dates=[date(2026, 10, 3), date(2026, 10, 1)],
        indicator_windows={"日前": (-2, 0)},
        reference_date=reference,
    )

    assert from_task[0]["mode"] == "repeat"
    assert [item.isoformat() for item in from_task[0]["dates"]] == ["2026-10-27", "2026-10-28", "2026-10-29"]
    assert [item.isoformat() for item in from_dates[0]["dates"]] == ["2026-10-01", "2026-10-03"]


def test_legacy_task_without_indicator_windows_keeps_backfill_dates():
    cfg = {
        "date_kind": "day",
        "date_window": {"start": -2, "end": 0},
        "steps": [{"action": "repeat", "start": -2, "end": 0, "steps": []}],
    }
    planned = plan_workflow_runs(
        ["日前"],
        configs={"日前": cfg},
        backfill_days=1,
        cover_days=0,
        reference_date=date(2026, 10, 29),
    )

    assert planned[0]["mode"] == "repeat"
    assert [item.isoformat() for item in planned[0]["dates"]] == ["2026-10-28"]


def test_month_window_expands_previous_months_and_crosses_years():
    from spider_gd.core.dates import expand_m_window

    october = date(2026, 10, 29)
    assert [item.isoformat() for item in expand_m_window(-1, -1, october)] == ["2026-09-01"]
    assert [item.isoformat() for item in expand_m_window(-3, -1, october)] == [
        "2026-07-01",
        "2026-08-01",
        "2026-09-01",
    ]
    assert [item.isoformat() for item in expand_m_window(-1, -1, date(2026, 1, 10))] == ["2025-12-01"]


def test_task_month_window_is_separate_from_day_window():
    month_cfg = {"date_kind": "month", "steps": [
        {"action": "pick_date", "value": "previous_month_start"},
    ]}
    day_cfg = {"date_kind": "day", "steps": [{"action": "pick_date", "value": "yesterday"}]}
    planned = plan_workflow_runs(
        ["日清", "日前"],
        configs={"日清": month_cfg, "日前": day_cfg},
        indicator_windows={"日清": (-3, -1), "日前": (-1, -1)},
        backfill_days=5,
        cover_days=2,
        reference_date=date(2026, 10, 29),
    )

    assert [item["query_month"] for item in planned if item["indicator"] == "日清"] == [
        "2026-07",
        "2026-08",
        "2026-09",
    ]
    day_runs = [item for item in planned if item["indicator"] == "日前"]
    assert len(day_runs) == 1
    assert day_runs[0]["dates"] == [date(2026, 10, 28)]


def test_explicit_months_override_saved_month_window():
    planned = plan_workflow_runs(
        ["日清"],
        configs={"日清": {"date_kind": "month", "steps": []}},
        explicit_months=[date(2026, 6, 1)],
        indicator_windows={"日清": (-1, -1)},
        reference_date=date(2026, 10, 29),
    )

    assert planned[0]["query_month"] == "2026-06"


def test_each_indicator_can_catch_up_its_own_periods():
    planned = plan_workflow_runs(
        ["日前", "实际", "日清"],
        configs={
            "日前": {"date_kind": "day", "steps": []},
            "实际": {"date_kind": "day", "steps": [{"action": "repeat", "steps": []}]},
            "日清": {"date_kind": "month", "steps": []},
        },
        explicit_dates=[date(2026, 1, 1)],
        explicit_months=[date(2026, 1, 1)],
        indicator_windows={"日前": (-5, 0), "日清": (-1, -1)},
        indicator_dates={"日前": ["2026-10-03"], "实际": ["2026-10-02", "2026-10-01"]},
        indicator_months={"日清": ["2026-08"]},
        reference_date=date(2026, 10, 29),
    )

    assert planned[0]["indicator"] == "日前"
    assert planned[0]["dates"] == [date(2026, 10, 3)]
    assert planned[1]["mode"] == "repeat"
    assert planned[1]["dates"] == [date(2026, 10, 1), date(2026, 10, 2)]
    assert planned[2]["query_month"] == "2026-08"


def test_build_catchup_config_keeps_each_indicator_period():
    cfg = build_catchup_config(
        login="",
        indicators=["日前", "日清"],
        indicator_dates={"日前": ["2026-10-02", "2026-10-01"]},
        indicator_months={"日清": ["2026-08"]},
    )

    assert cfg["indicator_dates"] == {"日前": ["2026-10-01", "2026-10-02"]}
    assert cfg["indicator_months"] == {"日清": ["2026-08"]}
    assert "target_dates" not in cfg
    with pytest.raises(ValueError, match="日前"):
        build_catchup_config(
            login="",
            indicators=["日前", "日清"],
            indicator_months={"日清": ["2026-08"]},
        )


def test_month_indicator_runs_once_even_when_dates_are_passed():
    cfg = {"date_kind": "month", "steps": [
        {"action": "pick_date", "value": "previous_month_start"},
        {"action": "pick_date", "value": "previous_month_end"},
    ]}
    planned = plan_workflow_runs(
        ["日清"],
        configs={"日清": cfg},
        explicit_dates=[date(2026, 10, 1), date(2026, 10, 3)],
    )

    assert planned == [{"indicator": "日清", "mode": "month", "dates": [date(2026, 10, 3)]}]


def test_output_dirs_follow_outputs_after_steps_are_wrapped_in_repeat():
    cfg = {"steps": [
        {"action": "navigate", "url": "https://example"},
        {"action": "pick_date", "value": "yesterday"},
        {"action": "export", "save_as": "日前_{date}.xlsx"},
        {"action": "export"},
    ]}
    looped = apply_date_profile(cfg, "day")

    dirs = output_slot_dirs(cfg, looped, {"日前_{date}.xlsx": "/日前", "s3": "/原名"}, "日前")

    assert dirs == {"s1.1": "/日前", "s1.2": "/原名"}
