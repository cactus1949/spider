"""多场站执行：证书窗口、补数场站、落盘隔离和失败后续跑。"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from task_runner import TaskRunner, apply_catchup_storage
from tests.unit.test_task_retry_policy import _Context
from tests.unit.test_workflow_recovery_guards import _LoginFormPage

from spider_gd.core.crawler.shared_browser import SharedBrowser

from spider_gd.core.output_paths import station_scoped_path
from spider_gd.core.ukey_plan import (
    login_page_can_be_skipped,
    should_reuse_login,
    station_followup,
)
from spider_gd.infrastructure.windows.cert_availability import probe_script
from spider_gd.infrastructure.windows.cert_dialog import (
    CertDialogResult,
    CertDialogRow,
    CertificateWatch,
    _read_rows,
    apply_dialog_choice,
    assemble_certificate_rows,
)


class _Node:
    def __init__(
        self, kind: str, name: str, children: list | None = None, selectable: bool = False,
    ):
        self.ControlTypeName = kind
        self.Name = name
        self._children = children or []
        self.selectable = selectable
        self.selected = False

    def GetChildren(self):
        return self._children

    def GetSelectionItemPattern(self):
        if not self.selectable:
            raise RuntimeError("不是可选行")
        return self

    def Select(self) -> None:
        self.selected = True

    @property
    def IsSelected(self) -> bool:
        return self.selected


def test_dialog_reads_list_rows_and_only_clicks_ok_after_selection_sticks():
    headers = [
        _Node("HeaderItemControl", "颁发给"),
        _Node("HeaderItemControl", "单位"),
        _Node("HeaderItemControl", "截止日期"),
        _Node("HeaderItemControl", "颁发者"),
    ]
    first = _Node("ListItemControl", "甲", [
        _Node("TextControl", "单位甲公司"),
        _Node("TextControl", "2031-01-01"),
        _Node("TextControl", "NETCA"),
    ], selectable=True)
    second = _Node("ListItemControl", "乙", [
        _Node("TextControl", "单位乙公司"),
        _Node("TextControl", "2031-06-01"),
        _Node("TextControl", "NETCA"),
    ], selectable=True)
    dialog = _Node("WindowControl", "证书选择对话框", [*headers, first, second])
    rows = _read_rows(dialog)
    assert [row.org_name for row in rows] == ["单位甲公司", "单位乙公司"]
    calls = {"ok": 0, "cancel": 0}

    def selected(index: int) -> bool:
        return False

    result = apply_dialog_choice(
        rows, "单位甲",
        lambda index: None,
        selected,
        lambda: calls.__setitem__("ok", calls["ok"] + 1),
        lambda: calls.__setitem__("cancel", calls["cancel"] + 1),
    )
    assert result.ok is False
    assert calls["ok"] == 0
    assert calls["cancel"] == 1
    assert "选中" in result.reason


def test_ambiguous_dialog_cancels_instead_of_confirming():
    rows = assemble_certificate_rows(
        ["颁发给", "单位", "截止日期", "颁发者"],
        [
            ["甲", "单位甲公司", "2031-01-01", "NETCA"],
            ["乙", "单位乙公司", "2031-01-01", "NETCA"],
        ],
    )
    calls = {"ok": 0, "cancel": 0}
    result = apply_dialog_choice(
        rows, "公司",
        lambda index: None,
        lambda index: True,
        lambda: calls.__setitem__("ok", 1),
        lambda: calls.__setitem__("cancel", 1),
    )
    assert result.ok is False
    assert calls == {"ok": 0, "cancel": 1}


def test_certificate_watch_unblocks_a_click_that_is_waiting_on_the_dialog():
    dialog = {"open": False, "ok": False}
    rows = [
        CertDialogRow("甲", "单位甲公司", "2031-01-01", "NETCA"),
        CertDialogRow("乙", "单位乙公司", "2031-01-01", "NETCA"),
    ]
    chosen = {"index": None}

    def probe():
        if not dialog["open"]:
            return CertDialogResult(False, "", absent=True)
        return apply_dialog_choice(
            rows, "单位甲",
            lambda index: chosen.__setitem__("index", index),
            lambda index: chosen["index"] == index,
            lambda: dialog.__setitem__("ok", True),
            lambda: None,
        )

    watch = CertificateWatch("单位甲", probe=probe, interval=0.05, timeout=2, after_click=0.2)
    watch.start()
    try:
        dialog["open"] = True
        deadline = time.monotonic() + 2
        while not dialog["ok"]:
            if time.monotonic() > deadline:
                raise TimeoutError("登录点击一直被证书窗口挡住")
            time.sleep(0.02)
        watch.mark_click_finished()
        result = watch.finish(2)
    finally:
        watch.stop()
    assert result.ok is True
    assert chosen["index"] == 0


def test_watch_stops_immediately_when_this_machine_cannot_see_the_dialog():
    sleeps: list[float] = []
    watch = CertificateWatch(
        "单位甲",
        probe=lambda: CertDialogResult(False, "需要现场验收", needs_windows=True, absent=True),
        sleeper=lambda seconds: sleeps.append(seconds),
        timeout=20,
    )
    watch.start()
    result = watch.finish(2)
    assert result.needs_windows is True
    assert result.absent is True
    assert sleeps == []


def test_watch_poll_interval_is_not_a_tight_loop():
    now = {"t": 0.0}
    sleeps: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    def probe():
        if now["t"] < 1.6:
            return CertDialogResult(False, "", absent=True)
        return CertDialogResult(True, "", 0)

    watch = CertificateWatch(
        "单位甲", probe=probe, interval=0.8, timeout=5, clock=clock, sleeper=sleeper,
    )
    watch.start()
    result = watch.finish(2)
    assert result.ok is True
    assert sleeps
    assert min(sleeps) >= 0.8


def test_private_key_probe_is_silent():
    script = probe_script()
    assert "CryptAcquireCertificatePrivateKey" in script
    assert "0x40" in script
    assert "CryptUI" not in script
    assert "CredUI" not in script
    assert "MessageBox" not in script


def test_catchup_keeps_the_matched_task_ukeys_when_storage_is_overridden():
    stored = apply_catchup_storage(
        {"indicators": ["日前"]},
        [{
            "indicators": ["日前"],
            "enabled": True,
            "created_at": 1,
            "ukey_station_ids": ["station-a", "station-b"],
            "path_structure": "- [日前]",
        }],
        path_structure="",
        storage_overridden=True,
    )
    assert stored["ukey_station_ids"] == ["station-a", "station-b"]
    assert stored["path_structure"] == ""


def test_station_directories_do_not_share_a_file_and_same_station_stays_stable():
    one = station_scoped_path("/data/task", "场站甲", "/data/task/2026/日前.xlsx")
    two = station_scoped_path("/data/task", "场站乙", "/data/task/2026/日前.xlsx")
    again = station_scoped_path("/data/task", "场站甲", "/data/task/2026/日前.xlsx")
    assert one != two
    assert one == again
    assert one.endswith("场站甲/2026/日前.xlsx")


def test_one_station_failure_does_not_stop_the_rest_unless_the_user_cancels():
    continued = station_followup(
        user_cancelled=False, station_failed=True, more_stations=True,
    )
    stopped = station_followup(
        user_cancelled=True, station_failed=True, more_stations=True,
    )
    assert continued == "continue"
    assert stopped == "stop"
    assert should_reuse_login(station_count=2, auth_state="authenticated") is False
    assert login_page_can_be_skipped(True, "authenticated") is False
    runner = TaskRunner({"name": "t"})
    runner._record_failure("登录失败")
    assert runner._failed is True
    assert runner._stopped is False


class _StationPage(_LoginFormPage):
    def __init__(self, submit_failures=()) -> None:
        super().__init__()
        self.submit_failures = list(submit_failures)
        self.listeners: dict[str, list] = {}
        self.closed = False
        self.activations = 0

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.activations += 1

    async def opener(self):
        return None

    def on(self, event, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler) -> None:
        self.listeners[event].remove(handler)

    def apply_click(self, selector, index):
        if selector != "button:has-text('立即登录')":
            return super().apply_click(selector, index)
        failed = self.submit_failures.pop(0) if self.submit_failures else False
        if not failed:
            return super().apply_click(selector, index)
        self.clicked.append(selector)

        async def body():
            return b'{"code": 1, "message": "certificate login failed"}'

        response = SimpleNamespace(
            status=200,
            url="https://pm.gd.csg.cn/portal/xhr/sso-auth/login",
            headers={"content-type": "application/json"},
            body=body,
            request=SimpleNamespace(
                method="POST", resource_type="xhr", frame=SimpleNamespace(page=self),
            ),
        )
        for handler in list(self.listeners.get("response", [])):
            handler(response)


class _StationContext(_Context):
    def __init__(self, page):
        super().__init__(page)
        self.listeners: dict[str, list] = {}

    def on(self, event, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler) -> None:
        self.listeners[event].remove(handler)

    async def new_page(self):
        page = _StationPage()
        page.url = "about:blank"
        page.context = self
        self.pages.append(page)
        for handler in list(self.listeners.get("page", [])):
            result = handler(page)
            if asyncio.iscoroutine(result):
                await result
        return page

    async def clear_cookies(self):
        for page in self.pages:
            page.url = "https://pm.gd.csg.cn/portal/#/user/login"
            page.visible.clear()
            page.storage.clear()


class _StationSession(SharedBrowser):
    def __init__(self, page):
        self._context = _StationContext(page)
        self._browser = SimpleNamespace(is_connected=lambda: True)
        self._bind_page(page)
        self._cancel_requested = False
        self._active_runner = None
        self._config = {
            "target_origin": "https://pm.gd.csg.cn",
            "success_selector": "text=现货交易",
            "expected_identity": "",
        }
        self.pauses = asyncio.Queue()
        self.decisions = asyncio.Queue()
        self.events: list[dict] = []

    @asynccontextmanager
    async def execution(self, _name, on_event=None, *, hardware_lease=None):
        from spider_gd.infrastructure.hardware_lease import hardware_operation

        with hardware_operation(hardware_lease):
            self.emit = on_event or self.events.append
            yield self.emit

    async def wait_continue(self):
        await self.pauses.put(None)
        return await self.decisions.get()


@pytest.fixture
def station_io(monkeypatch):
    import spider_gd.infrastructure.network.wifi_connector as wifi
    import spider_gd.infrastructure.windows.cert_dialog as cert

    connections: list[str] = []

    class Connector:
        def list_networks(self):
            return [SimpleNamespace(ssid=ssid) for ssid in ("wifi-a", "wifi-b")]

        def prepare(self, options):
            connections.append(options.ssid)
            return SimpleNamespace(ok=True)

    monkeypatch.setattr(wifi, "WifiConnector", Connector)
    monkeypatch.setattr(cert, "confirm_certificate", lambda keyword, _thumbprint="": (
        CertDialogResult(False, "证书没有匹配单位") if keyword == "单位甲"
        else CertDialogResult(True, "", 0)
    ))
    return connections


def _station_task(tmp_path, *, login="login", indicators=None, stations=None, loader=None):
    configs = {
        "login": {
            "name": "login", "type": "login", "needs_login": False,
            "steps": [
                {"action": "click", "selector": "button:has-text('立即登录')"},
                {"action": "dismiss_modal", "timeout": 0},
                {"action": "ensure_login"},
            ],
        },
        "a": {"name": "a", "needs_login": False, "steps": [{"action": "wait", "ms": 1}]},
    }
    return TaskRunner(
        {"name": "现场回归", "login": login, "indicators": indicators or ["a"],
         "target_dates": ["2026-11-01"], "indicator_retry": 0},
        download_dir=str(tmp_path), workflow_loader=loader or configs.get,
        station_queue=stations or [
            {"name": "场站甲", "org_name": "单位甲", "wifi_ssid": "wifi-a"},
            {"name": "场站乙", "org_name": "单位乙", "wifi_ssid": "wifi-b"},
        ],
    )


async def _assert_paused(task, session, events, connections):
    pause = asyncio.create_task(session.pauses.get())
    try:
        done, _ = await asyncio.wait({task, pause}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert pause in done and task not in done, "任务必须保持暂停，不能结束或切到后站"
        assert [event["name"] for event in events if event["event"] == "station_start"] == ["场站甲"]
        assert connections == ["wifi-a"]
        assert not any(event["event"] == "step_done" for event in events)
        assert not any(event.get("phase") == "indicator" for event in events)
    finally:
        if not pause.done():
            pause.cancel()
        await asyncio.gather(pause, return_exceptions=True)


async def _finish_test_task(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.unit
async def test_certificate_mismatch_waits_for_authenticated_manual_recovery(tmp_path, station_io):
    page = _StationPage(submit_failures=[True, False])
    session = _StationSession(page)
    events: list[dict] = []
    runner = _station_task(tmp_path)
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        await _assert_paused(task, session, events, station_io)
        assert (await session.check_auth(page))["state"] == "unauthenticated"
        await session.decisions.put(True)
        await _assert_paused(task, session, events, station_io)
        assert page.clicked == ["button:has-text('立即登录')"]
        assert page.reloads == 0 and page.gotos == []
        page.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
        page.visible.add("text=现货交易")
        assert (await session.check_auth(page))["state"] == "authenticated"
        await session.decisions.put(True)
        unexpected_pause = asyncio.create_task(session.pauses.get())
        try:
            done, _ = await asyncio.wait(
                {task, unexpected_pause}, timeout=5, return_when=asyncio.FIRST_COMPLETED,
            )
            assert task in done and unexpected_pause not in done, "人工认证恢复后不能再次暂停"
            await task
        finally:
            await _finish_test_task(unexpected_pause)
        assert sum(event["event"] == "error_pause" for event in events) == 2
        assert page.clicked == ["button:has-text('立即登录')"] * 2
        assert page.reloads == 0 and page.gotos == []
        assert any(event.get("phase") == "indicator" for event in events)
        assert any(event["event"] == "task_done" for event in events)
        assert not any(event["event"] == "task_error" for event in events)
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("new_tab", [True, False])
async def test_manual_recovery_binds_verified_workbench_before_indicators(
    tmp_path, station_io, new_tab,
):
    page = _StationPage(submit_failures=[True])
    session = _StationSession(page)
    events: list[dict] = []
    bindings = []
    runner = _station_task(
        tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲", "wifi_ssid": "wifi-a"}],
    )

    def emit(event):
        events.append(event)
        if event["event"] == "step_done":
            bindings.append((session._active_runner.page, session.page))

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _assert_paused(task, session, events, station_io)
        landing = await page.context.new_page() if new_tab else page
        landing.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
        assert (await session.check_auth(landing))["state"] == "unauthenticated"
        await session.decisions.put(True)
        await _assert_paused(task, session, events, station_io)
        assert session.page is page
        assert not bindings
        landing.visible.add("text=现货交易")
        await session.decisions.put(True)
        unexpected_pause = asyncio.create_task(session.pauses.get())
        try:
            done, _ = await asyncio.wait(
                {task, unexpected_pause}, timeout=5, return_when=asyncio.FIRST_COMPLETED,
            )
            assert task in done, "人工新工作台认证后不能再次暂停"
            assert unexpected_pause not in done, "人工新工作台认证后不能再次暂停"
            await task
        finally:
            await _finish_test_task(unexpected_pause)
        assert bindings
        assert all(pair == (landing, landing) for pair in bindings)
        assert runner._shared_pages[runner._current_page_idx] is landing
        assert session.page is landing
        assert session._context is landing.context
        assert (await session.check_auth())["state"] == "authenticated"
        assert landing.activations > 0
        assert not landing.is_closed()
        assert page.clicked == ["button:has-text('立即登录')"]
        if new_tab:
            assert landing.clicked == []
        assert page.reloads == landing.reloads == 0
        assert page.gotos == landing.gotos == []
        assert sum(event["event"] == "error_pause" for event in events) == 2
        assert any(event.get("phase") == "indicator" for event in events)
        assert any(event["event"] == "task_done" for event in events)
        assert not any(event["event"] == "task_error" for event in events)
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("session_cancelled", [False, True])
async def test_certificate_pause_cancel_stops_the_real_task(
    tmp_path, station_io, session_cancelled,
):
    page = _StationPage(submit_failures=[True, False])
    session = _StationSession(page)
    events: list[dict] = []
    runner = _station_task(tmp_path)
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        await _assert_paused(task, session, events, station_io)
        session._cancel_requested = session_cancelled
        await session.decisions.put(False)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert runner._stopped is True
        assert [event["name"] for event in events if event["event"] == "station_start"] == ["场站甲"]
        assert station_io == ["wifi-a"]
        assert page.clicked == ["button:has-text('立即登录')"]
        assert not any(event["event"] in {"step_done", "step_error", "task_done"} for event in events)
        assert sum(event["event"] == "task_error" for event in events) == 1
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("before_indicators", [True, False])
async def test_require_auth_cancel_closes_station_and_pending_plan(
    tmp_path, station_io, before_indicators,
):
    session = _StationSession(_StationPage())
    events: list[dict] = []
    configs = {
        "login": {"name": "login", "type": "login", "needs_login": False,
                  "steps": [{"action": "click", "selector": "button:has-text('立即登录')"}]},
        **{name: {"name": name, "needs_login": True,
                  "steps": [{"action": "wait", "ms": 1}]} for name in ("a", "b", "c")},
    }

    def load(name):
        if runner._active_station_name == "前站" and name != "login":
            return None
        return configs.get(name)

    runner = _station_task(
        tmp_path, indicators=["a", "b", "c"], loader=load,
        stations=[{"name": "前站", "wifi_ssid": "wifi-a"},
                  {"name": "场站甲", "wifi_ssid": "wifi-a"},
                  {"name": "场站甲", "wifi_ssid": "wifi-b"}],
    )

    def emit(event):
        events.append(event)
        if runner._active_station_name != "场站甲" or event["event"] != "task_step":
            return
        phase, status = ("login", "done") if before_indicators else ("indicator", "running")
        if event.get("phase") == phase and event.get("status") == status:
            session.page.visible.clear()

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await asyncio.wait_for(session.pauses.get(), timeout=5)
        assert not task.done()
        assert session._active_runner is runner
        assert station_io == ["wifi-a", "wifi-a"]
        boundary = len(events)
        session._cancel_requested = True
        await session.decisions.put(False)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        rows = next(event["stations"] for event in events if event["event"] == "station_report")
        assert len(rows) == 3, "认证检查取消必须收本站，并按队列下标跳过重名后站"
        assert rows[0]["status"] == "failed"
        assert "指标不存在" in rows[0]["reason"]
        assert rows[1]["status"] == "cancelled"
        assert rows[1]["counts"] == {"success": 0, "failed": 0, "skipped": 3}
        assert "用户取消" in rows[1]["reason"]
        assert "指标不存在" not in rows[1]["reason"]
        assert rows[2] == {
            "name": "场站甲", "status": "skipped", "reason": "任务已停止（用户取消）",
        }
        statuses = {event["indicator"]: event["status"] for event in events[boundary:]
                    if event["event"] == "indicator_status"}
        assert statuses == {"a": "skipped", "b": "skipped", "c": "skipped"}
        assert station_io == ["wifi-a", "wifi-a"]
        assert not any(event["event"] in {"station_start", "step_done", "task_done"}
                       for event in events[boundary:])
        errors = [event for event in events if event["event"] == "task_error"]
        assert len(errors) == 1
        assert errors[0]["error"] == "用户取消"
        if before_indicators:
            start = next(index for index, event in enumerate(events)
                         if event["event"] == "station_start" and event["index"] == 2)
            assert not any(event.get("phase") == "indicator" for event in events[start:])
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
async def test_login_workflow_final_auth_cancel_skips_pending_indicators(tmp_path, station_io):
    session = _StationSession(_StationPage())
    events: list[dict] = []
    configs = {
        "login": {"name": "login", "type": "login", "needs_login": False,
                  "steps": [{"action": "wait", "ms": 1}]},
        **{name: {"name": name, "needs_login": True,
                  "steps": [{"action": "wait", "ms": 1}]} for name in ("a", "b", "c")},
    }
    runner = _station_task(
        tmp_path, indicators=["a", "b", "c"], loader=configs.get,
        stations=[{"name": "场站甲", "wifi_ssid": "wifi-a"},
                  {"name": "场站甲", "wifi_ssid": "wifi-b"}],
    )
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        await asyncio.wait_for(session.pauses.get(), timeout=5)
        assert not task.done()
        assert session._active_runner.name == "login"
        assert not any(event.get("phase") == "indicator" for event in events)
        session._cancel_requested = True
        await session.decisions.put(False)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        rows = next(event["stations"] for event in events if event["event"] == "station_report")
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["counts"] == {"success": 0, "failed": 0, "skipped": 3}
        assert rows[1] == {
            "name": "场站甲", "status": "skipped", "reason": "任务已停止（用户取消）",
        }
        statuses = {event["indicator"]: event["status"] for event in events
                    if event["event"] == "indicator_status"}
        assert statuses == {"a": "skipped", "b": "skipped", "c": "skipped"}
        assert station_io == ["wifi-a"]
        errors = [event for event in events if event["event"] == "task_error"]
        assert len(errors) == 1
        assert errors[0]["error"] == "用户取消"
        assert not any(event["event"] == "task_done" for event in events)
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
async def test_first_indicator_cancel_marks_remaining_indicators(tmp_path, station_io):
    page = _StationPage()
    session = _StationSession(page)
    events: list[dict] = []
    runner = _station_task(
        tmp_path, login="", indicators=["a", "b", "c"],
        stations=[{"name": "场站甲", "wifi_ssid": "wifi-a"},
                  {"name": "场站甲", "wifi_ssid": "wifi-b"}],
        loader=lambda name: {"name": name, "needs_login": False,
                             "steps": [{"action": "dismiss_modal", "timeout": 0}]},
    )

    def emit(event):
        events.append(event)
        if event["event"] == "task_step" and event.get("status") == "running":
            page.visible.add(".ant-message-error")

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await asyncio.wait_for(session.pauses.get(), timeout=5)
        assert not task.done()
        assert session._active_runner.name == "a"
        boundary = len(events)
        await session.decisions.put(False)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        statuses = {event["indicator"]: event["status"] for event in events
                    if event["event"] == "indicator_status"}
        assert statuses == {
            "a": "skipped", "b": "skipped", "c": "skipped",
        }, "取消后不能留下 pending"
        rows = next(event["stations"] for event in events if event["event"] == "station_report")
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["counts"] == {"success": 0, "failed": 0, "skipped": 3}
        assert rows[1] == {
            "name": "场站甲", "status": "skipped", "reason": "任务已停止（用户取消）",
        }
        assert station_io == ["wifi-a"]
        assert not any(event["event"] in {"station_start", "step_done", "task_done"}
                       for event in events[boundary:])
        errors = [event for event in events if event["event"] == "task_error"]
        assert len(errors) == 1
        assert errors[0]["error"] == "用户取消"
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("during_auth", [False, True])
async def test_cancel_second_indicator_preserves_success_and_download(
    tmp_path, station_io, monkeypatch, during_auth,
):
    page = _StationPage()
    session = _StationSession(page)
    events: list[dict] = []
    content = b"date,value\n2026-11-01,1\n"

    async def save_as(path):
        Path(path).write_bytes(content)

    @asynccontextmanager
    async def expect_download(**_kwargs):
        value = asyncio.get_running_loop().create_future()
        value.set_result(SimpleNamespace(suggested_filename="a.csv", save_as=save_as))
        yield SimpleNamespace(value=value)

    monkeypatch.setattr(page, "expect_download", expect_download, raising=False)
    configs = {
        "a": {"name": "a", "needs_login": False,
              "steps": [{"action": "export", "save_as": "a.csv", "query_settle_ms": 0}]},
        "b": {"name": "b", "needs_login": during_auth,
              "steps": [{"action": "dismiss_modal", "timeout": 0}]},
        "c": {"name": "c", "needs_login": False, "steps": [{"action": "wait", "ms": 1}]},
    }
    runner = _station_task(
        tmp_path, login="", indicators=["a", "b", "c"], loader=configs.get,
        stations=[{"name": "场站甲", "wifi_ssid": "wifi-a"},
                  {"name": "场站甲", "wifi_ssid": "wifi-b"}],
    )

    def emit(event):
        events.append(event)
        if event["event"] != "task_step" or event.get("status") != "running":
            return
        if event.get("name") == "a":
            page.visible.add("button:has-text('导出')")
        elif event.get("name") == "b" and not during_auth:
            page.visible.add(".ant-message-error")

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await asyncio.wait_for(session.pauses.get(), timeout=5)
        assert not task.done()
        saved = [event for event in events
                 if event["event"] == "file_result" and event.get("status") == "saved"]
        assert len(saved) == 1
        path = Path(saved[0]["path"])
        assert path.read_bytes() == content
        boundary = len(events)
        session._cancel_requested = True
        await session.decisions.put(False)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        statuses = {event["indicator"]: event["status"] for event in events
                    if event["event"] == "indicator_status"}
        assert statuses == {"a": "success", "b": "skipped", "c": "skipped"}
        rows = next(event["stations"] for event in events if event["event"] == "station_report")
        assert rows[0]["status"] == "cancelled"
        assert rows[0]["counts"] == {"success": 1, "failed": 0, "skipped": 2}
        assert "已完成项保留" in rows[0]["reason"]
        assert rows[1] == {
            "name": "场站甲", "status": "skipped", "reason": "任务已停止（用户取消）",
        }
        assert path.read_bytes() == content
        assert not any(event["event"] == "file_result" and event.get("status") == "failed"
                       for event in events[boundary:])
        assert not any(event["event"] in {"station_start", "step_done", "task_done"}
                       for event in events[boundary:])
        assert station_io == ["wifi-a"]
        errors = [event for event in events if event["event"] == "task_error"]
        assert len(errors) == 1
        assert errors[0]["error"] == "用户取消"
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
async def test_cancel_after_saved_files_reports_interrupted_station(
    tmp_path, station_io, monkeypatch,
):
    from spider_gd.core.ukey_plan import format_station_report
    from workflow_runner import WorkflowExecutionError, WorkflowRunner

    saved = []

    async def export(runner, _step):
        if runner.download_dir.name == "前站":
            raise WorkflowExecutionError("数字证书连接失败：前站证书错误")
        if runner.name == "c":
            await runner._pause_and_wait("查询暂停，等待人工处理")
            return
        path = runner.download_dir / f"{runner.name}.xlsx"
        path.write_bytes(b"persisted station data")
        saved.append(path)
        runner._note_file("saved", path)

    monkeypatch.setattr(WorkflowRunner, "_do_export", export)
    page = _StationPage()
    session = _StationSession(page)
    events: list[dict] = []
    runner = _station_task(
        tmp_path, login="", indicators=["a", "b", "c"],
        stations=[
            {"name": "前站", "wifi_ssid": "wifi-a"},
            {"name": "场站甲", "wifi_ssid": "wifi-a"},
            {"name": "场站甲", "wifi_ssid": "wifi-b"},
        ],
        loader=lambda name: {"name": name, "needs_login": False,
                             "steps": [{"action": "export", "save_as": f"{name}.xlsx"}]},
    )
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        await asyncio.wait_for(session.pauses.get(), timeout=5)
        assert not task.done()
        assert [event["name"] for event in events if event["event"] == "station_start"] == ["前站", "场站甲"]
        assert station_io == ["wifi-a", "wifi-a"]
        assert len(saved) == 2 and all(path.is_file() for path in saved)
        assert sum(event["event"] == "file_result" and event.get("status") == "saved"
                   for event in events) == 2
        boundary = len(events)
        await session.decisions.put(False)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        rows = next(event["stations"] for event in events if event["event"] == "station_report")
        assert rows[1]["status"] == "cancelled"
        assert rows[1]["counts"] == {"success": 2, "failed": 0, "skipped": 1}
        assert "用户取消" in rows[1]["reason"] and "已完成项保留" in rows[1]["reason"]
        assert "前站证书错误" not in rows[1]["reason"]
        assert rows[2] == {"name": "场站甲", "status": "skipped", "reason": "任务已停止（用户取消）"}
        report = format_station_report(rows)
        assert "场站 场站甲：已中断" in report
        assert "未执行（任务已停止（用户取消））" in report
        assert len(rows) == 3 and rows[0]["status"] == "failed"
        assert all(path.read_bytes() == b"persisted station data" for path in saved)
        assert not any(event["event"] in {"step_done", "station_start", "task_done"}
                       for event in events[boundary:])
        errors = [event for event in events if event["event"] == "task_error"]
        assert len(errors) == 1 and errors[0]["error"] == "用户取消"
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("during_pause", [False, True])
async def test_wifi_cancel_closes_current_station_once_with_fresh_counts(
    tmp_path, monkeypatch, during_pause,
):
    import spider_gd.infrastructure.network.wifi_wait as wifi_wait

    calls = []

    def wait(ssid, **_kwargs):
        calls.append(ssid)
        if ssid == "wifi-first":
            return wifi_wait.WifiWaitResult(True, "connected", "", 1)
        status = "missing" if during_pause else "cancelled"
        return wifi_wait.WifiWaitResult(False, status, "热点不可用", 1)

    monkeypatch.setattr(wifi_wait, "wait_for_saved_ssid", wait)
    page = _StationPage()
    session = _StationSession(page)
    events: list[dict] = []
    runner = _station_task(
        tmp_path, login="", indicators=["a", "b"],
        stations=[
            {"name": "前站", "wifi_ssid": "wifi-first"},
            {"name": "场站甲", "wifi_ssid": "wifi-a"},
            {"name": "场站甲", "wifi_ssid": "wifi-b"},
        ],
        loader=lambda name: {"name": name, "needs_login": False,
                             "steps": [{"action": "wait", "ms": 1}]},
    )
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        if during_pause:
            await asyncio.wait_for(session.pauses.get(), timeout=5)
            assert not task.done()
            assert calls == ["wifi-first", "wifi-a"]
            await session.decisions.put(False)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        rows = next(event["stations"] for event in events if event["event"] == "station_report")
        assert rows[1]["status"] == "cancelled"
        assert rows[1]["counts"] == {"success": 0, "failed": 0, "skipped": 0}
        assert len(rows) == 3 and rows[0]["status"] == "ran" and rows[2]["status"] == "skipped"
        assert "用户取消" in rows[1]["reason"] and "用户取消" in rows[2]["reason"]
        assert calls == ["wifi-first", "wifi-a"]
        assert not any(event["event"] == "task_done" for event in events)
        assert sum(event["event"] == "task_error" for event in events) == 1
        assert sum(event.get("phase") == "indicator" and event.get("status") == "running"
                   for event in events) == 2
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
async def test_non_auth_station_failure_still_runs_next_station(tmp_path, station_io):
    page = _StationPage()
    session = _StationSession(page)
    events: list[dict] = []

    def load(name):
        if runner._active_station_name == "场站甲":
            return None
        return {"name": name, "needs_login": False, "steps": [{"action": "wait", "ms": 1}]}

    runner = _station_task(tmp_path, login="", loader=load)
    await runner.run(events.append, session=session)
    assert station_io == ["wifi-a", "wifi-b"]
    rows = next(event["stations"] for event in events if event["event"] == "station_report")
    assert [row["status"] for row in rows] == ["failed", "ran"]
    assert rows[0]["reason"] == "指标不存在: a"
    assert runner._stopped is False
    done = next(event for event in events if event["event"] == "task_done")
    assert done["success"] == 1 and done["failed"] == 1
    assert not any(event["event"] == "task_error" for event in events)


@pytest.mark.unit
@pytest.mark.parametrize("unattended", [False, True])
async def test_wifi_non_cancel_skip_remains_unexecuted_and_continues(
    tmp_path, monkeypatch, unattended,
):
    import spider_gd.infrastructure.network.wifi_wait as wifi_wait

    calls = []

    def wait(ssid, **_kwargs):
        calls.append(ssid)
        return wifi_wait.WifiWaitResult(ssid == "wifi-b", "missing", "热点不可用", 1)

    monkeypatch.setattr(wifi_wait, "wait_for_saved_ssid", wait)
    session = _StationSession(_StationPage())
    session.consume_station_skip = lambda: True
    events: list[dict] = []
    runner = _station_task(tmp_path, login="")
    runner._unattended = unattended
    if not unattended:
        await session.decisions.put(True)
    await asyncio.wait_for(runner.run(events.append, session=session), timeout=5)
    rows = next(event["stations"] for event in events if event["event"] == "station_report")
    assert [row["status"] for row in rows] == ["skipped", "ran"]
    assert rows[0]["reason"] == "热点不可用"
    assert calls == ["wifi-a", "wifi-b"]
    assert runner._stopped is False
    assert any(event["event"] == "task_done" for event in events)
    assert not any(event["event"] == "task_error" for event in events)


def test_shared_hardware_lease_is_exclusive():
    from spider_gd.infrastructure.hardware_lease import HardwareLease, hardware_operation

    with hardware_operation() as owner:
        assert HardwareLease.try_acquire() is None
        with hardware_operation(owner):
            assert HardwareLease.try_acquire() is None
        assert HardwareLease.try_acquire() is None
    with hardware_operation():
        pass
