"""工作流错误状态传播测试。

重点覆盖客户现场最容易误判的路径：
登录后出现数字证书/PIN/证书连接失败弹窗时，不能把工作流或任务标成完成。
"""

from __future__ import annotations

import base64

import pytest
import task_runner as task_runner_module
import workflow_runner as workflow_runner_module
from task_runner import TaskRunner
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowExecutionError, WorkflowRunner

from spider_gd.core.crawler.exceptions import CaptchaError


class _FakeButton:
    async def wait_for(self, **_kwargs):
        return None

    async def click(self):
        return None

    @property
    def first(self):
        return self


class _FakeText:
    def __init__(self, text: str) -> None:
        self._text = text

    async def text_content(self, **_kwargs):
        return self._text

    @property
    def first(self):
        return self


class _FakePage:
    async def evaluate(self, _script, arg):
        selector = arg["selector"]
        if arg["op"] == "visible":
            return selector == ".ant-modal-confirm-btns button"
        if arg["op"] == "texts":
            return [{".ant-modal-confirm-title": "登录失败",
                     ".ant-modal-confirm-content": "数字证书连接失败"}.get(selector, "")]
        raise AssertionError(arg["op"])

    def locator(self, selector: str):
        if selector == ".ant-modal-confirm-btns button":
            return _FakeButton()
        if selector == ".ant-modal-confirm-title":
            return _FakeText("登录失败")
        if selector == ".ant-modal-confirm-content":
            return _FakeText("数字证书连接失败")
        raise AssertionError(f"unexpected selector: {selector}")


class _FillShouldNotRunPage:
    async def fill(self, *_args, **_kwargs):
        raise AssertionError("unresolved placeholder should abort before browser fill")


class _GotoErrorPage:
    async def goto(self, *_args, **_kwargs):
        raise RuntimeError(
            "Page.goto: net::ERR_CONNECTION_CLOSED at https://pm.gd.csg.cn/portal/#/home"
        )


class _VisibleLocator:
    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None

    async def is_visible(self):
        return True


class _AlreadyLoggedInPage:
    def __init__(self) -> None:
        self.clicked: list[str] = []

    async def evaluate(self, _script, keys):
        return [[key, 'mock-token'] for key in keys]

    async def goto(self, *_args, **_kwargs):
        return None

    def locator(self, selector: str):
        if selector == ".el-menu":
            return _VisibleLocator()
        raise AssertionError(f"should not query after login skip: {selector}")

    async def wait_for_selector(self, selector: str, **_kwargs):
        raise AssertionError(f"should skip login button lookup: {selector}")

    async def click(self, selector: str, **_kwargs):
        self.clicked.append(selector)


class _FailingDownloadContext:
    async def __aenter__(self):
        raise TimeoutError("download timeout")

    async def __aexit__(self, *_args):
        return None


class _ExportFailPage:
    def expect_download(self, **_kwargs):
        return _FailingDownloadContext()

    async def click(self, *_args, **_kwargs):
        return None


class _ExportFallbackPage(_ExportFailPage):
    async def evaluate(self, *_args, **_kwargs):
        payload = b'{"data":[{"name":"fallback","value":1}]}'
        return {
            "ok": True,
            "status": 200,
            "statusText": "OK",
            "contentType": "application/json",
            "filename": "",
            "base64": base64.b64encode(payload).decode("ascii"),
        }


_CAPTCHA_DATA_URL = "data:image/png;base64," + base64.b64encode(
    b"not-a-real-image-but-good-enough"
).decode("ascii")


class _CaptchaFailPage(DomProbeFakePage):
    def __init__(self) -> None:
        super().__init__()
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []

    def probe_visible(self, selector: str, index: int | None = None) -> bool:
        return selector in ("img[src^='data:image']", "#validateOnly_verifyCode")

    def probe_attr(self, selector: str, name: str) -> str | None:
        if selector == "img[src^='data:image']" and name == "src":
            return _CAPTCHA_DATA_URL
        return None

    def apply_click(self, selector: str, _index: int | None) -> None:
        self.clicks.append(selector)

    async def screenshot(self, path: str, **_kwargs):
        from pathlib import Path

        Path(path).write_bytes(b"png")


