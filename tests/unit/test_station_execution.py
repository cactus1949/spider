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
        self._opener = None
        self.business_identity = ""
        self.identity_count = 1
        self.identity_visible = True
        self.identity_read_gate = None
        self.login_identities: list[str] = []
        self.login_tasks: set[asyncio.Task] = set()
        self.auth_proofs: dict[str, bool] = {}
        self.auth_proof_gate = None

    def __setattr__(self, name, value):
        previous = getattr(self, name, None)
        super().__setattr__(name, value)
        if previous == value:
            return
        if name in {"business_identity", "identity_count", "identity_visible"}:
            self._notify_proofs()
        if name in {"url", "closed"}:
            event = "framenavigated" if name == "url" else "close"
            for handler in list(getattr(self, "listeners", {}).get(event, [])):
                handler(self)

    def _notify_proofs(self):
        for token in list(getattr(self, "auth_proofs", {})):
            self.auth_proofs[token] = True
            for handler in list(getattr(self, "listeners", {}).get("console", [])):
                handler(SimpleNamespace(text=token))

    def tick_clock(self):
        """Benign dashboard churn (1Hz clock / polling widget) outside the identity."""
        # Mirrors the identity-scoped MutationObserver in _STATION_AUTH_PROOF_JS:
        # churn that does not intersect the identity element never dirties a proof.

    def replace_identity_node(self):
        """Identity element replaced then restored with identical text."""
        self._notify_proofs()

    async def evaluate(self, script, arg=None):
        # Browser boundary fake: document MutationObserver -> console notification.
        if isinstance(arg, dict) and str(arg.get("op", "")).startswith("station_auth_"):
            op, token = arg["op"], arg["token"]
            if self.auth_proof_gate is not None and self.auth_proof_gate[0] == op:
                _, started, release = self.auth_proof_gate
                started.set()
                await release.wait()
            if op == "station_auth_watch":
                self.auth_proofs[token] = False
                return True
            if op == "station_auth_flush":
                return token in self.auth_proofs and not self.auth_proofs[token]
            if op == "station_auth_stop":
                self.auth_proofs.pop(token, None)
                return True
            raise AssertionError(op)
        return await super().evaluate(script, arg)

    def locator(self, selector):
        if selector != "#current-business-identity":
            return super().locator(selector)
        page = self

        class IdentityLocator:
            @property
            def first(self):
                return self

            def nth(self, _index):
                return self

            async def count(self):
                return page.identity_count

            async def is_visible(self):
                return page.identity_visible

            async def inner_text(self, **_kwargs):
                if not page.identity_count:
                    raise TimeoutError("identity unavailable")
                if page.identity_read_gate is not None:
                    started, release = page.identity_read_gate
                    started.set()
                    await release.wait()
                return page.business_identity

        return IdentityLocator()

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.activations += 1

    async def opener(self):
        return self._opener

    def on(self, event, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler) -> None:
        self.listeners[event].remove(handler)

    def apply_click(self, selector, index):
        if selector != "button:has-text('立即登录')":
            return super().apply_click(selector, index)
        failed = self.submit_failures.pop(0) if self.submit_failures else False
        if not failed:
            identity = (
                self.login_identities.pop(0) if self.login_identities else self.business_identity
            )
            if "powerEnterprise" in self.url:
                self.clicked.append(selector)

                async def land():
                    landing = await self.context.new_page(opener=self)
                    _workbench(landing, identity)
                    landing.storage["pfxh-token"] = "token"

                task = asyncio.create_task(land())
                self.login_tasks.add(task)
                task.add_done_callback(self.login_tasks.discard)
            else:
                super().apply_click(selector, index)
                self.business_identity = identity
        else:
            self.clicked.append(selector)

        async def body():
            return (b'{"code": 1, "message": "certificate login failed"}' if failed
                    else b'{"code": 0}')

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
        self.cookies = [{"name": "session", "value": "old-unit"}]

    def on(self, event, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler) -> None:
        self.listeners[event].remove(handler)

    async def new_page(self, *, opener=None):
        page = _StationPage()
        page.url = "about:blank"
        page.context = self
        page._opener = opener
        self.pages.append(page)
        for handler in list(self.listeners.get("page", [])):
            result = handler(page)
            if asyncio.iscoroutine(result):
                await result
        return page

    async def clear_cookies(self):
        self.cookies.clear()


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
            "identity_selector": "#current-business-identity",
            "auth_policy": "reuse_only",
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
    page.login_identities = ["单位乙"]
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
        page.business_identity = "单位甲"
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
        landing.business_identity = "单位乙"
        await session.decisions.put(True)
        await _assert_paused(task, session, events, station_io)
        assert not bindings
        landing.business_identity = "单位甲"
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
        assert sum(event["event"] == "error_pause" for event in events) == 3
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


@pytest.fixture(autouse=True)
def offline_station_identity(monkeypatch):
    import socket
    import subprocess

    def forbidden(*_args, **_kwargs):
        raise AssertionError("station tests forbid network and subprocesses")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


def _workbench(page, identity):
    page.url = "https://pm.gd.csg.cn/portal/#/powerEnterprise"
    page.visible.update({"text=现货交易", "text=结算管理", "text=退出登录"})
    page.business_identity = identity


