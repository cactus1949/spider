"""动作诊断只保存指纹，网络请求归属发起时的动作阶段。"""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowRunner


async def test_trace_redacts_credentials_and_keeps_request_start_phase(tmp_path):
    page = MagicMock()
    page.url = "https://pm.gd.csg.cn/pfxh/stc/index?session=private"
    page.is_closed.return_value = False
    page.context.cookies = AsyncMock(return_value=[{
        "name": "VShPPFZUwMakP", "domain": "pm.gd.csg.cn", "path": "/", "value": "cookie-secret",
    }])
    runner = WorkflowRunner({"trace_browser_actions": True}, download_dir=str(tmp_path))
    runner.pages = [page]
    await runner._trace_browser_action("captcha.before_fill")

    request = MagicMock()
    request.url = "https://pm.gd.csg.cn/pfxh/api/getJddjTree?HdKGccbL=signed-secret"
    request.resource_type = "xhr"
    request.method = "GET"
    request.frame.page = page
    runner._on_page_request(request)
    runner._action_phase = "captcha.after_fill"
    response = MagicMock(request=request, status=200, url=request.url,
                         headers={"content-type": "application/json"})
    await runner._capture_page_response(response)

    text = runner._action_trace_file.read_text()
    entries = [json.loads(line) for line in text.splitlines()]
    assert entries[-1]["phase"] == "captcha.before_fill"
    assert entries[-1]["status"] == 200
    assert entries[0]["context"] == entries[-1]["context"]
    assert entries[0]["cookies"][0]["sha256"]
    assert "cookie-secret" not in text
    assert "signed-secret" not in text
    assert "session=private" not in text
    page.evaluate.assert_not_called()


async def test_trace_disabled_does_not_read_cookies_or_write_files(tmp_path):
    page = MagicMock()
    runner = WorkflowRunner({}, download_dir=str(tmp_path))
    runner.pages = [page]
    await runner._trace_browser_action("captcha.before_fill")
    page.context.cookies.assert_not_called()
    assert not runner._action_trace_file.exists()


class _Frame:
    def __init__(self, page):
        self.page = page


class _Request:
    resource_type = "xhr"
    method = "GET"
    failure = "net::ERR_CONNECTION_RESET"

    def __init__(self, frame, path):
        self.frame = frame
        self.url = f"https://pm.gd.csg.cn{path}?token=query-secret#fragment-secret"


class _QueryPage(DomProbeFakePage):
    def __init__(self):
        super().__init__()
        self.url = "https://pm.gd.csg.cn/pfxh/stc/check?session=page-secret"
        self.context = SimpleNamespace(pages=[self], cookies=AsyncMock())
        self.main_frame = _Frame(self)
        self.handlers = {}
        self.observed = asyncio.Event()
        self.clicks = []
        self.download_attempts = 0
        self.download_future = None

    def is_closed(self):
        return False

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)
        if event == "websocket":
            self.observed.set()

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)
        if not self.handlers[event]:
            del self.handlers[event]

    def emit(self, event, value):
        for handler in list(self.handlers.get(event, [])):
            handler(value)

    def probe_visible(self, selector, index=None):
        return selector == "#export"

    def apply_click(self, selector, _index):
        self.clicks.append(selector)
        if selector == "#export" and self.download_future is not None:
            async def save_as(path):
                Path(path).write_bytes(b"date,value\n2026-11-01,1\n")

            self.download_future.set_result(SimpleNamespace(
                suggested_filename="query.csv", save_as=save_as,
            ))

    @asynccontextmanager
    async def expect_download(self, **_kwargs):
        self.download_attempts += 1
        self.download_future = asyncio.get_running_loop().create_future()
        try:
            yield SimpleNamespace(value=self.download_future)
        finally:
            self.download_future = None


@pytest.fixture
def beta_trace_runner(tmp_path, monkeypatch):
    home = tmp_path / "beta"
    home.mkdir()
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    page = _QueryPage()
    page.context.cookies.return_value = []
    runner = WorkflowRunner({"trace_browser_actions": True}, download_dir=str(home))
    runner.pages = [page]
    return runner