class _LoginNoJumpPage(DomProbeFakePage):
    def __init__(self) -> None:
        super().__init__()
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []

    def probe_visible(self, selector: str, index: int | None = None) -> bool:
        return selector in ("img[src^='data:image']", "#validateOnly_verifyCode")

    def probe_attr(self, selector: str, name: str) -> str | None:
        if selector == "img[src^='data:image']" and name == "src":
            return _CAPTCHA_DATA_URL
        return None

    def apply_click(self, selector: str, _index: int | None) -> None:
        self.clicks.append(selector)


class _AlwaysFailCaptchaSolver:
    def recognize(self, _image_bytes: bytes) -> str:
        raise CaptchaError("识别结果长度异常")


class _AlwaysCodeCaptchaSolver:
    def recognize(self, _image_bytes: bytes) -> str:
        return "abcd"


class _DatePickerPage(DomProbeFakePage):
    """Element UI 日期面板的最小状态机。只通过探测 + 鼠标轨迹驱动。"""

    _MONTH_NAMES = {
        1: "一月", 2: "二月", 3: "三月", 4: "四月",
        5: "五月", 6: "六月", 7: "七月", 8: "八月",
        9: "九月", 10: "十月", 11: "十一月", 12: "十二月",
    }

    def __init__(self) -> None:
        super().__init__()
        self.opened = False
        self.panel = "date"
        self.year = 2026
        self.month = 6
        self.selected = ""
        self.actions: list[str] = []

    def probe_texts(self, selector: str) -> list[str]:
        if selector == ".el-date-picker__header-label":
            return [f"{self.year} 年", self._MONTH_NAMES[self.month]]
        if selector == ".el-year-table .cell":
            return [str(year) for year in range(2020, 2030)]
        return []

    def probe_count(self, selector: str) -> int:
        if selector == ".el-date-picker__header-label":
            return 2
        return 1

    def probe_visible(self, selector: str, index: int | None = None) -> bool:
        sel = selector
        if sel == ".dateBox .el-input__inner":
            return True
        if "shortcut" in sel:
            return False
        if sel == ".el-year-table .cell":
            return self.panel == "year"
        if '.el-year-table .cell:text-is("2025")' in sel:
            return self.panel == "year"
        if sel in (".el-month-table .cell", ".el-month-table td"):
            return self.panel == "month"
        if ".el-month-table .cell:text-is" in sel:
            return self.panel == "month"
        if ".el-date-picker__header-label" in sel:
            return self.opened and self.panel == "date"
        if "td.available" in sel and 'text-is("1")' in sel:
            return self.panel == "date" and self.year == 2025 and self.month == 1
        return False

    def apply_click(self, selector: str, index: int | None) -> None:
        sel = selector
        self.actions.append(f"click:{sel}:{index}")
        if sel == ".dateBox .el-input__inner":
            self.opened = True
            self.panel = "date"
            return
        if ".el-date-picker__header-label" in sel and ("年" in sel or index in (None, 0)):
            self.panel = "year"
            return
        if '.el-year-table .cell:text-is("2025")' in sel:
            self.year = 2025
            self.panel = "month"
            return
        if ".el-month-table .cell:text-is" in sel:
            self.month = 1
            self.panel = "date"
            return
        if "td.available" in sel and 'text-is("1")' in sel:
            self.selected = "2025-01-01"


class _MonthPickerPage(_DatePickerPage):
    def __init__(self) -> None:
        super().__init__()
        self.panel = "month"
        self.year = 2026
        self.month = 10

    def probe_texts(self, selector: str) -> list[str]:
        if selector == ".el-date-picker__header-label":
            return [f"{self.year} 年"]
        return super().probe_texts(selector)

    def probe_visible(self, selector: str, index: int | None = None) -> bool:
        if selector == ".el-date-editor--month input":
            return True
        if selector == ".el-date-picker__header-label":
            return self.opened and self.panel == "month"
        return super().probe_visible(selector, index)

    def apply_click(self, selector: str, index: int | None) -> None:
        if selector == ".el-date-editor--month input":
            self.opened = True
            return
        super().apply_click(selector, index)
        if ".el-month-table .cell:text-is" in selector:
            self.selected = f"{self.year}-{self.month:02d}"