async def _identity_pause(task, session, events=None):
    pause = asyncio.create_task(session.pauses.get())
    try:
        done, _ = await asyncio.wait({task, pause}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        message = str(events) if events else "wrong/unreadable unit must remain paused"
        assert pause in done, message
        assert task not in done, message
    finally:
        await _finish_test_task(pause)


def _locked_station_session(page):
    session = SharedBrowser()
    context = _StationContext(page)
    session._browser = SimpleNamespace(is_connected=lambda: True, contexts=[context])
    session._bind_page(page)
    session._config.update({
        "success_selector": "text=现货交易",
        "identity_selector": "#current-business-identity",
    })
    session.pauses = asyncio.Queue()
    return session


async def _complete_station_continue(task, session):
    run = session.status()["run"]
    session.resume(run["id"], run["pause_version"])
    pause = asyncio.create_task(session.pauses.get())
    try:
        done, _ = await asyncio.wait({task, pause}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert task in done and pause not in done, "Continue must use the uniquely verified new tab"
        await task
    finally:
        await _finish_test_task(pause)


@pytest.mark.parametrize("boundary", ["login", "indicator"])
async def test_station_identity_generic_continue_adopts_verified_tab(tmp_path, boundary):
    page = _StationPage()
    _workbench(page, "单位乙" if boundary == "login" else "单位甲")
    page.storage["signed-document"] = "keep-old"
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events, bindings = [], []

    def emit(event):
        events.append(event)
        if (boundary == "indicator" and event.get("phase") == "indicator"
                and event.get("status") == "running"):
            page.business_identity = "单位乙"
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)
        if event["event"] == "step_done":
            bindings.append((session._active_runner.page, session.page))

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        assert session._active_runner is runner
        assert session.status()["busy"] is True
        with pytest.raises(RuntimeError, match="占用"):
            await session.select_page("manual-selection-stays-locked")
        landing = await page.context.new_page()
        _workbench(landing, "单位甲有限公司")
        landing.storage["signed-document"] = "keep-new"
        assert session.page is page, "opening a tab alone must not adopt it"
        await _complete_station_continue(task, session)
        assert bindings and all(pair == (landing, landing) for pair in bindings)
        assert runner._workbench_page is landing
        assert runner._shared_pages == [landing] and session.page is landing
        assert session._context is runner._context is landing.context
        assert landing in runner._kept_pages and landing in runner._protected_pages
        assert landing.activations > 0
        assert not page.closed and not landing.closed
        assert page.storage == {"signed-document": "keep-old"}
        assert landing.storage == {"signed-document": "keep-new"}
        assert not page.clicked and not landing.clicked
        assert not page.gotos and not landing.gotos and page.reloads == landing.reloads == 0
        assert sum(event["event"] == "task_done" for event in events) == 1
        assert not any(event["event"] == "task_error" for event in events)
        assert session.status()["run"]["state"] == "succeeded"
        assert session.status()["busy"] is False
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("finish", ["continue", "cancel"])
@pytest.mark.parametrize("window", [
    "identity_change", "activation", "final_check", "final_identity_change", "final_flush",
    "transient_identity",
])
async def test_station_recovery_mutation_repauses_until_unique_stable_proof(
    tmp_path, monkeypatch, window, finish,
):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    session._station_org_name = "previous scope"
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events, bindings, owned, manual = [], [], [], []
    release = asyncio.Event()

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)
        if event["event"] == "step_done":
            bindings.append((session._active_runner.page, session.page))
        if event["event"] in {"station_start", "step_done"}:
            popup = _StationPage()
            _workbench(popup, "单位乙")
            popup.url = "https://pm.gd.csg.cn/pfxh/stc/automation"
            popup.context = page.context
            page.context.pages.append(popup)
            owned.append(popup)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        notes = await page.context.new_page()
        notes.url = "https://notes.example.test/manual"
        manual.extend([landing, notes])
        original = landing.bring_to_front
        # Each Continue gets new evidence; even a second mutation must re-pause.
        for _attempt in range(2):
            page.business_identity = "单位乙"
            for other in manual:
                if other is not landing:
                    other.business_identity = "单位乙"
            started, release = asyncio.Event(), asyncio.Event()
            landing.identity_read_gate = None
            landing.auth_proof_gate = None

            async def activation_gate(started=started, release=release):
                await original()
                if window == "activation":
                    started.set()
                    await release.wait()
                elif window in {"final_check", "final_identity_change", "transient_identity"}:
                    landing.identity_read_gate = started, release
                elif window == "final_flush":
                    landing.auth_proof_gate = "station_auth_flush", started, release

            monkeypatch.setattr(landing, "bring_to_front", activation_gate)
            if window == "identity_change":
                landing.identity_read_gate = started, release
            run = dict(session.status()["run"])
            session.resume(run["id"], run["pause_version"])
            await asyncio.wait_for(started.wait(), timeout=5)
            if window in {
                "identity_change", "final_identity_change", "final_flush", "transient_identity",
            }:
                # No URL/context change: a previously read candidate becomes valid.
                page.business_identity = "单位甲"
                if window == "transient_identity":
                    page.business_identity = "单位乙"  # Identical scan results are not stability.
            else:
                twin = await page.context.new_page()
                _workbench(twin, "单位甲")
                manual.append(twin)
            release.set()
            await _identity_pause(task, session, events)
            assert session.status()["run"]["state"] == "paused"
            assert session.status()["run"]["pause_version"] == run["pause_version"] + 1
            assert session.status()["busy"] is True
            assert session.page is page
            assert not bindings
            assert not any(event.get("phase") == "indicator" for event in events)
            assert not page.closed
            assert all(not other.closed for other in manual)
            with pytest.raises(RuntimeError, match="占用"):
                await session.select_page("manual-selection-stays-locked")

        if finish == "cancel":
            session.cancel(session.status()["run"]["id"])
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert not bindings
            assert session.status()["run"]["state"] == "cancelled"
            assert sum(event["event"] == "task_error" for event in events) == 1
        else:
            page.business_identity = "单位乙"
            for other in manual:
                if other is not landing:
                    other.business_identity = "单位乙"
            landing.identity_read_gate = landing.auth_proof_gate = None
            monkeypatch.setattr(landing, "bring_to_front", original)
            await _complete_station_continue(task, session)
            assert bindings
            assert all(pair == (landing, landing) for pair in bindings)
            assert session.page is landing
            assert session.status()["run"]["state"] == "succeeded"
            assert sum(event["event"] == "task_done" for event in events) == 1
            assert not any(event["event"] == "task_error" for event in events)
        assert not page.closed
        assert all(not other.closed for other in manual)
        assert owned
        assert all(popup.closed for popup in owned)
        assert not page.clicked
        assert all(not other.clicked for other in manual)
        assert session._station_org_name == "previous scope"
        assert session.status()["busy"] is False
        assert not any(page.context.listeners.values())
        assert all(not any(other.listeners.values()) and not other.auth_proofs
                   for other in page.context.pages)
    finally:
        release.set()
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("window", ["installation", "final_check"])
@pytest.mark.parametrize("cancel_source", ["session", "disconnect"])
async def test_station_recovery_cancel_settles_proof_cleanup(
    tmp_path, monkeypatch, window, cancel_source,
):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    session._station_org_name = "previous scope"
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events = []
    started, release = asyncio.Event(), asyncio.Event()

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        if window == "installation":
            landing.auth_proof_gate = "station_auth_watch", started, release
        else:
            original = landing.bring_to_front

            async def arm_final_gate():
                await original()
                landing.identity_read_gate = started, release

            monkeypatch.setattr(landing, "bring_to_front", arm_final_gate)
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await asyncio.wait_for(started.wait(), timeout=5)
        notes = await page.context.new_page()
        notes.url = "https://notes.example.test/manual"
        if cancel_source == "session":
            session.cancel(run["id"])
        else:
            task.cancel()
        release.set()
        if cancel_source == "disconnect":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
        else:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert session.status()["run"]["state"] == "cancelled"
        assert session.status()["busy"] is False
        assert session._station_org_name == "previous scope"
        assert not any(event.get("phase") == "indicator" for event in events)
        assert not page.closed
        assert not landing.closed
        assert not notes.closed
        assert not any(page.context.listeners.values())
        assert all(not any(other.listeners.values()) and not other.auth_proofs
                   for other in page.context.pages)
    finally:
        release.set()
        await _finish_test_task(task)


