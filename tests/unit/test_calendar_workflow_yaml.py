"""交易日历工作流：固定入口、月视图和 CSV 文件名，避免选择器回退成 hash。"""

from __future__ import annotations

from pathlib import Path
from datetime import date, timedelta

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / "workflows" / "交易日历.yml"


def test_trading_calendar_workflow_shape():
    cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    assert cfg["name"] == "交易日历"
    assert cfg["type"] == "indicator"
    assert cfg["needs_login"] is False
    assert cfg["date_window"] == {"start": 0, "end": 0}

    actions = [step["action"] for step in cfg["steps"]]
    assert actions == [
        "navigate", "click", "wait", "wait", "click", "wait", "wait", "extract_calendar",
    ]
    assert cfg["steps"][1]["selector"] == 'text="中长期交易"'
    assert cfg["steps"][1]["expect"] == "new_tab"
    assert cfg["steps"][2]["selector"] == ".calendar-box"
    assert cfg["steps"][4]["selector"] == '.calendar-box .el-radio-button:has-text("月")'
    assert cfg["steps"][5]["selector"] == ".calendar-box:has(.fc-dayGridMonth-view)"
    assert cfg["steps"][-1]["selector"] == ".calendar-box:has(.fc-dayGridMonth-view)"
    assert cfg["steps"][-1]["save_as"] == "交易日历{month_iso}.csv"
    assert cfg["steps"][-1]["include_empty_days"] is True


def test_selectors_do_not_use_css_module_hash():
    cfg = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    selectors = [str(step.get("selector", "")) for step in cfg["steps"]]
    assert selectors
    assert all("___" not in selector for selector in selectors)


def test_trading_calendar_week_detail_workflow():
    path = WORKFLOW.parent / "交易日历-周明细.yml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert cfg["name"] == "交易日历-周明细"
    # 配置按月、执行按周：界面只选月份，运行时页内自动翻周。
    assert cfg["date_kind"] == "month"
    assert cfg["date_window"] == {"start": 0, "end": 0}
    actions = [step["action"] for step in cfg["steps"]]
    assert actions == [
        "navigate", "click", "wait", "wait", "click", "wait", "wait", "extract_calendar_month_weeks",
    ]
    assert cfg["steps"][2]["selector"] == ".calendar-box"
    assert cfg["steps"][4]["selector"] == '.calendar-box .el-radio-button:has-text("周")'
    assert cfg["steps"][5]["selector"] == ".calendar-box:has(.fc-dayGridWeek-view)"
    extract = cfg["steps"][-1]
    assert extract["selector"] == ".calendar-box:has(.fc-dayGridWeek-view)"
    assert extract["include_other"] is True
    assert extract["include_empty_days"] is False
    assert extract["merge"] is True
    assert extract["save_as"] == "交易日历明细{month_iso}.csv"


@pytest.mark.asyncio
async def test_week_navigation_already_on_target_week_does_nothing(tmp_path):
    from workflow_runner import WorkflowRunner

    page = _WeekPickerFakePage(shown_monday=date(2026, 10, 26))
    runner = WorkflowRunner({"name": "周"}, download_dir=str(tmp_path), target_date="2026-10-28")
    runner.pages = [page]
    _stub_picker_clicks(runner, page)
    await runner._do_select_calendar_week({"action": "select_calendar_week"})
    assert page.clicks == []
    assert page.shown_monday == date(2026, 10, 26)


@pytest.mark.asyncio
async def test_week_navigation_picks_day_via_week_picker(tmp_path):
    """站点没有 fc-prev/next 按钮，只能通过 .date-box 周选择器翻周。"""
    from workflow_runner import WorkflowRunner

    page = _WeekPickerFakePage(shown_monday=date(2026, 10, 26), panel_month=(2026, 10))
    runner = WorkflowRunner({"name": "周"}, download_dir=str(tmp_path), target_date="2026-10-19")
    runner.pages = [page]
    _stub_picker_clicks(runner, page)
    await runner._do_select_calendar_week({"action": "select_calendar_week"})
    assert page.shown_monday == date(2026, 10, 19)
    # 同月翻周：开面板 → 直接点目标日，不需要翻月。
    assert [kind for kind, _ in page.clicks] == ["input", "day"]
    assert page.clicks[-1][1] == 19