@pytest.mark.unit
async def test_dismiss_modal_report_error_stops_without_false_completion(tmp_path):
    events: list[dict] = []

    async def _continue() -> bool:
        return True

    runner = WorkflowRunner(
        {
            "name": "登录流程测试",
            "steps": [
                {"action": "dismiss_modal", "timeout": 1, "report_error": True},
                {"action": "wait", "ms": 1},
            ],
        },
        download_dir=str(tmp_path),
    )
    runner.pages = [_FakePage()]

    await runner._execute_steps(events.append, _continue, shared=True)

    event_names = [e["event"] for e in events]
    assert "error_pause" in event_names
    assert "step_error" not in event_names
    assert "step_done" not in event_names
    assert "workflow_done" not in event_names
    assert runner.failed is True
    assert "数字证书连接失败" in runner.failure_reason


@pytest.mark.unit
async def test_goto_connection_closed_emits_site_unreachable_without_workflow_done(tmp_path):
    events: list[dict] = []
    runner = WorkflowRunner(
        {
            "name": "登录流程测试",
            "steps": [{"action": "navigate", "url": "https://pm.gd.csg.cn/portal/#/home"}],
        },
        download_dir=str(tmp_path),
    )
    runner.pages = [_GotoErrorPage()]

    await runner._execute_steps(events.append, None, shared=True)

    event_names = [e["event"] for e in events]
    assert "site_unreachable" in event_names
    assert "step_error" in event_names
    assert "aborted" in event_names
    assert "workflow_done" not in event_names
    assert runner.failed is True
    assert "ERR_CONNECTION_CLOSED" in runner.failure_reason
    unreachable = next(e for e in events if e["event"] == "site_unreachable")
    assert "随身 WiFi" in unreachable["message"]
    assert "原始错误" in unreachable["message"]


@pytest.mark.unit
async def test_export_failure_aborts_without_workflow_done(tmp_path):
    events: list[dict] = []
    runner = WorkflowRunner(
        {
            "name": "导出失败测试",
            "steps": [{"action": "export", "selector": "button:has-text('导出')", "timeout": 1}],
        },
        download_dir=str(tmp_path),
    )
    runner.pages = [_ExportFailPage()]

    await runner._execute_steps(events.append, None, shared=True)

    event_names = [e["event"] for e in events]
    assert "step_error" in event_names
    assert "aborted" in event_names
    assert "workflow_done" not in event_names
    assert runner.failed is True
    assert "导出失败" in runner.failure_reason


@pytest.mark.unit
async def test_export_failure_uses_api_fallback_without_workflow_error(tmp_path):
    events: list[dict] = []
    runner = WorkflowRunner(
        {
            "name": "导出降级测试",
            "api_fallback": {"enabled": True, "url": "/fallback/export", "method": "GET"},
            "steps": [
                {
                    "action": "export",
                    "selector": "button:has-text('导出')",
                    "save_as": "fallback.xlsx",
                    "timeout": 1,
                }
            ],
        },
        download_dir=str(tmp_path),
    )
    runner.pages = [_ExportFallbackPage()]

    await runner._execute_steps(events.append, None, shared=True)

    event_names = [e["event"] for e in events]
    assert "api_fallback" in event_names
    assert "step_error" not in event_names
    assert "workflow_done" in event_names
    assert runner.failed is False
    assert list(tmp_path.rglob("fallback.xlsx"))


@pytest.mark.unit
def test_leading_slash_save_as_stays_under_download_dir(tmp_path):
    runner = WorkflowRunner({"name": "保存路径测试", "steps": []}, download_dir=str(tmp_path))

    resolved = runner._resolve_save_path("/现货分时分类型出清电量/日前/结果.xlsx")

    assert resolved.is_relative_to(tmp_path)
    assert resolved.name == "结果.xlsx"
    # save_as 只管文件名，目录交给任务存储结构。
    assert "现货分时分类型出清电量" not in str(resolved)