@pytest.mark.unit
async def test_station_recovery_repeated_disconnect_keeps_lease_until_proof_cleanup(
    tmp_path,
):
    from spider_gd.infrastructure.hardware_lease import HardwareLease

    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    started, release = asyncio.Event(), asyncio.Event()
    cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event()
    events = []

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        landing.identity_read_gate = started, release
        landing.auth_proof_gate = "station_auth_stop", cleanup_started, cleanup_release
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done(), "disconnect must not abandon in-flight observer teardown"
        assert session.status()["busy"] is True
        lease = HardwareLease.try_acquire()
        if lease is not None:
            lease.close()
        assert lease is None, "real OS lease must remain held until cleanup settles"
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert session.status()["busy"] is False
        assert session.status()["run"]["state"] == "cancelled"
        assert not any(event.get("phase") == "indicator" for event in events)
        assert not page.closed
        assert not landing.closed
        assert not any(page.context.listeners.values())
        assert all(not other.auth_proofs for other in page.context.pages)
    finally:
        release.set()
        cleanup_release.set()
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("churn", ["correct_page", "stale_tab", "both"])
async def test_station_recovery_benign_dashboard_churn_does_not_block(tmp_path, churn):
    """Live-dashboard churn outside the identity element must not block recovery."""
    page = _StationPage()
    _workbench(page, "单位乙")  # stale same-origin business tab
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events, bindings = [], []
    started, release = asyncio.Event(), asyncio.Event()

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)
        if event["event"] == "step_done":
            bindings.append((session._active_runner.page, session.page))

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        landing.auth_proof_gate = "station_auth_flush", started, release
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await asyncio.wait_for(started.wait(), timeout=5)
        if churn in {"correct_page", "both"}:
            landing.tick_clock()
        if churn in {"stale_tab", "both"}:
            page.tick_clock()
        release.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert bindings
        assert all(pair == (landing, landing) for pair in bindings)
        assert session.page is landing
        assert session.status()["run"]["state"] == "succeeded"
        assert sum(event["event"] == "task_done" for event in events) == 1
        assert not any(event["event"] == "task_error" for event in events)
        assert not page.closed
        assert not landing.closed
        assert not any(page.context.listeners.values())
        assert all(not other.auth_proofs for other in page.context.pages)
    finally:
        release.set()
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ["text_revert", "node_replace"])
async def test_station_recovery_identity_change_and_revert_still_rejected(
    tmp_path, monkeypatch, mutation,
):
    """Scoping must keep change-and-revert ON THE IDENTITY fail closed."""
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events = []
    started, release = asyncio.Event(), asyncio.Event()

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        original = landing.bring_to_front

        async def gate():
            await original()
            landing.identity_read_gate = started, release

        monkeypatch.setattr(landing, "bring_to_front", gate)
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await asyncio.wait_for(started.wait(), timeout=5)
        if mutation == "text_revert":
            landing.business_identity = "单位甲（临时）"
            landing.business_identity = "单位甲"
        else:
            landing.replace_identity_node()
        release.set()
        await _identity_pause(task, session, events)
        assert session.page is page
        assert session.status()["run"]["state"] == "paused"
        assert not any(event.get("phase") == "indicator" for event in events)
        session.cancel(session.status()["run"]["id"])
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert session.status()["run"]["state"] == "cancelled"
        assert not page.closed
        assert not landing.closed
        assert not any(page.context.listeners.values())
        assert all(not other.auth_proofs for other in page.context.pages)
    finally:
        release.set()
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("bounded_by", ["eval_timeout", "cleanup_deadline"])
async def test_station_recovery_hung_cleanup_releases_lease_within_deadline(
    tmp_path, monkeypatch, bounded_by,
):
    """A wedged renderer must not hold the hardware lease forever during teardown."""
    import task_runner as task_runner_module

    from spider_gd.infrastructure.hardware_lease import HardwareLease

    monkeypatch.setattr(
        task_runner_module, "_AUTH_PROOF_EVAL_TIMEOUT",
        0.2 if bounded_by == "eval_timeout" else 30.0, raising=False,
    )
    monkeypatch.setattr(
        task_runner_module, "_AUTH_PROOF_CLEANUP_DEADLINE",
        2.0 if bounded_by == "eval_timeout" else 0.3, raising=False,
    )
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    started, release = asyncio.Event(), asyncio.Event()
    cleanup_started, stop_release = asyncio.Event(), asyncio.Event()
    events = []

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        landing.identity_read_gate = started, release
        # stop evaluate hangs until the test releases it (never during the run).
        landing.auth_proof_gate = "station_auth_stop", cleanup_started, stop_release
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=6)
        assert session.status()["busy"] is False
        assert session.status()["run"]["state"] == "cancelled"
        lease = HardwareLease.try_acquire()
        assert lease is not None, "wedged renderer must not hold the hardware lease forever"
        lease.close()
        assert not any(event.get("phase") == "indicator" for event in events)
        assert not page.closed
        assert not landing.closed
        assert not any(page.context.listeners.values())
        await asyncio.sleep(0.05)  # let abandoned stop tasks settle
    finally:
        release.set()
        stop_release.set()
        await _finish_test_task(task)


