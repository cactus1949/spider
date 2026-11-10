"""网关拒绝的交易日历不能记成空周；已登录的门户首页不必再点「登 录」。"""

from __future__ import annotations

import pytest
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowPaused, WorkflowRunner


def _gateway() -> dict:
    return {
        "status": 400,
        "resource_type": "xhr",
        "content_type": "text/html",
        "body_size": 6,
        "body_blank": True,
        "url": (
            "https://pm.gd.csg.cn/pfjy/qctc-pm-trade-zcq-out-xlfb/"
            "tradeCalendarOut/findTradeCalendar/"
        ),
        "page_url": "https://pm.gd.csg.cn/pfjy/stc/index",
    }


class _CalendarPage:
    def __init__(self) -> None:
        self.url = "https://pm.gd.csg.cn/pfjy/stc/index"

    async def evaluate(self, _script, arg=None):
        if isinstance(arg, dict) and "selector" in arg:
            return {
                "view": "周",
                "month": "2026-09",
                "rows": [{
                    "date": "2026-09-01",
                    "event": "",
                    "time_range": "",
                    "apply_time": "",
                    "event_type": "",
                    "status": "",
                }],
            }
        raise AssertionError("unexpected evaluate")


def _calendar_runner(tmp_path) -> tuple[WorkflowRunner, list[dict]]:
    events: list[dict] = []
    runner = WorkflowRunner(
        {"name": "交易日历-周明细", "steps": []},
        download_dir=str(tmp_path),
        target_date="2026-09-01",
    )
    runner.pages = [_CalendarPage()]
    runner._on_event = events.append
    return runner, events


@pytest.mark.unit
async def test_gateway_400_is_not_recorded_as_an_empty_calendar_week(tmp_path):
    runner, events = _calendar_runner(tmp_path)
    runner._page_errors.append(_gateway())
    runner._calendar_watch_baseline = 0

    with pytest.raises(WorkflowPaused, match="不能记成没有交易事件"):
        await runner._do_extract_calendar(
            {
                "selector": ".calendar-box:has(.fc-dayGridWeek-view)",
                "save_as": "交易日历明细{month_iso}.csv",
                "include_empty_days": False,
                "month_mode": "week",
                "merge": True,
            },
            strict=False,
        )

    assert not any(event.get("status") == "empty" for event in events)
    assert runner.page in runner._poisoned_pages


@pytest.mark.unit
async def test_week_without_events_stays_empty_when_the_query_succeeded(tmp_path):
    runner, events = _calendar_runner(tmp_path)
    runner._calendar_watch_baseline = 0

    await runner._do_extract_calendar(
        {
            "selector": ".calendar-box",
            "save_as": "交易日历明细{month_iso}.csv",
            "include_empty_days": False,
            "month_mode": "week",
        },
        strict=False,
    )

    empty = [event for event in events if event.get("status") == "empty"]
    assert len(empty) == 1
    assert empty[0]["reason"] == "没有交易事件"


class _LoggedInHome(DomProbeFakePage):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://pm.gd.csg.cn/portal/#/home"

    def probe_visible(self, selector, index=None):
        return selector in {"text=退出登录", "a:has-text('退出登录')"}


@pytest.mark.unit
async def test_login_click_skips_when_portal_home_already_shows_account(tmp_path):
    runner = WorkflowRunner(
        {
            "name": "登陆流程测试",
            "type": "login",
            "steps": [
                {"action": "click", "selector": "button:has-text('登 录')", "timeout": 200},
                {"action": "fill", "selector": "input[type='password']", "value": "secret"},
            ],
        },
        download_dir=str(tmp_path),
    )
    runner.pages = [_LoggedInHome()]
    events: list[dict] = []

    await runner._execute_steps(events.append, None, shared=True)

    assert runner.failed is False
    assert runner._skip_remaining_login_steps is True
    assert any(event.get("event") == "workflow_done" for event in events)
    assert not any(event.get("event") == "step_error" for event in events)


@pytest.mark.unit
async def test_network_calendar_400_is_not_an_empty_week(tmp_path):
    runner, events = _calendar_runner(tmp_path)
    runner._calendar_payloads.append({**_gateway(), "text": "\r\n\r\n\r\n"})
    runner._calendar_payload_baseline = 0
    runner._calendar_week_clicked = True
    runner._page_errors.append(_gateway())

    with pytest.raises(WorkflowPaused, match="不能记成没有交易事件"):
        await runner._do_extract_calendar(
            {"from_network": True, "month_mode": "week", "include_empty_days": False},
            strict=False,
        )

    assert not any(event.get("status") == "empty" for event in events)


@pytest.mark.unit
async def test_network_calendar_events_do_not_read_the_dom(tmp_path):
    class _NoEval:
        url = "https://pm.gd.csg.cn/pfjy/stc/index"

        async def evaluate(self, *_args, **_kwargs):
            raise AssertionError("周明细不应再执行页面脚本")

    runner = WorkflowRunner(
        {"name": "周"}, download_dir=str(tmp_path), target_date="2026-09-07",
    )
    runner.pages = [_NoEval()]
    runner._calendar_week_clicked = True
    runner._calendar_payload_baseline = 0
    runner._calendar_payloads.append({
        "status": 200,
        "url": "https://pm.gd.csg.cn/pfjy/qctc/findTradeCalendar",
        "text": (
            '{"data":[{"tradeDate":"2026-09-07","tradeName":"周双边",'
            '"applyTime":"09-07 14:00"}]}'
        ),
        "resource_type": "xhr",
        "content_type": "application/json",
    })

    await runner._do_extract_calendar(
        {
            "from_network": True,
            "month_mode": "week",
            "include_empty_days": False,
            "save_as": "交易日历明细{month_iso}.csv",
        },
        strict=False,
    )

    text = (tmp_path / "交易日历明细2026-09.csv").read_text(encoding="utf-8-sig")
    assert "周双边" in text


class _Tab:
    def __init__(self, url: str) -> None:
        self.url = url
        self.front = 0
        self.closed = False

    async def bring_to_front(self) -> None:
        self.front += 1

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed

    async def opener(self):
        return None


@pytest.mark.unit
async def test_closing_the_selected_tab_activates_the_one_left(tmp_path):
    runner = WorkflowRunner({"name": "标签"}, download_dir=str(tmp_path))
    keep = _Tab("https://pm.gd.csg.cn/portal/#/home")
    gone = _Tab("https://pm.gd.csg.cn/pfjy/stc/index")
    runner.pages = [keep, gone]
    runner._current_page_idx = 1

    await runner._do_close_tab({"index": 1})

    assert gone.closed is True
    assert keep.front >= 1
    assert runner.page is keep


@pytest.mark.unit
async def test_new_business_tab_is_brought_to_front(tmp_path):
    runner = WorkflowRunner({"name": "标签"}, download_dir=str(tmp_path))
    portal = _Tab("https://pm.gd.csg.cn/portal/#/home")
    popup = _Tab("https://pm.gd.csg.cn/pfjy/stc/index")

    async def opener():
        return portal

    popup.opener = opener
    runner.pages = [portal]

    await runner._on_new_page(popup)

    assert runner.page is popup
    assert popup.front == 1