@pytest.mark.parametrize("escape", ["directory", "leaf"])
async def test_beta_public_hover_survives_trace_denial_without_touching_foreign_files(
    beta_trace_runner, tmp_path, escape,
):
    runner = beta_trace_runner
    page = runner.page
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / runner._action_trace_file.name
    sentinel.write_bytes(b"foreign trace must stay unchanged\n")
    if escape == "directory":
        link = runner._action_trace_file.parent
        link.symlink_to(foreign, target_is_directory=True)
    else:
        runner._action_trace_file.parent.mkdir()
        link = runner._action_trace_file
        link.symlink_to(sentinel)
    original_target = link.readlink()

    await runner.exec_step({"action": "hover", "selector": "#export", "timeout": 100})

    assert page.probes == [("box", "#export", None)]
    assert len(page.mouse_moves) >= 7
    x, y = page.mouse_moves[-1]
    assert 100 <= x <= 120 and 40 <= y <= 60
    assert page.mouse_events == []
    assert not runner.failed
    assert sentinel.read_bytes() == b"foreign trace must stay unchanged\n"
    assert list(foreign.iterdir()) == [sentinel]
    assert link.is_symlink()
    assert link.readlink() == original_target


async def test_beta_public_hover_writes_action_trace_inside_home(beta_trace_runner):
    runner = beta_trace_runner

    await runner.exec_step({"action": "hover", "selector": "#export", "timeout": 100})

    entries = [json.loads(line) for line in runner._action_trace_file.read_text().splitlines()]
    assert [entry["phase"] for entry in entries] == ["hover.before", "hover.after"]
    assert all(entry["kind"] == "action" and entry["cookies"] == [] for entry in entries)
    assert len(runner.page.mouse_moves) >= 7
    assert runner.page.mouse_events == []
    assert not runner.failed