@pytest.mark.unit
async def test_station_recovery_hung_watch_install_fails_closed_and_repauses(
    tmp_path, monkeypatch,
):
    """A wedged watch-install evaluate must hit the eval timeout and re-pause."""
    import task_runner as task_runner_module

    monkeypatch.setattr(
        task_runner_module, "_AUTH_PROOF_EVAL_TIMEOUT", 0.2, raising=False,
    )
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events, bindings = [], []
    started, release = asyncio.Event(), asyncio.Event()

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)
        if event["event"] == "step_done":
            bindings.append(session.page)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        landing.auth_proof_gate = "station_auth_watch", started, release
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await asyncio.wait_for(started.wait(), timeout=5)
        # Install never returns: recovery must fail closed, not wedge the lease.
        await _identity_pause(task, session, events)
        assert session.status()["run"]["state"] == "paused"
        assert not bindings
        assert not any(event.get("phase") == "indicator" for event in events)
        assert not landing.closed
        session.cancel(session.status()["run"]["id"])
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert session.status()["run"]["state"] == "cancelled"
        assert session.status()["busy"] is False
        assert not any(page.context.listeners.values())
        assert all(not other.auth_proofs for other in page.context.pages)
    finally:
        release.set()
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("candidates", ["one_valid", "none"])
async def test_station_recovery_closed_bound_page_falls_through_to_candidates(
    tmp_path, candidates,
):
    """Closing the wrong-org bound tab during pause must not dead-end recovery."""
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events, bindings = [], []

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)
        if event["event"] == "step_done":
            bindings.append((session._active_runner.page, session.page))

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        if candidates == "none":
            landing.business_identity = "单位乙"
        page.closed = True  # user closes the bound tab during the pause
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        if candidates == "one_valid":
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert bindings
            assert all(pair == (landing, landing) for pair in bindings)
            assert session.page is landing
            assert runner._workbench_page is landing
            assert landing in runner._kept_pages
            assert landing in runner._protected_pages
            assert session.status()["run"]["state"] == "succeeded"
            assert sum(event["event"] == "task_done" for event in events) == 1
            assert not any(event["event"] == "task_error" for event in events)
            assert not landing.closed
            assert not page.clicked
            assert not landing.clicked
            assert not page.gotos
            assert not landing.gotos
        else:
            # Zero valid candidates: auth stays unknown and re-pauses, no crash.
            await _identity_pause(task, session, events)
            assert session.status()["run"]["state"] == "paused"
            assert session._pages == [page]
            assert not bindings
            assert not any(event.get("phase") == "indicator" for event in events)
            session.cancel(session.status()["run"]["id"])
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert session.status()["run"]["state"] == "cancelled"
        assert not any(page.context.listeners.values())
        assert all(not other.auth_proofs for other in page.context.pages)
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
async def test_station_recovery_unavailable_dom_proof_repauses_without_adoption(
    tmp_path, monkeypatch,
):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events = []

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        original = landing.evaluate

        async def unavailable(script, arg=None):
            if isinstance(arg, dict) and arg.get("op") == "station_auth_watch":
                raise RuntimeError("document observation unavailable")
            return await original(script, arg)

        monkeypatch.setattr(landing, "evaluate", unavailable)
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await _identity_pause(task, session, events)
        assert session.page is page
        assert not any(event.get("phase") == "indicator" for event in events)
        monkeypatch.setattr(landing, "evaluate", original)
        await _complete_station_continue(task, session)
        assert session.page is landing
        assert session.status()["run"]["state"] == "succeeded"
        assert not page.closed
        assert not landing.closed
        assert not any(page.context.listeners.values())
        assert all(not other.auth_proofs for other in page.context.pages)
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("finish", ["continue", "cancel"])
@pytest.mark.parametrize("bound_recovers", [False, True])
@pytest.mark.parametrize("change_kind", ["new_tab", "origin_change"])
async def test_manual_tab_opened_during_auth_recovery_does_not_allow_ambiguous_rebound(
    tmp_path, finish, bound_recovers, change_kind,
):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    session._station_org_name = "previous scope"
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events, bindings = [], []
    started, release = asyncio.Event(), asyncio.Event()

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)
        if event["event"] == "step_done":
            bindings.append((session._active_runner.page, session.page))

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        if change_kind == "origin_change":
            twin = await page.context.new_page()
            _workbench(twin, "单位甲")
            twin.url = "https://foreign.example.test/portal/#/powerEnterprise"
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        landing.identity_read_gate = started, release
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await asyncio.wait_for(started.wait(), timeout=5)
        if change_kind == "new_tab":
            twin = await page.context.new_page()
        _workbench(twin, "单位甲")
        if bound_recovers:
            page.business_identity = "单位甲"
        release.set()
        await _identity_pause(task, session, events)
        assert session.page is page and not bindings
        assert not any(event.get("phase") == "indicator" for event in events)
        assert session.status()["busy"] is True
        assert not page.closed and not landing.closed and not twin.closed
        with pytest.raises(RuntimeError, match="占用"):
            await session.select_page("manual-selection-stays-locked")
        if finish == "cancel":
            session.cancel(run["id"])
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert not bindings
            assert session.status()["run"]["state"] == "cancelled"
        else:
            page.business_identity = twin.business_identity = "单位乙"
            landing.identity_read_gate = None
            await _complete_station_continue(task, session)
            assert bindings and all(pair == (landing, landing) for pair in bindings)
            assert session.status()["run"]["state"] == "succeeded"
        assert not page.closed and not landing.closed and not twin.closed
        assert session._station_org_name == "previous scope"
        assert session.status()["busy"] is False
        assert not any(page.context.listeners.values()), "pause/workflow listeners must be removed"
    finally:
        release.set()
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("finish", ["cancel", "continue"])
async def test_manual_auth_pause_tabs_survive_cleanup_without_keeping_automation_tabs(
    tmp_path, finish,
):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    session._station_org_name = "previous scope"
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events, owned = [], []

    def automation_popup():
        popup = _StationPage()
        _workbench(popup, "单位乙")
        popup.url = "https://pm.gd.csg.cn/pfxh/stc/automation"
        popup.context = page.context
        page.context.pages.append(popup)
        owned.append(popup)

    def emit(event):
        events.append(event)
        # Browser I/O fake: task-owned popups before auth and during normal execution.
        if event["event"] in {"station_start", "step_done"}:
            automation_popup()
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        notes = await page.context.new_page()
        notes.url = "https://notes.example.test/manual"
        run = session.status()["run"]
        if finish == "cancel":
            session.cancel(run["id"])
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert not any(event["event"] == "resumed" for event in events)
            assert not any(event.get("phase") == "indicator" for event in events)
            assert session.status()["run"]["state"] == "cancelled"
        else:
            await _complete_station_continue(task, session)
            assert session.page is landing
            assert any(event["event"] == "step_done" for event in events)
            assert session.status()["run"]["state"] == "succeeded"
        assert not page.closed and not landing.closed and not notes.closed
        assert owned and all(popup.closed for popup in owned)
        assert session._station_org_name == "previous scope"
        assert session.status()["busy"] is False
        assert not any(page.context.listeners.values()), "pause/workflow listeners must be removed"
    finally:
        await _finish_test_task(task)


