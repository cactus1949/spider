"""导出没等到 download 事件时，要写出请求状态、弹窗或空结果，而不是只报 60 秒超时。"""

from __future__ import annotations

import time
from datetime import date

import pytest
from workflow_runner import (
    WorkflowDateExportFailed,
    WorkflowExecutionError,
    WorkflowPaused,
    WorkflowRunner,
    diagnose_export_miss,
)


def _gateway(url: str = (
    "https://pm.gd.csg.cn/pfjs/qctc-pm-jsgl-electricity-month-query/"
    "sbs/calDayStatQuery/exportItemsWithoutCalVersion"
)) -> dict:
    return {
        "status": 400,
        "resource_type": "xhr",
        "content_type": "text/html",
        "body_size": 6,
        "body_blank": True,
        "url": url,
        "page_url": "https://pm.gd.csg.cn/pfjs/stc/qctc-settle-gz/pages/daily_settlement/daily_overview_withoutCalVersion",
    }


def test_empty_dialog_skips_and_gateway_400_is_not_a_plain_timeout():
    assert diagnose_export_miss([], "") is None
    skipped = diagnose_export_miss([], "该日暂无数据，请稍后再试")
    assert skipped["action"] == "skip"
    assert "暂无数据" in skipped["reason"]

    gateway = diagnose_export_miss([_gateway()], "")
    assert gateway["action"] == "gateway"
    assert "HTTP 400" in gateway["reason"]
    assert "请求签名" in gateway["reason"]
    assert "Timeout" not in gateway["reason"]


def test_http0_names_the_browser_failure_instead_of_only_the_download_timeout():
    found = diagnose_export_miss(
        [{
            "status": 0,
            "resource_type": "xhr",
            "content_type": "",
            "url": (
                "https://pm.gd.csg.cn/pfjs/qctc-pm-jsgl-electricity-month-query/"
                "sbs/calDayStatQuery/exportItemsWithoutCalVersion"
            ),
            "page_url": "https://pm.gd.csg.cn/pfjs/stc/daily",
            "failure": "net::ERR_CONNECTION_RESET",
        }],
        "",
        timeout_error='Timeout 60000ms exceeded while waiting for event "download"',
    )

    assert found["action"] == "fail"
    assert "HTTP 0" in found["reason"]
    assert "net::ERR_CONNECTION_RESET" in found["reason"]
    assert "HTTP 400" in found["reason"]


class _HangDownload:
    """下载事件一直不来，直到被取消。"""

    def __init__(self) -> None:
        self.timeout = None
        self.closed = False

    def __call__(self, **kwargs):
        self.timeout = kwargs.get("timeout")
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        return False

    @property
    def value(self):
        import asyncio

        return asyncio.get_running_loop().create_future()


def _runner(tmp_path) -> WorkflowRunner:
    runner = WorkflowRunner({"name": "日清算", "steps": []}, download_dir=str(tmp_path))
    page = _HangDownload()
    # expect_download 是 page 的方法；页面本身用同一个替身挂住下载。
    page.expect_download = page
    runner.pages = [page]

    async def ready(**_kwargs):
        return None

    runner._pause_until_export_ready = ready
    return runner


@pytest.mark.unit
async def test_export_stops_on_gateway_400_without_waiting_out_the_download(tmp_path):
    runner = _runner(tmp_path)

    async def click(_selector, timeout=0):
        runner._page_errors.append(_gateway())
        return True

    runner._low_profile_click = click
    started = time.monotonic()
    with pytest.raises(WorkflowPaused, match="请求签名已失效") as caught:
        await runner._do_export({"selector": "button:has-text('导出')", "timeout": 60000})

    assert time.monotonic() - started < 3
    assert "HTTP 400" in str(caught.value)
    assert runner.page in runner._poisoned_pages


@pytest.mark.unit
async def test_export_http0_fails_fast_and_does_not_block_the_next_attempt(tmp_path):
    runner = _runner(tmp_path)

    async def click(_selector, timeout=0):
        runner._page_errors.append({
            "status": 0,
            "resource_type": "xhr",
            "content_type": "",
            "failure": "net::ERR_ABORTED",
            "url": _gateway()["url"],
            "page_url": _gateway()["page_url"],
        })
        return True

    runner._low_profile_click = click
    started = time.monotonic()
    with pytest.raises(WorkflowDateExportFailed, match="HTTP 0") as caught:
        await runner._do_export({"selector": "#export", "timeout": 60000})

    # 给真正的下载事件留 1 秒，但不能干等 60 秒。
    assert time.monotonic() - started < 3
    assert "net::ERR_ABORTED" in str(caught.value)
    assert runner._unacknowledged_page_errors() == []


@pytest.mark.unit
async def test_empty_result_dialog_skips_the_date(tmp_path):
    runner = _runner(tmp_path)

    async def click(_selector, timeout=0):
        return True

    async def notice():
        return "暂无数据"

    runner._low_profile_click = click
    runner._read_export_notice = notice
    with pytest.raises(Exception, match="没有可导出的数据") as caught:
        await runner._do_export({"selector": "#export", "timeout": 60000})
    assert type(caught.value).__name__ == "WorkflowSkipRepeatRound"


@pytest.mark.unit
async def test_repeat_keeps_going_after_one_date_and_reports_it(tmp_path):
    runner = WorkflowRunner(
        {
            "name": "补数据",
            "steps": [{
                "action": "repeat",
                "steps": [{
                    "action": "export",
                    "selector": "#export",
                    "save_as": "日清算概况_{date}.xlsx",
                }],
            }],
        },
        download_dir=str(tmp_path),
        repeat_dates=["2026-11-08", "2026-11-09", "2026-11-10"],
    )
    seen: list[str] = []

    async def exec_step(_step):
        seen.append(runner.target_date.isoformat())
        if runner.target_date == date(2026, 11, 10):
            raise WorkflowDateExportFailed("导出请求没有收到 HTTP 响应（HTTP 0）")

    runner._exec_step = exec_step
    with pytest.raises(WorkflowExecutionError, match="部分日期导出失败") as caught:
        await runner._do_repeat(runner.steps[0])

    assert seen == ["2026-11-08", "2026-11-09", "2026-11-10"]
    assert "2026-11-10" in str(caught.value)
    assert "HTTP 0" in str(caught.value)
    assert "2026-11-08" not in str(caught.value)