@pytest.mark.unit
def test_relative_save_as_does_not_add_second_date_layer(tmp_path):
    dated_dir = tmp_path / "2026-06" / "2026-06-29"
    runner = WorkflowRunner({"name": "保存路径测试", "steps": []}, download_dir=str(dated_dir))

    resolved = runner._resolve_save_path("市场用电信息({date}).xlsx")

    assert resolved.parent == dated_dir
    assert "202606" not in resolved.parts
    assert "20260629" not in resolved.parts


@pytest.mark.unit
def test_leading_slash_save_as_keeps_only_filename(tmp_path):
    dated_dir = tmp_path / "2026-06" / "2026-06-29"
    runner = WorkflowRunner({"name": "保存路径测试", "steps": []}, download_dir=str(dated_dir))

    resolved = runner._resolve_save_path("/现货分时分类型出清电量/日前/结果.xlsx")

    assert resolved == dated_dir / "结果.xlsx"


@pytest.mark.unit
async def test_unresolved_placeholder_fill_aborts_before_browser_input(tmp_path):
    events: list[dict] = []
    runner = WorkflowRunner({"name": "登录参数测试", "steps": []}, download_dir=str(tmp_path))
    runner.pages = [_FillShouldNotRunPage()]
    runner._on_event = events.append

    with pytest.raises(WorkflowExecutionError, match="未配置或未替换"):
        await runner._do_fill({"selector": "input[type='password']", "value": "${CERT_PASSWORD}"})

    assert any(e["event"] == "error_pause" for e in events)
    assert any(e["event"] == "waiting_input" for e in events)


@pytest.mark.unit
async def test_login_workflow_skips_form_when_already_on_dashboard(mocker, tmp_path):
    async def _no_sleep(*_args, **_kwargs):
        return None

    mocker.patch.object(workflow_runner_module.asyncio, "sleep", _no_sleep)
    runner = WorkflowRunner(
        {
            "name": "登录",
            "type": "login",
            "steps": [
                {"action": "login", "url": "https://pm.gd.csg.cn/portal/#/home"},
                {"action": "click", "selector": "button:has-text('登 录')"},
                {"action": "captcha", "submit_selector": "button:has-text('立即登录')"},
            ],
        },
        download_dir=str(tmp_path),
    )
    page = _AlreadyLoggedInPage()
    runner.pages = [page]
    events: list[dict] = []

    await runner._execute_steps(events.append, None, shared=True)

    assert runner.failed is False
    assert page.clicked == []
    assert any(e["event"] == "workflow_done" for e in events)


@pytest.mark.unit
def test_task_runner_storage_state_lives_at_download_root(tmp_path):
    dated_dir = tmp_path / "2026-06" / "2026-06-29"
    runner = TaskRunner({"name": "任务"}, download_dir=str(dated_dir))

    assert runner._storage_state_path == tmp_path / "storage_state.json"


@pytest.mark.unit
async def test_pick_date_uses_element_year_month_panel_for_far_date(tmp_path):
    runner = WorkflowRunner({"name": "日期测试", "steps": []}, download_dir=str(tmp_path))
    page = _DatePickerPage()
    runner.pages = [page]

    await runner._do_pick_date({"selector": ".dateBox .el-input__inner", "value": "2025-01-01"})

    assert page.selected == "2025-01-01"
    assert any(".el-date-picker__header-label" in action for action in page.actions)
    assert any('.el-year-table .cell:text-is("2025")' in action for action in page.actions)
    assert any(".el-month-table .cell:text-is" in action for action in page.actions)
    assert not any(".el-icon-arrow-left" in action for action in page.actions)
    assert not any(action.startswith("fill:") for action in page.actions)
    # 回归守卫：整条路径只能用探测读几何 + 鼠标轨迹落点。
    # 替身的 locator / page.click 会直接断言失败，这里再确认按下与抬起成对。
    assert page.probes, "没有走 DOM 探测"
    assert page.mouse_events.count("down") == page.mouse_events.count("up") == len(page.actions)