@pytest.mark.unit
@pytest.mark.parametrize("finish", ["cancel", "continue"])
async def test_manual_tabs_survive_login_final_auth_pause_without_bypassing_explicit_page(
    tmp_path, finish,
):
    page = _StationPage()
    session = _locked_station_session(page)
    session._station_org_name = "previous scope"
    configs = {
        "login": {"name": "login", "type": "login", "needs_login": False,
                  "steps": [{"action": "wait", "ms": 1}]},
        "a": {"name": "a", "needs_login": False, "steps": [{"action": "wait", "ms": 1}]},
    }
    runner = _station_task(
        tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}], loader=configs.get,
    )
    events, indicator_bindings = [], []

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)
        if event.get("phase") == "indicator" and event.get("status") == "done":
            indicator_bindings.append(session.page)

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        assert session._active_runner is not runner, "login workflow owns the active pause"
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        notes = await page.context.new_page()
        notes.url = "https://notes.example.test/manual"
        run = session.status()["run"]
        if finish == "cancel":
            session.cancel(run["id"])
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert not any(event["event"] == "resumed" for event in events)
            assert not indicator_bindings
            assert session.status()["run"]["state"] == "cancelled"
        else:
            session.resume(run["id"], run["pause_version"])
            await _identity_pause(task, session, events)
            assert session.page is page and not indicator_bindings
            assert not any(event.get("phase") == "indicator" for event in events)
            _workbench(page, "单位甲")
            await _complete_station_continue(task, session)
            assert indicator_bindings == [page], "explicit auth must not adopt the manual tab"
            assert session.status()["run"]["state"] == "succeeded"
        assert not page.closed and not landing.closed and not notes.closed
        assert session._station_org_name == "previous scope"
        assert session.status()["busy"] is False
        assert not any(page.context.listeners.values()), "pause/workflow listeners must be removed"
    finally:
        await _finish_test_task(task)


