"""按导出配范围 / 补数据按导出选日期。"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from spider_gd.core.dates import (
    apply_date_profile,
    normalize_output_dates,
    plan_repeat_rounds,
    read_output_windows,
    set_output_windows,
    step_output_dates,
)
from task_runner import build_catchup_config, plan_workflow_runs
from workflow_runner import WorkflowRunner

D1 = date(2026, 10, 29)  # D-1
D2 = date(2026, 10, 28)  # D-2
REF = date(2026, 10, 30)

# 现货页的一个日期循环：负荷 / 日前 / 实时 三个导出共用一次选日期
SPOT_BLOCK = [
    {"action": "pick_date", "value": "{target}"},                 # 0
    {"action": "export", "save_as": "负荷.xlsx"},                  # 1
    {"action": "click", "selector": "日前"},                       # 2
    {"action": "export", "save_as": "出清.xlsx", "name": "日前"},  # 3
    {"action": "click", "selector": "实时"},                       # 4
    {"action": "export", "save_as": "出清.xlsx", "name": "实时"},  # 5
    {"action": "wait", "ms": 1000},                               # 6
]


@pytest.mark.unit
def test_rounds_unchanged_when_no_output_has_its_own_dates():
    rounds = plan_repeat_rounds(SPOT_BLOCK, "s3", [D2, D1], {})
    assert rounds == [(D2, list(range(7))), (D1, list(range(7)))]


@pytest.mark.unit
def test_rounds_replay_from_pick_date_to_the_export_and_skip_other_exports():
    rounds = plan_repeat_rounds(SPOT_BLOCK, "s3", [D1], {"s3.3": [D2, D1]})
    # D-2 只有日前：选日期 → 点日前 → 导出日前，后面的点实时不跑
    assert rounds[0] == (D2, [0, 2, 3])
    # D-1 三个都要：同一天只选一次日期
    assert rounds[1] == (D1, list(range(7)))


@pytest.mark.unit
def test_rounds_skip_output_set_to_no_catchup():
    rounds = plan_repeat_rounds(SPOT_BLOCK, "s3", [D1], {"s3.1": []})
    assert rounds == [(D1, [0, 2, 3, 4, 5, 6])]


@pytest.mark.unit
def test_rounds_with_empty_default_only_run_outputs_that_have_dates():
    rounds = plan_repeat_rounds(SPOT_BLOCK, "s3", [], {"s3.5": [D2]})
    # 共用步骤（点日前）也重放，宁可多点也不漏
    assert rounds == [(D2, [0, 2, 4, 5, 6])]


@pytest.mark.unit
def test_normalize_output_dates_keeps_empty_list_as_no_catchup():
    assert normalize_output_dates({"A::日前": [], "A::实时": ["2026-10-29", "2026-10-28"]}) == {
        "A::日前": [],
        "A::实时": [D2, D1],
    }


SPOT_CFG = {"steps": [
    {"action": "navigate", "url": "https://example"},
    {"action": "pick_date", "value": "yesterday"},
    *SPOT_BLOCK[1:],
]}


@pytest.mark.unit
def test_output_window_overrides_indicator_window_for_that_export():
    planned = plan_workflow_runs(
        ["现货"], configs={"现货": SPOT_CFG},
        indicator_windows={"现货": (-1, -1)},
        output_windows={"现货::日前": (-2, -1)},
        reference_date=REF,
    )
    assert planned == [{
        "indicator": "现货", "mode": "repeat", "dates": [D1],
        "output_dates": {"日前": [D2, D1]},
    }]


@pytest.mark.unit
def test_same_window_as_indicator_is_not_reported_separately():
    planned = plan_workflow_runs(
        ["现货"], configs={"现货": SPOT_CFG},
        indicator_windows={"现货": (-1, -1)},
        output_windows={"现货::日前": (-1, -1), "现货::已删除": (-3, -1)},
        reference_date=REF,
    )
    assert "output_dates" not in planned[0]


@pytest.mark.unit
def test_catchup_output_dates_override_indicator_dates():
    planned = plan_workflow_runs(
        ["现货"], configs={"现货": SPOT_CFG},
        indicator_dates={},
        output_dates={"现货::实时": [D2]},
        reference_date=REF,
    )
    # 指标默认「不补」，只补实时
    assert planned == [{
        "indicator": "现货", "mode": "repeat", "dates": [],
        "output_dates": {"实时": [D2]},
    }]


@pytest.mark.unit
def test_output_windows_are_ignored_by_catchup_dates():
    planned = plan_workflow_runs(
        ["现货"], configs={"现货": SPOT_CFG},
        indicator_dates={"现货": ["2026-10-01"]},
        output_windows={"现货::日前": (-2, -1)},
        reference_date=REF,
    )
    assert planned[0]["dates"] == [date(2026, 10, 1)]
    assert "output_dates" not in planned[0]


@pytest.mark.unit
def test_workflow_without_relative_pick_date_still_replays_each_day():
    # save_as 里带点号的文件名不能被当成循环里的输出
    cfg = {"steps": [{"action": "export", "save_as": "x.xlsx"}]}
    planned = plan_workflow_runs(
        ["A"], configs={"A": cfg}, indicator_windows={"A": (-2, -1)}, reference_date=REF,
    )
    assert [item["mode"] for item in planned] == ["replay", "replay"]


@pytest.mark.unit
def test_build_catchup_config_accepts_output_dates_only():
    cfg = build_catchup_config(
        login="", indicators=["现货"],
        indicator_dates={}, output_dates={"现货::实时": ["2026-10-28"], "现货::日前": []},
    )
    assert cfg["output_dates"] == {"现货::实时": ["2026-10-28"], "现货::日前": []}
    with pytest.raises(ValueError, match="现货"):
        build_catchup_config(login="", indicators=["现货"], output_dates={"现货::日前": []})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeat_runs_each_export_on_its_own_dates(tmp_path):
    runner = WorkflowRunner(
        {"name": "x", "steps": []}, download_dir=str(tmp_path),
        repeat_dates=[D1], output_repeat_dates={"s0.3": [D2, D1]},
    )
    runner._executing_step_index = 0
    seen: list[tuple[str, str]] = []

    async def record(step):
        seen.append((runner.target_date.isoformat(), step.get("selector") or step.get("save_as") or step["action"]))

    runner._exec_step = record
    await runner._do_repeat({"action": "repeat", "steps": SPOT_BLOCK})
    assert seen == [
        ("2026-10-28", "pick_date"), ("2026-10-28", "日前"), ("2026-10-28", "出清.xlsx"),
        ("2026-10-29", "pick_date"), ("2026-10-29", "负荷.xlsx"), ("2026-10-29", "日前"),
        ("2026-10-29", "出清.xlsx"), ("2026-10-29", "实时"), ("2026-10-29", "出清.xlsx"),
        ("2026-10-29", "wait"),
    ]


# ---- 指标里给单个导出写默认范围（导出步骤上的 start/end）----

SPOT_LOOP_CFG = {"date_kind": "day", "steps": [
    {"action": "navigate", "url": "https://example"},
    {"action": "repeat", "start": -1, "end": -1, "steps": [
        {"action": "pick_date", "value": "{target}"},
        {"action": "export", "save_as": "负荷.xlsx"},
        {"action": "click", "selector": "日前"},
        {"action": "export", "save_as": "出清.xlsx", "name": "日前", "start": -2, "end": -1},
        {"action": "click", "selector": "实时"},
        {"action": "export", "save_as": "出清.xlsx", "name": "实时"},
    ]},
]}


@pytest.mark.unit
def test_step_window_on_export_gives_its_own_dates():
    nested = SPOT_LOOP_CFG["steps"][1]["steps"]
    assert step_output_dates(nested, "s1", REF) == {"s1.3": [D2, D1]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_run_uses_export_step_window(tmp_path):
    runner = WorkflowRunner({"name": "x", "steps": []}, download_dir=str(tmp_path))
    runner._executing_step_index = 1
    seen: list[tuple[str, str]] = []

    async def record(step):
        seen.append((runner.target_date.isoformat(), step.get("selector") or step.get("name") or step["action"]))

    runner._exec_step = record
    with patch("workflow_runner.date") as fake_date:
        fake_date.today.return_value = REF
        await runner._do_repeat(SPOT_LOOP_CFG["steps"][1])
    assert seen[:3] == [("2026-10-28", "pick_date"), ("2026-10-28", "日前"), ("2026-10-28", "日前")]
    assert ("2026-10-29", "实时") in seen


@pytest.mark.unit
@pytest.mark.asyncio
async def test_task_dates_ignore_export_step_window(tmp_path):
    # 任务传了日期，导出步骤上的范围已经在排计划时算过，不再重复套用
    runner = WorkflowRunner({"name": "x", "steps": []}, download_dir=str(tmp_path), repeat_dates=[D1])
    runner._executing_step_index = 1
    days: set[str] = set()

    async def record(step):
        days.add(runner.target_date.isoformat())

    runner._exec_step = record
    await runner._do_repeat(SPOT_LOOP_CFG["steps"][1])
    assert days == {"2026-10-29"}


@pytest.mark.unit
def test_task_follow_uses_export_step_window():
    planned = plan_workflow_runs(
        ["现货"], configs={"现货": SPOT_LOOP_CFG},
        indicator_windows={"现货": (-1, -1)}, reference_date=REF,
    )
    assert planned[0]["output_dates"] == {"日前": [D2, D1]}


@pytest.mark.unit
def test_task_output_window_beats_export_step_window():
    planned = plan_workflow_runs(
        ["现货"], configs={"现货": SPOT_LOOP_CFG},
        indicator_windows={"现货": (-1, -1)},
        output_windows={"现货::日前": (-1, -1)}, reference_date=REF,
    )
    assert "output_dates" not in planned[0]


@pytest.mark.unit
def test_catchup_follow_ignores_export_step_window():
    planned = plan_workflow_runs(
        ["现货"], configs={"现货": SPOT_LOOP_CFG},
        indicator_dates={"现货": ["2026-10-01"]}, reference_date=REF,
    )
    assert "output_dates" not in planned[0]


@pytest.mark.unit
def test_read_and_set_output_windows_round_trip():
    assert read_output_windows(SPOT_LOOP_CFG) == {"日前": (-2, -1)}
    updated = set_output_windows(SPOT_LOOP_CFG, {"日前": None, "实时": (-3, -2), "已删除": (-1, -1)})
    assert read_output_windows(updated) == {"实时": (-3, -2)}
    assert "start" not in updated["steps"][1]["steps"][3]
    # 原配置不被改动
    assert read_output_windows(SPOT_LOOP_CFG) == {"日前": (-2, -1)}


@pytest.mark.unit
def test_switching_to_month_drops_export_windows():
    updated = apply_date_profile(SPOT_LOOP_CFG, "month", -1, -1)
    exports = [step for step in updated["steps"] if step.get("action") == "export"]
    assert exports and all("start" not in step and "end" not in step for step in exports)


@pytest.mark.unit
def test_day_profile_keeps_export_windows():
    updated = apply_date_profile(SPOT_LOOP_CFG, "day", -3, -1)
    assert read_output_windows(updated) == {"日前": (-2, -1)}