@pytest.mark.asyncio
async def test_week_navigation_crosses_month_backward(tmp_path):
    from workflow_runner import WorkflowRunner

    page = _WeekPickerFakePage(shown_monday=date(2026, 10, 26), panel_month=(2026, 10))
    runner = WorkflowRunner({"name": "周"}, download_dir=str(tmp_path), target_date="2026-09-28")
    runner.pages = [page]
    _stub_picker_clicks(runner, page)
    await runner._do_select_calendar_week({"action": "select_calendar_week"})
    assert page.shown_monday == date(2026, 9, 28)
    assert [kind for kind, _ in page.clicks] == ["input", "prev-month", "day"]
    assert page.clicks[-1][1] == 28


@pytest.mark.asyncio
async def test_week_navigation_crosses_month_forward(tmp_path):
    from workflow_runner import WorkflowRunner

    page = _WeekPickerFakePage(shown_monday=date(2026, 10, 26), panel_month=(2026, 10))
    runner = WorkflowRunner({"name": "周"}, download_dir=str(tmp_path), target_date="2026-11-04")
    runner.pages = [page]
    _stub_picker_clicks(runner, page)
    await runner._do_select_calendar_week({"action": "select_calendar_week"})
    assert page.shown_monday == date(2026, 11, 2)
    assert [kind for kind, _ in page.clicks] == ["input", "next-month", "day"]
    assert page.clicks[-1][1] == 2


class _WeekPickerFakePage:
    """ modeled on 2026-10-31 现场 HTML：无 fc 工具栏，周选择器是 el-date-editor--week。

    面板日期格索引 = 日号 - 1（只建模当月 available 格，足够验证编排逻辑）。
    locator / click 一律断言失败：翻周必须走 _low_profile_click，不得退回 locator。
    """

    def __init__(self, shown_monday: date, panel_month: tuple[int, int] | None = None):
        self.shown_monday = shown_monday
        self.panel_month = panel_month or (shown_monday.year, shown_monday.month)
        self.panel_open = False
        self.clicks: list[tuple[str, object]] = []
        self.selectors: list[str] = []
        self.empty_weeks: set[date] = set()

    def locator(self, *_args, **_kwargs):
        raise AssertionError("翻周不得走 locator 路径")

    async def click(self, *_args, **_kwargs):
        raise AssertionError("翻周不得走 page.click 路径")

    async def evaluate(self, script, arg=None):
        if "el-date-picker__header-label" in script:
            if not self.panel_open:
                return None
            year, month = self.panel_month
            day = int(arg["day"])
            import calendar as _cal

            days = _cal.monthrange(year, month)[1]
            return {
                "year": year,
                "month": month,
                "dayIndex": day - 1 if 1 <= day <= days else -1,
                "prevIndex": 0,
                "nextIndex": 0,
            }
        if "fc-timeGridDay-view" in script:
            # _CALENDAR_EXTRACT_JS：每周造一个事件（周一+3 天）。
            # 注意要先于 fc-dayGridWeek-view 判断：提取脚本里也含周视图类名。
            if self.shown_monday in self.empty_weeks:
                return {"view": "周", "month": self.shown_monday.strftime("%Y-%m"), "rows": []}
            day = self.shown_monday + timedelta(days=3)
            return {"view": "周", "month": self.shown_monday.strftime("%Y-%m"),
                    "rows": [{"date": day.isoformat(), "event": f"交易{day.isoformat()}"}]}
        if "fc-dayGridWeek-view" in script:
            return self.shown_monday.isoformat()
        raise AssertionError(f"未预期的 evaluate 脚本: {script[:60]!r}")

    async def wait_for_function(self, script, *, arg, timeout):
        assert self.shown_monday.isoformat() == arg["expected"], "周视图未切到目标周"

    def apply_click(self, selector: str, index):
        self.selectors.append(selector)
        if ".date-box" in selector:
            self.clicks.append(("input", None))
            self.panel_open = True
        elif "el-icon-arrow-left" in selector or "el-icon-arrow-right" in selector:
            year, month = self.panel_month
            delta = -1 if "arrow-left" in selector else 1
            month += delta
            if month < 1:
                year, month = year - 1, 12
            elif month > 12:
                year, month = year + 1, 1
            self.panel_month = (year, month)
            self.clicks.append(("prev-month" if delta < 0 else "next-month", None))
        elif ".el-date-table td" in selector:
            assert self.panel_open, "面板未打开就点日期格"
            year, month = self.panel_month
            day = int(index) + 1
            picked = date(year, month, day)
            self.shown_monday = picked - timedelta(days=picked.weekday())
            self.panel_open = False
            self.clicks.append(("day", day))
        else:
            raise AssertionError(f"未预期的点击选择器: {selector}")