@pytest.mark.parametrize("candidate_kind", [
    "foreign_origin", "foreign_scheme", "foreign_context", "ambiguous",
    "wrong_unit", "unreadable", "public", "unrelated", "closed",
])
async def test_station_identity_generic_continue_rejects_unsafe_candidates(
    tmp_path, candidate_kind,
):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events, bindings = [], []

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            session.pauses.put_nowait(None)
        if event["event"] == "step_done":
            bindings.append((session._active_runner.page, session.page))

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session, events)
        if candidate_kind == "foreign_context":
            candidate = _StationPage()
            foreign_context = _StationContext(candidate)
            session._browser.contexts.append(foreign_context)
        else:
            candidate = await page.context.new_page()
        _workbench(candidate, "单位甲")
        if candidate_kind == "foreign_origin":
            candidate.url = "https://pm.gd.csg.cn.evil.test/portal/#/powerEnterprise"
        elif candidate_kind == "foreign_scheme":
            candidate.url = "http://pm.gd.csg.cn/portal/#/powerEnterprise"
        elif candidate_kind == "ambiguous":
            twin = await page.context.new_page()
            _workbench(twin, "单位甲")
        elif candidate_kind == "wrong_unit":
            candidate.business_identity = "单位乙"
            candidate.visible.add("text=单位甲")
        elif candidate_kind == "unreadable":
            candidate.identity_count = 2
        elif candidate_kind == "public":
            candidate.url = "https://pm.gd.csg.cn/portal/#/home"
        elif candidate_kind == "unrelated":
            candidate.url = "https://pm.gd.csg.cn/help/manual"
        elif candidate_kind == "closed":
            candidate.closed = True
        run = session.status()["run"]
        session.resume(run["id"], run["pause_version"])
        await _identity_pause(task, session, events)
        assert session.page is page and runner._shared_pages == [page]
        assert not bindings and not any(event.get("phase") == "indicator" for event in events)
        assert session.status()["busy"] is True
        with pytest.raises(RuntimeError, match="占用"):
            await session.select_page("manual-selection-stays-locked")
        for other in page.context.pages:
            if other is not page:
                other.business_identity = "单位乙"
        landing = await page.context.new_page()
        _workbench(landing, "单位甲")
        await _complete_station_continue(task, session)
        assert bindings and all(pair == (landing, landing) for pair in bindings)
        assert runner._workbench_page is landing and session.page is landing
        assert all(not other.closed for other in page.context.pages
                   if other is not candidate or candidate_kind != "closed")
        assert candidate.closed is (candidate_kind == "closed")
        assert not page.clicked and not candidate.clicked and not landing.clicked
        assert sum(event["event"] == "task_done" for event in events) == 1
        assert not any(event["event"] == "task_error" for event in events)
    finally:
        await _finish_test_task(task)


@pytest.mark.parametrize("call_kind", [
    "explicit_page", "storage_keys", "empty_storage_keys", "public_bound", "unbound", "unscoped",
])
async def test_station_identity_generic_recovery_preserves_caller_binding(tmp_path, call_kind):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _StationSession(page)
    session._station_org_name = "单位甲"
    session.emit = session.events.append
    runner = TaskRunner({"name": "explicit auth"}, download_dir=str(tmp_path))
    runner._shared_session = session
    runner._context = page.context
    runner._shared_pages = [page]
    session._active_runner = runner
    landing = await page.context.new_page()
    _workbench(landing, "单位甲")
    kwargs = {"allow_auto": False}
    if call_kind == "explicit_page":
        kwargs["page"] = page
    elif call_kind in {"storage_keys", "empty_storage_keys"}:
        kwargs["require_storage_keys"] = ["pfxh-token"] if call_kind == "storage_keys" else []
    elif call_kind == "public_bound":
        page.url = "https://pm.gd.csg.cn/portal/#/user/login"
    elif call_kind == "unbound":
        session._pages = []
        session._page_ids = {page: "old", landing: "new"}
    elif call_kind == "unscoped":
        session._station_org_name = ""
        page.visible.clear()
    task = asyncio.create_task(session.require_auth(**kwargs))
    try:
        await _identity_pause(task, session)
        await session.decisions.put(True)
        await _identity_pause(task, session)
        assert session._pages == ([] if call_kind == "unbound" else [page])
        assert runner._shared_pages == [page]
        assert not page.closed and not landing.closed
        await session.decisions.put(False)
        with pytest.raises(RuntimeError, match="用户取消登录检查"):
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
    finally:
        await _finish_test_task(task)


@pytest.mark.parametrize("cancel_source", ["runner", "session"])
async def test_station_identity_no_runnable_cancel_during_preparation_is_terminal(
    tmp_path, monkeypatch, cancel_source,
):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = TaskRunner(
        {"name": "no runnable station", "indicators": ["a", "b"], "indicator_retry": 0,
         "_skipped_stations": [{"name": "场站甲", "status": "skipped", "reason": "无可用证书"}]},
        download_dir=str(tmp_path), station_queue=[],
        workflow_loader=lambda name: {"name": name, "needs_login": True,
                                      "steps": [{"action": "wait", "ms": 1}]},
    )
    activate = page.bring_to_front
    cancelled = False

    async def cancel_during_preparation():
        nonlocal cancelled
        await activate()
        if cancelled:
            return
        cancelled = True
        if cancel_source == "session":
            session.cancel(session.status()["run"]["id"])
        else:
            runner._stopped = True

    monkeypatch.setattr(page, "bring_to_front", cancel_during_preparation)
    events = []
    await asyncio.wait_for(runner.run(events.append, session=session), timeout=5)
    assert cancelled
    assert [event["error"] for event in events if event["event"] == "task_error"] == ["用户取消"]
    assert not any(event["event"] in {"station_start", "step_start", "step_done", "task_done"}
                   for event in events)
    statuses = [(event["indicator"], event["status"]) for event in events
                if event["event"] == "indicator_status"]
    assert statuses == [("a", "skipped"), ("b", "skipped")]
    assert runner._rolled == {"success": 0, "failed": 0, "skipped": 2}
    assert runner._terminal_sent and runner._stopped
    assert session._active_runner is None and session.status()["busy"] is False
    if cancel_source == "session":
        assert session.status()["run"]["state"] == "cancelled"
    assert not page.clicked and not page.closed