@pytest.mark.parametrize("resume", [False, True])
async def test_query_pause_logs_request_lifecycle_without_changing_export_guard(tmp_path, resume):
    page = _QueryPage()
    runner = WorkflowRunner({"name": "查询诊断", "page_recovery": False, "steps": [
        {"action": "wait", "ms": 50},
        {"action": "export", "selector": "#export", "timeout": 3000,
         "query_wait_timeout": 0, "query_settle_ms": 0},
    ]}, download_dir=str(tmp_path))
    runner.pages = [page]
    events = []
    paused = asyncio.Event()
    decisions = asyncio.Queue()
    logs = []
    sink = logger.add(lambda message: logs.append(message.record["message"]))

    def emit(event):
        events.append(event)
        if event["event"] == "error_pause":
            paused.set()

    task = asyncio.create_task(runner._execute_steps(emit, decisions.get, shared=True))
    try:
        await asyncio.wait_for(page.observed.wait(), timeout=5)
        page.emit("framenavigated", page.main_frame)
        finished = _Request(page.main_frame, "/pfxh/api/finished")
        finished.url = ("https://diagnostic-user:credential-secret@pm.gd.csg.cn"
                        "/pfxh/api/finished?token=query-secret#fragment-secret")
        old = _Request(page.main_frame, "/pfxh/api/old-document")
        page.emit("request", finished)
        page.emit("requestfinished", finished)
        page.emit("request", old)
        page.emit("framenavigated", page.main_frame)
        current = _Request(page.main_frame, "/pfxh/api/current-document")
        page.emit("request", current)
        other = _QueryPage()
        failed = _Request(other.main_frame, "/pfxh/api/other-tab")
        failed.resource_type = "document"
        page.emit("request", failed)
        page.emit("requestfailed", failed)
        await asyncio.wait_for(paused.wait(), timeout=5)

        pause = next(event for event in events if event["event"] == "error_pause")
        assert "当前页的同源查询请求在途" in pause["message"], pause["message"]
        lines = [line for line in logs if line.startswith("[query-diagnostics] ")]
        assert len(lines) == 1, "查询暂停缺少请求生命周期诊断"
        report = json.loads(lines[0].split(" ", 1)[1])
        assert report["pending_count"] == 2, "不能为了诊断丢弃旧文档的在途请求"
        pending = {item["path"]: item for item in report["pending"]}
        assert set(pending) == {"/pfxh/api/old-document", "/pfxh/api/current-document"}
        assert pending["/pfxh/api/old-document"]["observed_document_generation"] == 1
        assert pending["/pfxh/api/current-document"]["observed_document_generation"] == 2
        frames = {item["frame_id"]: item for item in report["frames"]}
        assert frames[str(id(page.main_frame))]["observed_document_generation"] == 2
        assert all(item["age_ms"] >= 0 and item["frame_id"] for item in pending.values())
        assert all(item["page_id"] == str(id(page)) for item in pending.values())
        assert all(item["observed"] is True for item in pending.values())
        finished_events = [item for item in report["recent"]
                           if item["path"] == "/pfxh/api/finished"]
        assert [item["state"] for item in finished_events] == ["started", "finished"]
        assert finished_events[0]["request_id"] == finished_events[1]["request_id"]
        assert finished_events[1]["duration_ms"] >= 0
        assert all(item["at_ms"] > 0 for item in report["recent"])
        assert finished_events[0]["at_ms"] <= finished_events[1]["at_ms"]
        assert all(item["started_at_ms"] > 0 for item in pending.values())
        failed_events = [item for item in report["recent"] if item["path"] == "/pfxh/api/other-tab"]
        assert [item["state"] for item in failed_events] == ["started", "failed"]
        assert failed_events[-1]["page_id"] != str(id(page))
        assert not page.clicks
        assert page.download_attempts == 0
        assert not any(event["event"] == "file_result" for event in events)
        assert not any(secret in lines[0] for secret in (
            "query-secret", "fragment-secret", "page-secret",
            "credential-secret", "diagnostic-user",
        ))
        page.context.cookies.assert_not_awaited()
        assert not runner._action_trace_file.exists()
        if resume:
            page.emit("requestfinished", old)
            page.emit("requestfinished", current)
        await decisions.put(resume)
        await asyncio.wait_for(task, timeout=5)
        if resume:
            assert page.clicks == ["#export"]
            assert page.download_attempts == 1
            assert (tmp_path / "query.csv").read_bytes() == b"date,value\n2026-11-01,1\n"
            assert sum(event["event"] == "export_saved" for event in events) == 1
            assert not runner.failed
        else:
            assert not page.clicks
            assert page.download_attempts == 0
            assert runner._stopped
    finally:
        logger.remove(sink)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_export_call_chain_accepts_validated_new_document_without_replaying_clicks(tmp_path):
    from datetime import date

    page = _QueryPage()
    runner = WorkflowRunner({"name": "current result", "steps": [
        {"action": "wait", "ms": 50},
        {"action": "export", "selector": "#export", "timeout": 1000,
         "query_wait_timeout": 0, "query_settle_ms": 0},
    ], "query_result": {"url": "/pfxh/api/query", "params": {"date": "{date}"},
                        "required_fields": ["price"]}},
        download_dir=str(tmp_path), target_date=date(2026, 10, 1))
    runner.pages = [page]
    events = []
    task = asyncio.create_task(runner._execute_steps(events.append, None, shared=True))

    async def response(request, status, body):
        async def read():
            return body
        await runner._capture_page_response(SimpleNamespace(
            request=request, url=request.url, status=status,
            headers={"content-type": "text/html" if status == 400 else "application/json"},
            body=read,
        ))

    try:
        await asyncio.wait_for(page.observed.wait(), timeout=5)
        old = _Request(page.main_frame, "/pfxh/api/query")
        old.url = old.url.split("#")[0] + "&date=2026-10-01"
        page.emit("request", old)
        await response(old, 400, b"\r\n")
        page.emit("requestfinished", old)
        navigation = _Request(page.main_frame, "/pfxh/stc/new-document")
        navigation.resource_type = "document"
        page.emit("request", navigation)
        await response(navigation, 200, b"{}")
        page.emit("framenavigated", page.main_frame)
        page.emit("requestfinished", navigation)
        current = _Request(page.main_frame, "/pfxh/api/query")
        current.url = current.url.split("#")[0] + "&date=2026-10-01"
        page.emit("request", current)
        page.emit("requestfinished", current)
        await response(current, 200, b'{"code":200,"data":[{"price":42}]}')
        await asyncio.wait_for(task, timeout=5)
        assert not runner.failed, runner.failure_reason
        assert page.clicks == ["#export"]
        assert page.download_attempts == 1
        assert (tmp_path / "query.csv").read_bytes() == b"date,value\n2026-11-01,1\n"
        assert not any(event["event"] == "error_pause" for event in events)
        assert sum(event["event"] == "export_saved" for event in events) == 1
        assert events[-1]["event"] == "workflow_done"
        assert len(runner._page_errors) == 1
        assert not runner._page_errors[0].get("_resolved")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_query_diagnostics_bound_history_and_mark_unobserved_requests(tmp_path):
    page = _QueryPage()
    runner = WorkflowRunner({"name": "有界查询诊断", "steps": [
        {"action": "wait", "ms": 50},
        {"action": "export", "selector": "#export", "query_wait_timeout": 0},
    ]}, download_dir=str(tmp_path))
    runner.pages = [page]
    paused = asyncio.Event()
    decisions = asyncio.Queue()
    logs = []
    sink = logger.add(lambda message: logs.append(message.record["message"]))

    def emit(event):
        if event["event"] == "error_pause":
            paused.set()

    task = asyncio.create_task(runner._execute_steps(emit, decisions.get, shared=True))
    try:
        await asyncio.wait_for(page.observed.wait(), timeout=5)
        frames = [_Frame(page) for _ in range(65)]
        for frame in frames:
            page.emit("framenavigated", frame)
        requests = [_Request(frames[0], f"/pfxh/api/query-{index}") for index in range(129)]
        requests[-1].frame = frames[-1]
        for request in requests:
            page.emit("request", request)
        await asyncio.wait_for(paused.wait(), timeout=5)
        lines = [line for line in logs if line.startswith("[query-diagnostics] ")]
        report = json.loads(lines[0].split(" ", 1)[1])
        assert report["pending_count"] == 129
        assert len(report["pending"]) == 128
        assert report["pending_truncated"] == 1
        paused.clear()
        page.emit("requestfinished", requests[1])
        await decisions.put(True)
        await asyncio.wait_for(paused.wait(), timeout=5)
        lines = [line for line in logs if line.startswith("[query-diagnostics] ")]
        assert len(lines) == 2
        report = json.loads(lines[1].split(" ", 1)[1])
        assert report["pending_count"] == 128
        assert len(report["pending"]) == 128
        assert report["metadata_dropped"] == 1
        assert len(report["recent"]) == 40
        assert report["history_dropped"] > 0
        assert report["frames_truncated"] is True
        pending = {item["path"]: item for item in report["pending"]}
        assert pending["/pfxh/api/query-0"]["observed"] is False
        assert pending["/pfxh/api/query-0"]["request_id"] is None
        assert pending["/pfxh/api/query-0"]["age_ms"] is None
        assert pending["/pfxh/api/query-128"]["observed_document_generation"] is None
        assert not page.clicks
        assert page.download_attempts == 0
        await decisions.put(False)
        await asyncio.wait_for(task, timeout=5)
        assert not page.handlers
        assert not runner._query_request_metadata
        assert not runner._query_frame_generations
    finally:
        logger.remove(sink)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_transient_requestfinished_mount_failure_recovers_without_false_query_pause(tmp_path):
    class TransientPage(_QueryPage):
        def __init__(self):
            super().__init__()
            self.query = _Request(self.main_frame, "/pfxh/api/query")
            self.finish_mount_failed = False
            self.listener_counts = None

        def on(self, event, handler):
            if event == "requestfinished" and not self.finish_mount_failed:
                self.finish_mount_failed = True
                self.emit("request", self.query)
                raise RuntimeError("首次完成事件监听注册失败")
            super().on(event, handler)
            if event == "requestfinished":
                self.listener_counts = (
                    len(self.handlers["request"]), len(self.handlers["requestfinished"]),
                )
                self.emit("requestfinished", self.query)

    page = TransientPage()
    runner = WorkflowRunner({"name": "监听器补挂", "page_recovery": False, "steps": [
        {"action": "wait", "ms": 1},
        {"action": "export", "selector": "#export", "timeout": 3000,
         "query_wait_timeout": 0, "query_settle_ms": 0},
    ]}, download_dir=str(tmp_path))
    runner.pages = [page]
    events = []

    async def cancel_pause():
        return False

    await runner._execute_steps(events.append, cancel_pause, shared=True)
    assert not any(event["event"] == "error_pause" for event in events), "已完成查询不能误暂停"
    assert page.listener_counts == (1, 1)
    assert page.clicks == ["#export"]
    assert (tmp_path / "query.csv").read_bytes() == b"date,value\n2026-11-01,1\n"
    assert not page.handlers
    assert not runner._query_request_metadata


async def test_partial_observer_mount_does_not_leak_or_duplicate_listeners(tmp_path):
    class PartialPage(_QueryPage):
        def on(self, event, handler):
            if event == "framenavigated":
                raise RuntimeError("页面监听器注册失败")
            return super().on(event, handler)

    page = PartialPage()
    runner = WorkflowRunner({"name": "监听器清理", "steps": [
        {"action": "wait", "ms": 1},
    ]}, download_dir=str(tmp_path))
    runner.pages = [page]
    events = []
    await runner._execute_steps(events.append, None, shared=True)
    assert not runner.failed
    assert not page.handlers, "部分挂载的监听器也必须在终态移除"
    page.emit("request", _Request(page.main_frame, "/pfxh/api/after-run"))
    assert not runner._query_request_metadata
    assert not runner._query_request_history
