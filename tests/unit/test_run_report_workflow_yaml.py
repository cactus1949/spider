"""运行日报工作流配置。"""

from __future__ import annotations

from pathlib import Path
from datetime import date

import pytest
import yaml

from spider_gd.core.workflow_outputs import workflow_outputs


@pytest.mark.unit
def test_run_report_workflow_uses_verified_repeat_container():
    path = Path(__file__).resolve().parents[2] / "workflows" / "运行日报.yml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    step = next(s for s in cfg["steps"] if s["action"] == "repeat")
    assert (step["start"], step["end"]) == (-1, 0)
    nested = step["steps"]
    assert next(s for s in nested if s["action"] == "fill")["value"] == "{target}"
    assert next(s for s in nested if s["action"] == "export")["name"] == "日报附件"
    assert cfg["needs_login"] is True

    outputs = workflow_outputs(cfg, "运行日报")
    assert len(outputs) == 1
    assert outputs[0].label == "日报附件"
    assert outputs[0].action == "export"


@pytest.mark.unit
def test_run_report_dates_share_one_browser_run():
    from task_runner import plan_workflow_runs

    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "workflows" / "运行日报.yml").read_text(encoding="utf-8"))
    runs = plan_workflow_runs(
        ["运行日报"], configs={"运行日报": cfg},
        explicit_dates=[date(2026, 10, 29), date(2026, 10, 30)],
    )
    assert len(runs) == 1
    assert runs[0]["mode"] == "repeat"
    assert runs[0]["dates"] == [date(2026, 10, 29), date(2026, 10, 30)]

    own = plan_workflow_runs(
        ["运行日报"], configs={"运行日报": cfg},
        output_dates={"运行日报::日报附件": [date(2026, 10, 28)]},
    )
    assert len(own) == 1
    assert own[0]["dates"] == []


@pytest.mark.unit
def test_download_notice_reports_ui_action_removed():
    """UI 版运行日报动作走原生 locator.click/fill，已删除；只保留 API 版。"""
    from spider_gd.core.workflow_outputs import OUTPUT_ACTIONS
    from workflow_runner import WorkflowRunner

    assert "download_notice_reports_ui" not in OUTPUT_ACTIONS
    assert "download_notice_reports" in OUTPUT_ACTIONS
    for name in ("_do_download_notice_reports_ui", "_query_report_list",
                 "_return_report_list", "_find_report_list_item"):
        assert not hasattr(WorkflowRunner, name)
    assert hasattr(WorkflowRunner, "_do_download_notice_reports")