async def test_station_identity_preparation_cancel_preserves_runnable_station_report(tmp_path):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _locked_station_session(page)
    runner = _station_task(
        tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"},
                            {"name": "场站乙", "org_name": "单位乙"}],
    )
    events = []

    def emit(event):
        events.append(event)
        if event["event"] == "indicator_plan":
            session.cancel(session.status()["run"]["id"])

    await asyncio.wait_for(runner.run(emit, session=session), timeout=5)
    reports = [event["stations"] for event in events if event["event"] == "station_report"]
    assert reports == [[
        {"name": "场站甲", "status": "skipped", "reason": "任务已停止（用户取消）"},
        {"name": "场站乙", "status": "skipped", "reason": "任务已停止（用户取消）"},
    ]]
    assert [event["error"] for event in events if event["event"] == "task_error"] == ["用户取消"]
    assert not any(
        event["event"] in {"station_start", "step_done", "task_done"} for event in events
    )
    assert session.status()["run"]["state"] == "cancelled"
    assert session.status()["busy"] is False


@pytest.mark.parametrize("identity", ["单位乙", "", "单位"])
async def test_station_identity_blocks_single_station_reuse(tmp_path, identity):
    page = _StationPage()
    _workbench(page, identity)
    page.storage["signed-document"] = "keep"
    session = _StationSession(page)
    original = dict(session._config)
    runner = _station_task(tmp_path, stations=[{"name": "显示名称", "org_name": "单位甲"}])
    events = []
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        await _identity_pause(task, session)
        await session.decisions.put(True)
        await _identity_pause(task, session)
        assert not page.clicked
        assert not any(event.get("phase") == "indicator" for event in events)
        page.business_identity = "单位甲有限公司"
        await session.decisions.put(True)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert any(event["event"] == "task_done" for event in events)
        assert session._config == original
        assert page.storage == {"signed-document": "keep"}
        assert page.reloads == 0
        assert page.gotos == []
        page.business_identity = "单位乙"
        assert (await session.check_auth())["state"] == "authenticated", "scope must not leak"
    finally:
        await _finish_test_task(task)