def _stub_picker_clicks(runner, page):
    async def fake_click(selector, *, timeout=3000, index=None):
        page.apply_click(selector, index)
        return True

    runner._low_profile_click = fake_click


@pytest.mark.asyncio
async def test_month_weeks_loop_reads_every_week_of_month(tmp_path):
    """按月选、按周执行：一次运行内翻遍目标月的所有周并合并进月文件。"""
    from workflow_runner import WorkflowRunner

    page = _WeekPickerFakePage(shown_monday=date(2026, 10, 26), panel_month=(2026, 10))
    runner = WorkflowRunner(
        {"name": "周明细"}, download_dir=str(tmp_path),
        target_date="2026-10-01", query_month="2026-10",
    )
    runner.pages = [page]
    _stub_picker_clicks(runner, page)
    await runner._do_extract_calendar_month_weeks({
        "action": "extract_calendar_month_weeks",
        "selector": ".calendar-box:has(.fc-dayGridWeek-view)",
        "save_as": "交易日历明细{month_iso}.csv",
        "include_other": True, "include_empty_days": False, "merge": True,
    })
    # 2026-10 相交 5 周：09-28 / 10-05 / 10-12 / 10-19 / 10-26。
    day_clicks = [value for kind, value in page.clicks if kind == "day"]
    assert day_clicks == [28, 5, 12, 19, 26]
    assert any("fc-dayGridWeek-view" in selector and ".date-box" in selector for selector in page.selectors)
    text = (tmp_path / "交易日历明细2026-10.csv").read_text(encoding="utf-8-sig")
    for day in (1, 8, 15, 22, 29):
        assert f"交易2026-10-{day:02d}" in text


@pytest.mark.asyncio
async def test_month_weeks_loop_tolerates_empty_week(tmp_path):
    from workflow_runner import WorkflowRunner

    page = _WeekPickerFakePage(shown_monday=date(2026, 10, 26), panel_month=(2026, 10))
    page.empty_weeks = {date(2026, 10, 12)}
    runner = WorkflowRunner(
        {"name": "周明细"}, download_dir=str(tmp_path),
        target_date="2026-10-01", query_month="2026-10",
    )
    runner.pages = [page]
    _stub_picker_clicks(runner, page)
    await runner._do_extract_calendar_month_weeks({
        "action": "extract_calendar_month_weeks",
        "save_as": "交易日历明细{month_iso}.csv",
        "include_other": True, "include_empty_days": False, "merge": True,
    })
    text = (tmp_path / "交易日历明细2026-10.csv").read_text(encoding="utf-8-sig")
    assert "交易2026-10-15" not in text
    assert "交易2026-10-22" in text
    assert len([kind for kind, _ in page.clicks if kind == "day"]) == 5


@pytest.mark.asyncio
async def test_week_crossing_month_saves_both_months(tmp_path):
    from workflow_runner import WorkflowRunner

    class Page:
        async def evaluate(self, _script, _options):
            return {"view": "周", "month": "2026-10", "rows": [
                {"date": "2026-10-30", "event": "十月交易"},
                {"date": "2026-11-01", "event": "十一月交易"},
            ]}

    runner = WorkflowRunner({"name": "周"}, download_dir=str(tmp_path), target_date="2026-10-26")
    runner.pages = [Page()]
    await runner._do_extract_calendar({
        "action": "extract_calendar", "month_mode": "week",
        "save_as": "交易日历明细{month_iso}.csv", "include_empty_days": False,
    })
    assert (tmp_path / "交易日历明细2026-10.csv").exists()
    assert (tmp_path / "交易日历明细2026-11.csv").exists()
    assert "十一月交易" not in (tmp_path / "交易日历明细2026-10.csv").read_text(encoding="utf-8-sig")
    assert "十月交易" not in (tmp_path / "交易日历明细2026-11.csv").read_text(encoding="utf-8-sig")