@pytest.mark.unit
async def test_pick_date_month_only_stops_after_month_selection(tmp_path):
    runner = WorkflowRunner({"name": "月结算单", "steps": []}, download_dir=str(tmp_path))
    page = _MonthPickerPage()
    runner.pages = [page]

    await runner._apply_date_pick({
        "selector": ".el-date-editor--month input",
        "value": "2025-01-01",
        "month_only": True,
    })

    assert page.selected == "2025-01"
    assert not any("td.available" in action for action in page.actions)


@pytest.mark.unit
async def test_captcha_exhausted_emits_screenshot_and_aborts(monkeypatch, tmp_path):
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    monkeypatch.setattr(captcha_solver, "CaptchaSolver", _AlwaysFailCaptchaSolver)
    events: list[dict] = []
    runner = WorkflowRunner(
        {
            "name": "验证码失败测试",
            "steps": [
                {
                    "action": "captcha",
                    "captcha_img": "img[src^='data:image']",
                    "captcha_input": "#validateOnly_verifyCode",
                    "retry": 1,
                }
            ],
        },
        download_dir=str(tmp_path),
    )
    runner.pages = [_CaptchaFailPage()]

    await runner._execute_steps(events.append, None, shared=True)

    event_names = [e["event"] for e in events]
    assert "captcha_failed" in event_names
    assert "step_error" in event_names
    assert "workflow_done" not in event_names
    captcha_event = next(e for e in events if e["event"] == "captcha_failed")
    assert captcha_event["screenshot"]
    assert (tmp_path / "_failures").exists()
    assert runner.failed is True


@pytest.mark.unit
async def test_captcha_rejects_legacy_auto_submit(monkeypatch, tmp_path):
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(captcha_solver, "CaptchaSolver", _AlwaysCodeCaptchaSolver)
    monkeypatch.setattr(workflow_runner_module.asyncio, "sleep", _no_sleep)

    events: list[dict] = []
    page = _LoginNoJumpPage()
    runner = WorkflowRunner({"name": "登录熔断测试", "steps": []}, download_dir=str(tmp_path))
    runner.pages = [page]
    runner._on_event = events.append

    with pytest.raises(WorkflowExecutionError, match="captcha 不再提交登录"):
        await runner._do_captcha(
            {
                "captcha_img": "img[src^='data:image']",
                "captcha_input": "#validateOnly_verifyCode",
                "retry": 5,
                "submit_selector": "button:has-text('立即登录')",
                "login_retry": 3,
            }
        )

    assert page.clicks.count("button:has-text('立即登录')") == 0
    assert not any(e["event"] == "captcha_failed" for e in events)


@pytest.mark.unit
async def test_task_login_failure_does_not_emit_task_done(monkeypatch, tmp_path):
    class _FailedWorkflowRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            self.pages = ["page"]
            self._current_page_idx = 0
            self.failed = True
            self.failure_reason = "错误弹窗: 数字证书连接失败"

        async def _execute_steps(self, on_event, _wait_continue, *, shared: bool = False):
            on_event({"event": "error_pause", "error": "数字证书连接失败"})
            on_event({"event": "aborted", "message": "步骤 1 失败: 数字证书连接失败"})

    monkeypatch.setattr(task_runner_module, "WorkflowRunner", _FailedWorkflowRunner)

    runner = TaskRunner(
        {"name": "串联任务", "login": "登陆流程测试", "indicators": ["指标A"]},
        download_dir=str(tmp_path),
        workflow_loader=lambda _wf_id: {"name": "登陆流程测试", "steps": [{"action": "wait"}]},
    )
    runner._shared_pages = ["page"]
    runner._current_page_idx = 0

    events: list[dict] = []

    def _emit(event: str, **kwargs):
        events.append({"event": event, **kwargs})

    await runner._run_login_shared(1, 2, _emit)

    assert any(e["event"] == "task_error" for e in events)
    assert any(
        e["event"] == "task_step" and e.get("status") == "error"
        for e in events
    )
    assert not any(
        e["event"] == "task_step" and e.get("status") == "done"
        for e in events
    )
    assert not any(e["event"] == "task_done" for e in events)
    assert runner._failed is True