async def test_station_identity_checks_completed_login(tmp_path, station_io, monkeypatch):
    import spider_gd.infrastructure.windows.cert_dialog as cert

    monkeypatch.setattr(cert, "confirm_certificate", lambda *_args: CertDialogResult(True, "", 0))
    page = _StationPage()
    page.login_identities = ["单位乙"]
    session = _StationSession(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events = []
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        await _identity_pause(task, session)
        assert page.clicked == ["button:has-text('立即登录')"]
        assert not any(event.get("phase") == "indicator" for event in events)
        await session.decisions.put(True)
        await _identity_pause(task, session)
        page.business_identity = "单位甲"
        await session.decisions.put(True)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert any(event["event"] == "task_done" for event in events)
    finally:
        await _finish_test_task(task)


async def test_station_identity_rechecks_before_indicator_steps(tmp_path):
    page = _StationPage()
    _workbench(page, "单位甲")
    session = _StationSession(page)
    runner = _station_task(tmp_path, stations=[{"name": "场站甲", "org_name": "单位甲"}])
    events = []

    def emit(event):
        events.append(event)
        if event.get("phase") == "indicator" and event.get("status") == "running":
            page.business_identity = "单位乙"

    task = asyncio.create_task(runner.run(emit, session=session))
    try:
        await _identity_pause(task, session)
        assert not any(event["event"] == "step_start" for event in events)
        await session.decisions.put(True)
        await _identity_pause(task, session)
        page.business_identity = "单位甲"
        await session.decisions.put(True)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert any(event["event"] == "step_done" for event in events)
    finally:
        await _finish_test_task(task)


async def test_station_identity_rejects_retained_dom_after_cookie_clear(tmp_path):
    page = _StationPage()
    _workbench(page, "单位甲")
    page.storage["pfxh-token"] = "retained-token"
    session = _StationSession(page)
    def load(name):
        if runner._active_station_name == "场站甲" and name == "b":
            return None
        return {"name": name, "needs_login": True, "steps": [{"action": "wait", "ms": 1}]}

    runner = _station_task(
        tmp_path, login="", indicators=["a", "b"],
        stations=[{"name": "场站甲", "org_name": "单位甲"},
                  {"name": "场站乙", "org_name": "单位乙"}], loader=load,
    )
    events = []
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        await _identity_pause(task, session, events)
        started = [event["name"] for event in events if event["event"] == "station_start"]
        assert started == ["场站甲", "场站乙"]
        assert sum(event["event"] == "step_done" for event in events) == 1
        assert page.business_identity == "单位甲"
        assert session._context.cookies == []
        assert page.storage == {"pfxh-token": "retained-token"}
        assert "text=现货交易" in page.visible
        await session.decisions.put(True)
        await _identity_pause(task, session)
        page.business_identity = "单位乙"
        await session.decisions.put(True)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        # 场站甲跑完 a（b 失败），切到场站乙确认单位身份后应独立执行 a 与 b（共 3 次 step_done）
        assert sum(event["event"] == "step_done" for event in events) == 3
        assert page.reloads == 0
        assert page.gotos == []
    finally:
        await _finish_test_task(task)


async def test_station_identity_cancel_stops_queue_and_releases_scope(tmp_path):
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _StationSession(page)
    original = dict(session._config)
    runner = _station_task(
        tmp_path, login="", stations=[{"name": "场站甲", "org_name": "单位甲"},
                                      {"name": "场站乙", "org_name": "单位乙"}],
        loader=lambda name: {"name": name, "needs_login": True,
                             "steps": [{"action": "wait", "ms": 1}]},
    )
    events = []
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        await _identity_pause(task, session, events)
        session._cancel_requested = True
        await session.decisions.put(False)
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        started = [event["name"] for event in events if event["event"] == "station_start"]
        assert started == ["场站甲"]
        assert not any(event["event"] == "step_done" for event in events)
        assert sum(event["event"] == "task_error" for event in events) == 1
        assert session._config == original
        assert (await session.check_auth())["state"] == "authenticated"
    finally:
        await _finish_test_task(task)


@pytest.mark.parametrize(("identity", "count", "visible", "selector"), [
    ("单位乙", 1, True, "#current-business-identity"),
    ("", 1, True, "#current-business-identity"),
    ("单位甲", 0, True, "#current-business-identity"),
    ("单位甲", 2, True, "#current-business-identity"),
    ("单位甲", 1, False, "#current-business-identity"),
    ("单位甲", 1, True, ""),
])
async def test_station_identity_require_auth_rechecks_instead_of_approving(
    identity, count, visible, selector,
):
    page = _StationPage()
    _workbench(page, identity)
    session = _StationSession(page)
    session._station_org_name = "单位甲"
    session._config["identity_selector"] = selector
    session._config["auth_policy"] = "reuse_or_auto"
    session.emit = session.events.append
    page.identity_count, page.identity_visible = count, visible
    task = asyncio.create_task(session.require_auth(allow_auto=True))
    try:
        await _identity_pause(task, session)
        await session.decisions.put(True)
        await _identity_pause(task, session)
        session._config["identity_selector"] = "#current-business-identity"
        page.identity_count, page.identity_visible = 1, True
        page.business_identity = "单位甲"
        await session.decisions.put(True)
        assert await asyncio.wait_for(asyncio.shield(task), timeout=5) is True
    finally:
        await _finish_test_task(task)


@pytest.mark.parametrize(("keyword", "identity", "expected"), [
    ("韶关浈江", "三峡新能源发电（韶关 浈江）有限公司", "authenticated"),
    ("ＡＢ", "AB有限公司", "authenticated"),
    ("单位甲有限公司", "单位甲", "wrong_account"),
    ("单位甲", "单位", "wrong_account"),
    ("甲", "单位甲", "wrong_account"),
    ("单位甲", "单位甲...", "unknown"),
    ("单位甲", "单位甲…", "unknown"),
    ("单位甲", "单位甲︙", "unknown"),
    ("单位甲", "单位甲﹒﹒﹒", "unknown"),
    ("单位甲", "单位甲．．．", "unknown"),
    ("单位甲", "单位甲 …", "unknown"),
    ("单位甲", "单位甲. \u2003. .", "unknown"),
    ("单位甲", "单位甲︙ \u3000", "unknown"),
    ("单位甲", "单位乙", "wrong_account"),
])
async def test_station_identity_uses_normalized_keyword_not_reverse_or_truncated_match(
    keyword, identity, expected,
):
    page = _StationPage()
    _workbench(page, identity)
    page.visible.add("text=单位甲")
    session = _StationSession(page)
    session._station_org_name = keyword
    assert (await session.check_auth())["state"] == expected


@pytest.mark.parametrize(("actual", "expected", "state"), [
    ("单位甲", "单位甲", "authenticated"),
    ("单位甲有限公司", "单位甲", "wrong_account"),
    ("ＡＢ", "AB", "wrong_account"),
])
async def test_station_identity_preserves_exact_global_expected_identity(actual, expected, state):
    page = _StationPage()
    _workbench(page, actual)
    session = _StationSession(page)
    session._station_org_name = "单位甲" if "单位" in actual else "AB"
    session._config["expected_identity"] = expected
    assert (await session.check_auth())["state"] == state


async def test_station_identity_is_required_even_when_global_identity_matches():
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _StationSession(page)
    session._station_org_name = "单位甲"
    session._config["expected_identity"] = "单位乙"
    assert (await session.check_auth())["state"] == "wrong_account"


@pytest.mark.parametrize("private", [True, False])
async def test_station_identity_zero_runnable_plan_does_not_fallback_for_private_tasks(
    tmp_path, private,
):
    from spider_gd.core.station_profile import StationProfile
    from spider_gd.core.ukey_plan import plan_task_execution

    plan = plan_task_execution(
        [StationProfile("a", "显示名称", "", org_name="单位甲")], selected_ids=[],
        present_device_ids=[], certs=[], mode="scheduled",
    )
    assert plan.runnable == []
    page = _StationPage()
    _workbench(page, "单位乙")
    session = _StationSession(page)
    runner = TaskRunner(
        {"name": "fallback", "indicators": ["a"], "indicator_retry": 0,
         "_skipped_stations": [{"name": item.name, "status": item.status, "reason": item.reason}
                               for item in plan.skipped]},
        download_dir=str(tmp_path), station_queue=plan.runnable,
        workflow_loader=lambda name: {"name": name, "needs_login": private,
                                       "steps": [{"action": "wait", "ms": 1}]},
    )
    events = []
    await asyncio.wait_for(runner.run(events.append, session=session), timeout=5)
    assert any(event["event"] == "step_done" for event in events) is not private
    assert any(event["event"] == "task_error" for event in events) is private


def test_shared_hardware_lease_is_exclusive():
    from spider_gd.infrastructure.hardware_lease import HardwareLease, hardware_operation

    with hardware_operation() as owner:
        assert HardwareLease.try_acquire() is None
        with hardware_operation(owner):
            assert HardwareLease.try_acquire() is None
        assert HardwareLease.try_acquire() is None
    with hardware_operation():
        pass
