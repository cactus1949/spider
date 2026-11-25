"""导出没等到 download 事件时，要写出请求状态、弹窗或空结果，而不是只报 60 秒超时。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import (
    WorkflowDateExportFailed,
    WorkflowExecutionError,
    WorkflowPaused,
    WorkflowRunner,
    diagnose_export_miss,
    render_save_as,
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


def test_unrelated_http0_does_not_fail_the_export_while_download_is_still_open():
    heartbeat = {
        "status": 0,
        "resource_type": "xhr",
        "content_type": "",
        "url": "https://pm.gd.csg.cn/pfjs/qctc/getSwitch",
        "failure": "net::ERR_ABORTED",
    }
    assert diagnose_export_miss([heartbeat], "") is None
    timed_out = diagnose_export_miss(
        [heartbeat], "", timeout_error="Timeout 60000ms exceeded",
    )
    assert timed_out["action"] == "fail"
    assert "HTTP 0" in timed_out["reason"]


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


class _QueryFrame:
    def __init__(self, page):
        self.page = page


class _QueryRequest:
    resource_type = "xhr"
    method = "GET"
    failure = "net::ERR_CONNECTION_RESET"
    post_data = None

    def __init__(self, page, path="/query?date=2026-10-01"):
        from urllib.parse import urlsplit

        self.frame = page.main_frame
        origin = urlsplit(page.url)
        self.url = f"{origin.scheme}://{origin.netloc}" + path


class _QueryPage:
    url = "https://example.invalid/report"

    def __init__(self, result=None):
        self.main_frame = _QueryFrame(self)
        self.result = result
        self.arguments = []
        self.clicks = []
        self.handlers = {}

    def is_closed(self):
        return False

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)
        if not self.handlers[event]:
            del self.handlers[event]

    def emit(self, event, value):
        for handler in list(self.handlers.get(event, [])):
            handler(value)

    async def evaluate(self, script, arg=None):
        if isinstance(arg, dict) and "jsonBody" in arg:
            self.arguments.append(arg)
            return self.result
        return []


class _Clock:
    def __init__(self, monkeypatch):
        self.now = 0.0
        self.on_sleep = lambda: None
        monkeypatch.setattr("workflow_runner.time.monotonic", lambda: self.now)
        monkeypatch.setattr("workflow_runner.asyncio.sleep", self.sleep)

    async def sleep(self, seconds):
        import inspect

        self.now += seconds
        result = self.on_sleep()
        if inspect.isawaitable(result):
            await result


def _query_runner(tmp_path, *, contract=True):
    config = {"name": "query export", "steps": []}
    if contract:
        config["query_result"] = {
            "url": "/query", "method": "GET", "params": {"date": "{date}"},
            "response_path": "data", "required_fields": ["price"],
        }
    runner = WorkflowRunner(config, download_dir=str(tmp_path), target_date=date(2026, 10, 1))
    runner.pages = [_QueryPage()]
    runner._executing_step_index = 0
    return runner


async def _query_response(runner, request, payload, *, status=200):
    import json
    from types import SimpleNamespace

    async def body():
        return json.dumps(payload).encode()

    await runner._capture_page_response(SimpleNamespace(
        request=request, url=request.url, status=status,
        headers={"content-type": "application/json"}, body=body,
    ))


async def _navigate_document(runner, *, page=None):
    page = page or runner.page
    navigation = _QueryRequest(page, "/report")
    navigation.resource_type = "document"
    runner._on_page_request(navigation)
    await _query_response(runner, navigation, {})
    runner._on_page_frame_navigated(page.main_frame)
    runner._on_page_request_done(navigation)


async def _current_success(runner, path="/query?date=2026-10-01", payload=None, *, frame=None, page=None):
    request = _QueryRequest(page or runner.page, path)
    if frame is not None:
        request.frame = frame
    runner._on_page_request(request)
    # Playwright may dispatch requestfinished before the response capture task runs.
    runner._on_page_request_done(request)
    await _query_response(runner, request, payload or {"code": 200, "data": [{"price": 42}]})
    return request


async def test_old_document_error_superseded_only_by_validated_current_query(tmp_path):
    runner = _query_runner(tmp_path)
    old = _QueryRequest(runner.page)
    runner._on_page_request(old)
    await _navigate_document(runner)
    runner._on_page_request_failed(old)
    assert runner._export_blocking_errors()
    await _current_success(runner)
    await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    assert not runner._export_blocking_errors()
    assert len(runner._page_errors) == 1
    assert not runner._page_errors[0].get("_resolved")
    assert any(item["state"] == "failed" for item in runner._query_request_history)


@pytest.mark.parametrize("proof", ["transport_only", "wrong_date", "business_failure", "wrong_shape"])
async def test_old_document_failure_cannot_be_replaced_by_unvalidated_query(tmp_path, proof):
    runner = _query_runner(tmp_path)
    old = _QueryRequest(runner.page)
    runner._on_page_request(old)
    await _navigate_document(runner)
    runner._on_page_request_failed(old)
    if proof == "transport_only":
        current = _QueryRequest(runner.page)
        runner._on_page_request(current)
        runner._on_page_request_done(current)
    else:
        payload = {"code": 500, "data": [{"price": 42}]} if proof == "business_failure" else (
            {"code": 200, "data": [{"other": 42}]} if proof == "wrong_shape" else None
        )
        await _current_success(runner, "/query?date=2026-09-01" if proof == "wrong_date" else "/query?date=2026-10-01", payload)
    with pytest.raises(WorkflowPaused):
        await runner._do_export({"selector": "#export", "query_settle_ms": 0, "query_wait_timeout": 0})
    assert runner.page.clicks == []
    assert runner._page_errors[0] in runner._export_blocking_errors()


@pytest.mark.parametrize("correct_frame", ["child", "main"])
async def test_export_business_proof_belongs_to_main_export_document(tmp_path, correct_frame):
    runner = _query_runner(tmp_path)
    child = _QueryFrame(runner.page)
    await _current_success(runner, "/query?date=" + (
        "2026-10-01" if correct_frame == "main" else "2026-09-01"
    ))
    # A child response is newer, but the export DOM probe runs in page's main document.
    await _current_success(runner, "/query?date=" + (
        "2026-10-01" if correct_frame == "child" else "2026-09-01"
    ), frame=child)
    if correct_frame == "child":
        with pytest.raises(WorkflowPaused):
            await runner._pause_until_export_ready(timeout=0, settle_ms=0)
        assert runner.page.clicks == []
    else:
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)


class _DeferredQueryResponse:
    status = 200
    headers = {"content-type": "application/json"}

    def __init__(self, request, payload):
        self.request = request
        self.url = request.url
        self.payload = json.dumps(payload).encode()
        self.reading = asyncio.Event()
        self.completed = asyncio.Event()
        self.cancelled = False
        self.read_count = 0

    async def body(self):
        self.read_count += 1
        self.reading.set()
        try:
            await self.completed.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self.payload

    def finish(self, page):
        page.emit("requestfinished", self.request)
        self.completed.set()


@pytest.mark.parametrize("completion", ["valid", "valid_evicted", "invalid", "incomplete", "failed"])
async def test_response_headers_wait_for_real_body_completion_within_query_budget(tmp_path, completion):
    runner = _query_runner(tmp_path)
    page = runner.page
    runner._observe_page(page)
    request = _QueryRequest(page)
    response = _DeferredQueryResponse(request, {
        "code": 500 if completion == "invalid" else 200, "data": [{"price": 42}],
    })
    page.emit("request", request)
    page.emit("response", response)  # Playwright response means headers, NOT a complete body.
    await asyncio.wait_for(response.reading.wait(), timeout=1)
    assert runner.page.clicks == []
    assert not runner._page_errors
    timer = None
    try:
        if completion in ("valid", "valid_evicted"):
            if completion == "valid_evicted":
                _emit_switch_polls(runner, 256)
                assert request not in runner._query_request_metadata
            timer = asyncio.get_running_loop().call_later(2.2, response.finish, page)
            await runner._pause_until_export_ready(timeout=3500, settle_ms=0)
            assert response.completed.is_set()
            assert not response.cancelled
            assert not runner._page_errors
        else:
            if completion == "invalid":
                response.finish(page)
            elif completion == "failed":
                page.emit("requestfailed", request)
            else:
                # Even requestfinished cannot authorize an unread business body.
                page.emit("requestfinished", request)
            started = asyncio.get_running_loop().time()
            with pytest.raises(WorkflowPaused):
                await runner._pause_until_export_ready(timeout=100, settle_ms=0)
            if completion == "incomplete":
                assert asyncio.get_running_loop().time() - started < 0.5
                assert not runner._page_errors
                assert not response.cancelled
            elif completion == "invalid":
                assert len(runner._page_errors) == 1
                assert "业务结果未通过校验" in runner._page_errors[0]["failure"]
            else:
                assert len(runner._page_errors) == 1
                assert runner._page_errors[0]["status"] == 0
            assert runner.page.clicks == []
    finally:
        if timer is not None:
            timer.cancel()
        runner._release_page_observers()
        pending = list(runner._response_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("body_valid", [True, False])
async def test_unread_same_document_body_holds_export_even_after_newer_query_success(tmp_path, body_valid):
    runner = _query_runner(tmp_path)
    page = runner.page
    runner._observe_page(page)
    older = _QueryRequest(page)
    response = _DeferredQueryResponse(older, {
        "code": 200 if body_valid else 500, "data": [{"price": 42}],
    })
    page.emit("request", older)
    page.emit("response", response)
    await asyncio.wait_for(response.reading.wait(), timeout=1)
    page.emit("requestfinished", older)
    await _current_success(runner)
    try:
        with pytest.raises(WorkflowPaused):
            await runner._pause_until_export_ready(timeout=100, settle_ms=0)
        assert not runner._page_errors
        assert not response.cancelled
        response.completed.set()
        if body_valid:
            await runner._pause_until_export_ready(timeout=1000, settle_ms=0)
        else:
            with pytest.raises(WorkflowPaused):
                await runner._pause_until_export_ready(timeout=1000, settle_ms=0)
            assert len(runner._page_errors) == 1
    finally:
        runner._release_page_observers()
        pending = list(runner._response_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("owner", ["old_document", "other_page"])
async def test_unread_body_keeps_document_supersession_and_other_page_export_scope(tmp_path, owner):
    runner = _query_runner(tmp_path)
    page = runner.page if owner == "old_document" else _QueryPage()
    runner._observe_page(page)
    request = _QueryRequest(page)
    response = _DeferredQueryResponse(request, {"code": 500, "data": [{"price": 42}]})
    page.emit("request", request)
    page.emit("response", response)
    await asyncio.wait_for(response.reading.wait(), timeout=1)
    page.emit("requestfinished", request)
    if owner == "old_document":
        await _navigate_document(runner)
    await _current_success(runner)
    try:
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
        response.completed.set()
        await runner._drain_response_captures(timeout=1)
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
        assert len(runner._page_errors) == 1
        assert not runner._page_errors[0].get("_resolved")
    finally:
        runner._release_page_observers()
        pending = list(runner._response_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_same_document_error_is_not_cleared_by_continue_or_later_success(tmp_path):
    runner = _query_runner(tmp_path)
    old = _QueryRequest(runner.page)
    runner._on_page_request(old)
    runner._on_page_request_failed(old)
    await _current_success(runner)
    pauses = []

    async def resume():
        pauses.append(True)
        return len(pauses) == 1

    runner._wait_continue = resume
    with pytest.raises(WorkflowPaused):
        await runner._do_export({"selector": "#export", "query_settle_ms": 0})
    assert len(pauses) == 2
    assert not runner._page_errors[0].get("_resolved")
    assert runner.page.clicks == []


async def test_same_document_frame_navigation_is_not_a_new_document_success_proof(tmp_path):
    runner = _query_runner(tmp_path)
    old = _QueryRequest(runner.page)
    runner._on_page_request(old)
    runner._on_page_request_failed(old)
    # Playwright also emits framenavigated for history/hash navigation in the same document.
    runner._on_page_frame_navigated(runner.page.main_frame)
    await _current_success(runner)
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    assert old not in runner._inflight
    assert not runner._page_errors[0].get("_resolved")


async def test_observer_mount_failure_fails_closed_export_authorization(tmp_path):
    runner = _query_runner(tmp_path)
    page = runner.page
    await _current_success(runner)
    original_on = page.on

    def failing_on(event, handler):
        if event == "framenavigated":
            raise RuntimeError("listener attach failed")
        original_on(event, handler)

    page.on = failing_on
    runner._observe_page(page)
    assert runner._query_safety_lost is True
    # Real cross-document navigation; the new document issues no declared query.
    # framenavigated has no handler, so only the sticky loss can fail this closed.
    navigation = _QueryRequest(page, "/report")
    navigation.resource_type = "document"
    page.emit("request", navigation)
    page.emit("framenavigated", page.main_frame)
    page.emit("requestfinished", navigation)
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    assert runner.page.clicks == []


async def test_late_failed_document_does_not_clear_newer_navigation_pending(tmp_path):
    runner = _query_runner(tmp_path)
    page = runner.page
    await _current_success(runner)
    first = _QueryRequest(page, "/report")
    first.resource_type = "document"
    second = _QueryRequest(page, "/report")
    second.resource_type = "document"
    runner._on_page_request(first)
    runner._on_page_request(second)
    # The older document's late failure must not discard the newer one's marker.
    runner._on_page_request_failed(first)
    runner._on_page_frame_navigated(page.main_frame)
    runner._on_page_request_done(second)
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    assert runner.page.clicks == []


async def test_closed_page_frames_are_evicted_from_generation_tracking(tmp_path):
    runner = _query_runner(tmp_path)
    page = runner.page
    for _ in range(64):
        request = _QueryRequest(page)
        request.frame = _QueryFrame(page)
        runner._on_page_request(request)
        runner._on_page_request_done(request)
    assert len(runner._query_frame_generations) == 64
    extra = _QueryFrame(page)
    request = _QueryRequest(page)
    request.frame = extra
    runner._on_page_request(request)
    assert extra not in runner._query_frame_generations
    runner._on_page_close(page)
    assert not runner._query_frame_generations
    assert not runner._query_frame_safety
    fresh = _QueryFrame(page)
    request = _QueryRequest(page)
    request.frame = fresh
    runner._on_page_request(request)
    assert fresh in runner._query_frame_generations


async def test_resume_renews_query_wait_budget_without_reclicking(tmp_path, monkeypatch):
    runner = _query_runner(tmp_path, contract=False)
    clock = _Clock(monkeypatch)
    request = _QueryRequest(runner.page)
    runner._on_page_request(request)
    pauses = []

    async def resume():
        pauses.append(clock.now)
        clock.now += 20  # time spent in manual intervention is not a waiting budget
        clock.on_sleep = lambda: runner._on_page_request_done(request)
        return len(pauses) == 1

    runner._wait_continue = resume
    await runner._pause_until_export_ready(timeout=100, settle_ms=0)
    assert len(pauses) == 1
    assert not runner._inflight_query_requests()
    assert runner.page.clicks == []


async def test_resume_waits_for_new_document_business_proof_without_clearing_old_error(tmp_path, monkeypatch):
    runner = _query_runner(tmp_path)
    clock = _Clock(monkeypatch)
    old = _QueryRequest(runner.page)
    runner._on_page_request(old)
    runner._on_page_request_failed(old)
    pauses = []

    async def resume():
        pauses.append(True)
        if len(pauses) > 1:
            return False
        clock.now += 20
        await _navigate_document(runner)
        current = _QueryRequest(runner.page)
        runner._on_page_request(current)
        assert runner._page_errors[0] in runner._export_blocking_errors()

        async def finish():
            runner._on_page_request_done(current)
            await _query_response(runner, current, {"code": 200, "data": [{"price": 42}]})

        clock.on_sleep = finish
        return True

    runner._wait_continue = resume
    await runner._pause_until_export_ready(timeout=1000, settle_ms=0)
    assert len(pauses) == 1
    assert len(runner._page_errors) == 1
    assert not runner._page_errors[0].get("_resolved")
    assert runner.page.clicks == []


async def test_resumed_new_document_body_uses_fresh_budget_after_requestfinished(tmp_path):
    runner = _query_runner(tmp_path)
    page = runner.page
    runner._observe_page(page)
    old = _QueryRequest(page)
    page.emit("request", old)
    page.emit("requestfailed", old)
    waits = []
    timer = None

    async def resume():
        nonlocal timer
        waits.append(True)
        if len(waits) > 1:
            return False
        await _navigate_document(runner)
        current = _QueryRequest(page)
        response = _DeferredQueryResponse(current, {"code": 200, "data": [{"price": 42}]})
        page.emit("request", current)
        page.emit("response", response)
        await asyncio.wait_for(response.reading.wait(), timeout=1)
        page.emit("requestfinished", current)
        timer = asyncio.get_running_loop().call_later(0.15, response.completed.set)
        return True

    runner._wait_continue = resume
    try:
        await runner._pause_until_export_ready(timeout=500, settle_ms=0)
        assert len(waits) == 1
        assert len(runner._page_errors) == 1
        assert not runner._page_errors[0].get("_resolved")
        assert page.clicks == []
    finally:
        if timer is not None:
            timer.cancel()
        runner._release_page_observers()
        pending = list(runner._response_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_only_declared_switch_polling_not_arbitrary_business_paths_is_excluded(tmp_path):
    runner = _query_runner(tmp_path, contract=False)
    polling = _QueryRequest(runner.page, "/pfjs/qctc/systemSwitch/getSwitch")
    unknown = _QueryRequest(runner.page, "/heartbeat")
    similarly_named = _QueryRequest(runner.page, "/business/getSwitch")
    for request in (polling, unknown, similarly_named):
        runner._on_page_request(request)
    assert set(runner._inflight_query_requests()) == {unknown, similarly_named}
    assert polling in runner._inflight
    assert len(runner._query_request_history) == 3


async def test_old_document_pending_query_excluded_after_current_validation_only(tmp_path):
    runner = _query_runner(tmp_path)
    old = _QueryRequest(runner.page)
    runner._on_page_request(old)
    await _navigate_document(runner)
    assert runner._inflight_query_requests() == [old]
    await _current_success(runner)
    await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    assert old in runner._inflight
    assert not runner._inflight_query_requests()


def _emit_switch_polls(runner, count):
    for _ in range(count):
        request = _QueryRequest(runner.page, "/poll/systemSwitch/getSwitch")
        runner._on_page_request(request)
        runner._on_page_request_done(request)


@pytest.mark.parametrize("polls", [127, 256])
async def test_diagnostic_eviction_cannot_resurrect_superseded_query_error(tmp_path, polls):
    runner = _query_runner(tmp_path)
    old = _QueryRequest(runner.page)
    runner._on_page_request(old)
    runner._on_page_request_failed(old)
    await _navigate_document(runner)
    await _current_success(runner)
    await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    _emit_switch_polls(runner, polls)
    assert len(runner._query_request_metadata) <= 128
    assert len(runner._query_request_history) <= 40
    await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    assert not runner._unacknowledged_page_errors()
    assert len(runner._page_errors) == 1
    assert not runner._page_errors[0].get("_resolved")
    # New endpoint traffic must still invalidate prior proof, even after truncation.
    newest = _QueryRequest(runner.page, "/query?date=2026-09-01")
    runner._on_page_request(newest)
    runner._on_page_request_done(newest)
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)


async def test_active_declared_query_cannot_become_polling_after_diagnostic_eviction(tmp_path):
    runner = _query_runner(tmp_path)
    runner.config["query_result"]["url"] = "/query/systemSwitch/getSwitch"
    await _current_success(runner, "/query/systemSwitch/getSwitch?date=2026-10-01")
    active = _QueryRequest(runner.page, "/query/systemSwitch/getSwitch?date=2026-10-01")
    runner._on_page_request(active)
    _emit_switch_polls(runner, 256)
    assert active not in runner._query_request_metadata
    assert runner._inflight_query_requests() == [active]
    assert await runner._wait_network_quiet(idle_ms=0, timeout=0) is False
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    runner._on_page_request_done(active)
    await _query_response(runner, active, {"code": 200, "data": [{"price": 42}]})
    await runner._pause_until_export_ready(timeout=0, settle_ms=0)


async def test_network_quiet_waits_for_completion_not_constant_request_count(tmp_path, monkeypatch):
    runner = _query_runner(tmp_path, contract=False)
    clock = _Clock(monkeypatch)
    request = _QueryRequest(runner.page)
    runner._on_page_request(request)

    def finish():
        if clock.now >= 0.4:
            runner._on_page_request_done(request)

    clock.on_sleep = finish
    assert await runner._wait_network_quiet(idle_ms=100, timeout=1000) is True
    assert clock.now >= 0.4
    assert request not in runner._inflight


async def test_network_quiet_timeout_is_not_reported_as_completion(tmp_path, monkeypatch):
    runner = _query_runner(tmp_path, contract=False)
    clock = _Clock(monkeypatch)
    request = _QueryRequest(runner.page)
    runner._on_page_request(request)
    assert await runner._wait_network_quiet(idle_ms=0, timeout=200) is False
    assert clock.now >= 0.2
    assert request in runner._inflight


async def test_current_document_navigation_blocks_export_even_after_prior_query_success(tmp_path):
    runner = _query_runner(tmp_path)
    await _current_success(runner)
    navigation = _QueryRequest(runner.page, "/report")
    navigation.resource_type = "document"
    runner._on_page_request(navigation)
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    assert navigation in runner._inflight


async def test_missing_document_generation_never_proves_current_query_success(tmp_path):
    runner = _query_runner(tmp_path)
    for _ in range(64):
        runner._on_page_frame_navigated(_QueryFrame(runner.page))
    await _current_success(runner)
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)


async def test_newer_query_without_valid_response_invalidates_prior_current_result(tmp_path):
    runner = _query_runner(tmp_path)
    await _current_success(runner)
    newest = _QueryRequest(runner.page)
    runner._on_page_request(newest)
    runner._on_page_request_done(newest)
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)


async def test_network_quiet_is_not_delayed_by_other_tab(tmp_path, monkeypatch):
    runner = _query_runner(tmp_path, contract=False)
    clock = _Clock(monkeypatch)
    other = _QueryPage()
    request = _QueryRequest(other)
    runner._on_page_request(request)
    assert await runner._wait_network_quiet(idle_ms=0, timeout=1000) is True
    assert clock.now == 0
    assert request in runner._inflight


class _IdentityLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    def nth(self, _index):
        return self

    async def count(self):
        return 1 if self.selector == "#auth" else self.page.identity_count

    async def is_visible(self):
        return self.selector == "#auth" or self.page.identity_visible

    async def inner_text(self, **_kwargs):
        self.page.identity_reads += 1
        return self.page.business_identity


class _QueryContext:
    def __init__(self):
        self.pages = []
        self.handlers = {}

    on = _QueryPage.on
    remove_listener = _QueryPage.remove_listener
    emit = _QueryPage.emit


class _InitialNavigationRequest(_QueryRequest):
    resource_type = "document"
    page_available = False

    @property
    def frame(self):
        if not self.page_available:
            raise RuntimeError("Initial popup frame not available before page event")
        return self._frame

    @frame.setter
    def frame(self, value):
        self._frame = value

    def is_navigation_request(self):
        return True


class _ExportPage(_QueryPage, DomProbeFakePage):
    """Real export probes, mouse input, download and page/network listener boundaries."""

    def __init__(self, context=None, opener=None):
        _QueryPage.__init__(self)
        DomProbeFakePage.__init__(self)
        self.context = context or _QueryContext()
        self.context.pages.append(self)
        self._opener = opener
        self.closed = False
        self.observed = asyncio.Event()
        self.download_attempts = 0
        self.download_future = None
        self.business_identity = "单位甲"
        self.identity_count = 1
        self.identity_visible = True
        self.identity_reads = 0

    def is_closed(self):
        return self.closed

    def on(self, event, handler):
        super().on(event, handler)
        if event == "websocket":
            self.observed.set()

    async def opener(self):
        return self._opener

    async def bring_to_front(self):
        return None

    async def close(self):
        self.closed = True

    async def evaluate(self, script, arg=None):
        from spider_gd.core.crawler.workbench_popup import POPUP_READ_JS

        if script == POPUP_READ_JS:
            return None
        return await DomProbeFakePage.evaluate(self, script, arg)

    def locator(self, selector):
        if selector in ("#auth", "#identity"):
            return _IdentityLocator(self, selector)
        return DomProbeFakePage.locator(self, selector)

    def probe_visible(self, selector, index=None):
        return selector == "#export"

    def apply_click(self, selector, _index):
        self.clicks.append(selector)
        if selector == "#export" and self.download_future is not None:
            async def save_as(path):
                Path(path).write_bytes(b"date,price\n2026-10-01,42\n")
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


@pytest.mark.parametrize("http_status,body_valid", [(200, True), (200, False), (500, False)])
async def test_declared_calendar_unread_body_blocks_actual_export_after_newest_success(tmp_path, http_status, body_valid):
    runner = _query_runner(tmp_path)
    runner.config["query_result"]["url"] = "/findTradeCalendar"
    page = _ExportPage()
    runner.pages = [page]
    runner._observe_page(page)
    path = "/findTradeCalendar?date=2026-10-01"
    older = _QueryRequest(page, path)
    response = _DeferredQueryResponse(older, {
        "code": 200 if body_valid else 500, "data": [{"price": 42}],
    })
    response.status = http_status
    page.emit("request", older)
    page.emit("response", response)
    await asyncio.wait_for(response.reading.wait(), timeout=1)
    page.emit("requestfinished", older)
    await _current_success(runner, path)
    timer = None
    try:
        with pytest.raises(WorkflowPaused):
            await runner._do_export({"selector": "#export", "query_wait_timeout": 100,
                                     "query_settle_ms": 0})
        assert page.clicks == [] and page.download_attempts == 0
        assert len(runner._page_errors) == (1 if http_status == 500 else 0)
        assert not response.cancelled
        if body_valid:
            timer = asyncio.get_running_loop().call_later(2.2, response.completed.set)
            await runner._do_export({"selector": "#export", "query_wait_timeout": 3500,
                                     "query_settle_ms": 0})
            assert page.clicks == ["#export"] and page.download_attempts == 1
            assert (tmp_path / "query.csv").exists()
            assert not response.cancelled
            assert len(runner._calendar_payloads) == 2
            assert json.loads(runner._calendar_payloads[-1]["text"])["code"] == 200
        else:
            response.completed.set()
            with pytest.raises(WorkflowPaused):
                await runner._do_export({"selector": "#export", "query_wait_timeout": 1000,
                                         "query_settle_ms": 0})
            assert page.clicks == [] and page.download_attempts == 0
            assert len(runner._page_errors) == 1
            if http_status == 200:
                assert "业务结果未通过校验" in runner._page_errors[0]["failure"]
            else:
                assert runner._page_errors[0]["status"] == 500
            assert not runner._page_errors[0].get("_resolved")
        if http_status == 200:
            assert response.read_count == 1
    finally:
        if timer is not None:
            timer.cancel()
        runner._release_page_observers()
        pending = list(runner._response_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("http_status,retired", [(200, False), (200, True), (500, True)])
async def test_ordinary_calendar_payload_contract_is_preserved(tmp_path, http_status, retired):
    runner = _query_runner(tmp_path, contract=False)
    page = runner.page
    if retired:
        runner._retired_pages.add(page)
    request = _QueryRequest(page, "/findTradeCalendar")
    runner._calendar_ignored_requests.add(id(request))
    await _query_response(runner, request, {"data": []}, status=http_status)
    assert len(runner._calendar_payloads) == 1
    payload = runner._calendar_payloads[0]
    assert json.loads(payload["text"]) == {"data": []}
    assert payload["status"] == http_status and payload["ignored"]
    assert payload["_page"] is page
    assert not runner._page_errors and not runner._query_pending_bodies


async def _initial_popup_transport(runner, new_page, *, status=200, failed=False):
    context = new_page.context
    navigation = _InitialNavigationRequest(new_page, "/report")
    context.emit("request", navigation)
    context.emit("response", SimpleNamespace(request=navigation, status=status))
    navigation.page_available = True
    await runner._on_new_page(new_page)
    event = "requestfailed" if failed else "requestfinished"
    context.emit(event, navigation)
    new_page.emit(event, navigation)
    return navigation


@pytest.mark.parametrize("proof", [
    "valid", "unrelated_opener", "other_context", "other_origin", "wrong_scheme",
    "public_page", "public_after_binding", "old_page", "outside_manual", "old_query", "wrong_date",
    "child_only", "transport_failed", "http_error", "no_transport", "overflow",
])
async def test_manual_popup_initial_transport_before_page_event_requires_scoped_proof(tmp_path, proof):
    runner = _query_runner(tmp_path)
    runner.steps = [{"action": "export", "selector": "#export", "timeout": 1000,
                     "query_wait_timeout": 0, "query_settle_ms": 0}]
    runner.config["steps"] = runner.steps
    old_page = _ExportPage()
    if proof in ("public_page", "public_after_binding"):
        old_page.url = "https://pm.gd.csg.cn/report"
    runner.pages = [old_page]
    runner.context = old_page.context
    runner._observe_page(old_page)
    old = _QueryRequest(old_page)
    old_page.emit("request", old)
    old_page.emit("requestfailed", old)
    new_page = None
    events, waits = [], []

    async def replace():
        nonlocal new_page
        if new_page is None:
            new_page = _ExportPage(None if proof == "other_context" else old_page.context,
                                   opener=None if proof == "unrelated_opener" else old_page)
        if proof == "public_after_binding":
            new_page.url = "https://pm.gd.csg.cn/report"
        if proof == "other_origin":
            new_page.url = "https://other.invalid/report"
        elif proof == "wrong_scheme":
            new_page.url = "http://example.invalid/report"
        elif proof == "public_page":
            new_page.url = "https://pm.gd.csg.cn/portal/#/home"
        if proof == "overflow":
            for _ in range(65):
                navigation = _InitialNavigationRequest(new_page, "/report")
                old_page.context.emit("request", navigation)
        if proof == "no_transport":
            await runner._on_new_page(new_page)
        else:
            await _initial_popup_transport(runner, new_page, status=500 if proof == "http_error" else 200,
                                           failed=proof == "transport_failed")
        if proof == "public_after_binding":
            new_page.url = "https://pm.gd.csg.cn/portal/#/home"
        if proof != "old_query":
            await _current_success(runner, "/query?date=" + (
                "2026-09-01" if proof == "wrong_date" else "2026-10-01"
            ), page=new_page, frame=_QueryFrame(new_page) if proof == "child_only" else None)

    if proof in ("old_page", "old_query"):
        new_page = _ExportPage(old_page.context, opener=old_page)
        runner._observe_page(new_page)
        await _navigate_document(runner, page=new_page)
        await _current_success(runner, page=new_page)
    if proof == "outside_manual":
        await replace()

    async def resume():
        waits.append(True)
        if len(waits) > 1:
            return False
        if proof != "outside_manual":
            await replace()
        return True

    await runner._execute_steps(events.append, resume, shared=True)
    if proof == "valid":
        assert not runner.failed, runner.failure_reason
        assert len(waits) == 1
        assert new_page.clicks == ["#export"]
        assert (tmp_path / "query.csv").exists()
    else:
        assert runner.failed
        assert len(waits) == 2
        assert new_page.clicks == [] and new_page.download_attempts == 0
        assert not (tmp_path / "query.csv").exists()
    assert old_page.clicks == [] and not old_page.closed and not new_page.closed
    assert len(runner._page_errors) >= 1
    assert all(not item.get("_resolved") for item in runner._page_errors)
    assert not runner._recovery_attempts and not runner._recovery_budget
    assert not old_page.context.handlers and not new_page.context.handlers
    assert not old_page.handlers and not new_page.handlers


@pytest.mark.parametrize("replacement", [
    "valid", "unrelated", "wrong_date", "no_transport", "other_context", "outside_manual",
    "other_origin", "child_only", "transport_only", "before_binding", "station_valid",
    "station_wrong", "station_unknown", "station_scope_wrong", "station_no_session",
    "global_identity_wrong", "old_unknown", "old_child", "late_old_query",
])
async def test_real_step_loop_requires_related_manual_replacement_proof(tmp_path, replacement):
    runner = _query_runner(tmp_path)
    runner.steps = [{"action": "wait", "ms": 20}, {
        "action": "export", "selector": "#export", "timeout": 1000,
        "query_wait_timeout": 0, "query_settle_ms": 0,
    }]
    runner.config["steps"] = runner.steps
    old_page = _ExportPage()
    runner.pages = [old_page]
    runner.context = old_page.context
    events = []
    waits = []
    new_page = None
    other_page = _ExportPage(old_page.context)
    if replacement.startswith("station_") or replacement == "global_identity_wrong":
        from spider_gd.core.crawler.shared_browser import SharedBrowser

        runner.config["_station"] = {"org_name": "单位甲"}
        if replacement != "station_no_session":
            session = SharedBrowser(config_path=None)
            session._config.update({"target_origin": "https://example.invalid",
                                    "success_selector": "#auth", "identity_selector": "#identity"})
            session._station_org_name = "单位乙" if replacement == "station_scope_wrong" else "单位甲"
            if replacement == "global_identity_wrong":
                session._config["expected_identity"] = "单位乙"
            runner._shared_session = session
            runner.browser = SimpleNamespace(is_connected=lambda: True)
    def make_new_page():
        page = _ExportPage(None if replacement == "other_context" else old_page.context, opener=old_page)
        if replacement == "other_origin":
            page.url = "https://other.invalid/report"
        if replacement in ("station_wrong", "station_scope_wrong"):
            page.business_identity = "单位乙"
        elif replacement == "station_unknown":
            page.business_identity = "单位甲..."
        return page

    old_request = _QueryRequest(other_page if replacement == "unrelated" else old_page,
                                "/unknown" if replacement == "old_unknown" else "/query?date=2026-10-01")
    if replacement == "old_child":
        old_request.frame = _QueryFrame(old_page)
    runner._observe_page(old_page)
    old_page.emit("request", old_request)
    old_page.emit("requestfailed", old_request)

    async def replace():
        nonlocal new_page
        if new_page is None:
            new_page = make_new_page()
        await runner._on_new_page(new_page)
        if replacement == "before_binding":
            return
        if replacement != "no_transport":
            await _navigate_document(runner, page=new_page)
        else:
            new_page.emit("framenavigated", new_page.main_frame)
        if replacement == "transport_only":
            request = _QueryRequest(new_page)
            new_page.emit("request", request)
            new_page.emit("requestfinished", request)
        else:
            await _current_success(runner, "/query?date=" + (
                "2026-09-01" if replacement == "wrong_date" else "2026-10-01"
            ), page=new_page, frame=_QueryFrame(new_page) if replacement == "child_only" else None)
        if replacement == "late_old_query":
            late = _QueryRequest(old_page)
            old_page.emit("request", late)
            old_page.emit("requestfailed", late)

    if replacement == "before_binding":
        new_page = make_new_page()
        runner._observe_page(new_page)
        await _navigate_document(runner, page=new_page)
        await _current_success(runner, page=new_page)
    if replacement == "outside_manual":
        await replace()  # Same-context/origin proof alone is not a manual relationship.

    async def resume():
        waits.append(True)
        if len(waits) > 1:
            return False
        if replacement != "outside_manual":
            await replace()
        return True

    await runner._execute_steps(events.append, resume, shared=True)
    if replacement in ("valid", "station_valid"):
        assert not runner.failed, runner.failure_reason
        assert len(waits) == 1
        assert new_page.clicks == ["#export"]
        assert new_page.download_attempts == 1
        assert (tmp_path / "query.csv").read_bytes() == b"date,price\n2026-10-01,42\n"
        assert events[-1]["event"] == "workflow_done"
        assert sum(event["event"] == "export_saved" for event in events) == 1
    else:
        assert runner.failed
        assert len(waits) == 2
        assert new_page.clicks == []
        assert new_page.download_attempts == 0
        assert not (tmp_path / "query.csv").exists()
        assert not any(event["event"] in ("workflow_done", "step_done") for event in events)
    assert old_page.clicks == []
    assert not old_page.closed and not new_page.closed and not other_page.closed
    assert len(runner._page_errors) == (2 if replacement == "late_old_query" else 1)
    assert all(not item.get("_resolved") for item in runner._page_errors)
    assert runner._recovery_attempts == 0
    assert not runner._recovery_budget
    assert not old_page.handlers and not new_page.handlers
    assert not runner._query_frame_safety and not runner._query_active_requests
    assert not runner._manual_query_replacements and runner._manual_query_recovery is None
    if replacement in ("station_valid", "station_wrong", "station_unknown", "global_identity_wrong"):
        assert new_page.identity_reads > 0  # Real SharedBrowser.check_auth, not a bypass/mock.


async def test_manual_initial_navigation_listener_is_released_on_cancellation(tmp_path):
    runner = _query_runner(tmp_path)
    runner.steps = [{"action": "export", "selector": "#export", "query_wait_timeout": 0}]
    runner.config["steps"] = runner.steps
    page = _ExportPage()
    runner.pages, runner.context = [page], page.context
    runner._observe_page(page)
    request = _QueryRequest(page)
    page.emit("request", request)
    page.emit("requestfailed", request)
    paused = asyncio.Event()

    async def resume():
        paused.set()
        await asyncio.Event().wait()

    run = asyncio.create_task(runner._execute_steps(None, resume, shared=True))
    await asyncio.wait_for(paused.wait(), timeout=1)
    new_page = _ExportPage(page.context, opener=page)
    page.context.emit("request", _InitialNavigationRequest(new_page, "/report"))
    assert page.context.handlers
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert not page.context.handlers and not page.handlers
    assert not runner._manual_query_navigation_requests and not runner._manual_query_context_observers
    assert runner._manual_query_recovery is None
    assert not page.closed and not new_page.closed and not page.clicks
    assert len(runner._page_errors) == 1 and not runner._page_errors[0].get("_resolved")


async def test_late_manual_popup_opener_cannot_bind_after_pause_scope_closes(tmp_path):
    runner = _query_runner(tmp_path)
    runner.steps = [{"action": "export", "selector": "#export", "query_wait_timeout": 0}]
    runner.config["steps"] = runner.steps
    page = _ExportPage()
    runner.pages, runner.context = [page], page.context
    runner._observe_page(page)
    request = _QueryRequest(page)
    page.emit("request", request)
    page.emit("requestfailed", request)
    started, release = asyncio.Event(), asyncio.Event()
    callback = None
    new_page = None
    waits = []

    async def resume():
        nonlocal callback, new_page
        waits.append(True)
        if len(waits) > 1:
            return False
        new_page = _ExportPage(page.context, opener=page)
        navigation = _InitialNavigationRequest(new_page, "/report")
        page.context.emit("request", navigation)
        page.context.emit("response", SimpleNamespace(request=navigation, status=200))
        navigation.page_available = True

        async def opener():
            started.set()
            await release.wait()
            return page

        new_page.opener = opener
        callback = asyncio.create_task(runner._on_new_page(new_page))
        await started.wait()
        return True

    try:
        await runner._execute_steps(None, resume, shared=True)
        release.set()
        await callback
        assert runner.failed and len(waits) == 2
        assert new_page not in runner.pages
        assert not new_page.handlers and not page.handlers and not page.context.handlers
        assert not page.closed and not new_page.closed
        assert not page.clicks and not new_page.clicks
    finally:
        release.set()
        if callback is not None:
            await asyncio.gather(callback, return_exceptions=True)


@pytest.mark.parametrize("result", [
    "valid", "no_response", "new_error", "wrong_date", "foreign", "invalid", "cancel", "repeat",
])
async def test_full_recovery_continue_uses_configured_export_query_budget(tmp_path, monkeypatch, result):
    runner = _query_runner(tmp_path)
    runner.steps = [{"action": "wait", "ms": 20}, {
        "action": "export", "selector": "#export", "timeout": 1000,
        "query_wait_timeout": 10000, "query_settle_ms": 0,
    }]
    runner.config["steps"] = runner.steps
    clock = _Clock(monkeypatch)
    old_page = _ExportPage()
    runner.pages, runner.context = [old_page], old_page.context
    runner._observe_page(old_page)
    old = _QueryRequest(old_page)
    old_page.emit("request", old)
    old_page.emit("requestfailed", old)
    new_page, current = None, None
    waits, events = [], []
    due = 5.0
    finished = False
    error_sent = False

    async def advance():
        nonlocal finished, error_sent
        if result == "new_error" and clock.now >= 1 and not error_sent:
            error_sent = True
            error = _QueryRequest(new_page, "/unrelated-business")
            runner._on_page_request(error)
            runner._on_page_request_failed(error)
        if (current is not None and clock.now >= due and not finished
                and result not in ("no_response", "new_error")
                and (result != "repeat" or len(waits) > 1)):
            finished = True
            runner._on_page_request_done(current)
            await _query_response(runner, current, {
                "code": 500 if result == "invalid" else 200, "data": [{"price": 42}],
            })

    clock.on_sleep = advance

    async def resume():
        nonlocal new_page, current, due
        waits.append(clock.now)
        if result == "cancel":
            return False
        if len(waits) > 1:
            if result == "repeat" and len(waits) == 2:
                due = clock.now + 5
                return True
            return False
        new_page = _ExportPage(old_page.context, opener=old_page)
        if result == "foreign":
            new_page.url = "https://other.invalid/report"
        await _initial_popup_transport(runner, new_page)
        current = _QueryRequest(new_page, "/query?date=" + (
            "2026-09-01" if result == "wrong_date" else "2026-10-01"
        ))
        runner._on_page_request(current)
        due = clock.now + 5
        return True

    await runner._execute_steps(events.append, resume, shared=True)
    if result in ("valid", "repeat"):
        assert not runner.failed, runner.failure_reason
        assert len(waits) == (2 if result == "repeat" else 1)
        assert new_page.clicks == ["#export"] and new_page.download_attempts == 1
        assert (tmp_path / "query.csv").exists()
        assert clock.now >= (15 if result == "repeat" else 5)
    else:
        assert runner.failed
        assert old_page.clicks == []
        if new_page is not None:
            assert new_page.clicks == [] and new_page.download_attempts == 0
        assert not (tmp_path / "query.csv").exists()
        if result == "no_response":
            assert len(waits) == 2 and 10 <= waits[1] - waits[0] < 10.2
        elif result == "new_error":
            assert len(waits) == 2 and waits[1] - waits[0] < 1.2
    assert len(runner._page_errors) == (2 if result in ("new_error", "invalid") else 1)
    assert all(not error.get("_resolved") for error in runner._page_errors)
    assert not runner._recovery_attempts and not runner._recovery_budget
    assert not old_page.context.handlers and not old_page.handlers
    assert not runner._response_tasks and not runner._query_pending_bodies


async def test_compact_query_safety_overflow_stays_bounded_and_fails_closed(tmp_path):
    runner = _query_runner(tmp_path)
    runner.config["query_result"]["url"] = "/query/systemSwitch/getSwitch"
    page = runner.page
    runner._observe_page(page)
    requests = [_QueryRequest(page, "/query/systemSwitch/getSwitch?date=2026-10-01")
                for _ in range(260)]
    for request in requests:
        page.emit("request", request)
    _emit_switch_polls(runner, 256)
    assert len(runner._query_active_requests) <= 128
    assert len(runner._query_frame_safety) <= 64
    assert len(runner._query_request_metadata) <= 128
    assert len(runner._query_request_history) <= 40
    assert runner._query_safety_truncated
    assert set(runner._inflight_query_requests()) == set(requests)
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    runner._release_page_observers()
    assert not page.handlers
    assert not runner._query_active_requests and not runner._query_frame_safety
    assert not runner._manual_query_replacements


async def test_pending_body_safety_overflow_is_bounded_and_cancellation_cleans_it(tmp_path):
    runner = _query_runner(tmp_path)
    page = runner.page
    runner._observe_page(page)
    responses = []
    try:
        for _ in range(130):
            request = _QueryRequest(page)
            response = _DeferredQueryResponse(request, {"code": 200, "data": [{"price": 42}]})
            responses.append(response)
            page.emit("request", request)
            page.emit("response", response)
            await asyncio.wait_for(response.reading.wait(), timeout=1)
            page.emit("requestfinished", request)
        await _current_success(runner)
        assert len(runner._query_pending_bodies) <= 128
        assert runner._query_body_overflow == 2
        assert len(runner._query_active_requests) <= 128
        with pytest.raises(WorkflowPaused):
            await runner._pause_until_export_ready(timeout=0, settle_ms=0)
        assert not runner._page_errors
    finally:
        runner._release_page_observers()
        pending = list(runner._response_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    assert all(response.cancelled for response in responses)
    assert not runner._query_pending_bodies and runner._query_body_overflow == 0
    assert not runner._query_frame_safety and not runner._query_active_requests


async def test_real_step_cleanup_cancels_unfinished_business_body_without_false_failure(tmp_path):
    runner = _query_runner(tmp_path)
    runner.steps = [{"action": "export", "selector": "#export", "query_wait_timeout": 100,
                     "query_settle_ms": 0}]
    runner.config["steps"] = runner.steps
    page = _ExportPage()
    runner.pages = [page]
    events = []
    run = asyncio.create_task(runner._execute_steps(events.append, None, shared=True))
    try:
        await asyncio.wait_for(page.observed.wait(), timeout=1)
        request = _QueryRequest(page)
        response = _DeferredQueryResponse(request, {"code": 200, "data": [{"price": 42}]})
        page.emit("request", request)
        page.emit("response", response)
        await asyncio.wait_for(response.reading.wait(), timeout=1)
        await asyncio.wait_for(run, timeout=4)
        assert runner.failed
        assert response.cancelled
        assert not runner._page_errors  # Incomplete is not completed-invalid.
        assert page.clicks == [] and page.download_attempts == 0
        assert not page.handlers and not runner._response_tasks
        assert not runner._query_request_metadata and not runner._query_frame_generations
        assert not runner._query_active_requests and not runner._query_frame_safety
        assert not runner._query_pending_bodies and runner._query_body_overflow == 0
        assert not runner._manual_query_replacements and runner._manual_query_recovery is None
        assert not any(event["event"] == "workflow_done" for event in events)
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)


def _api_result(payload, content_type="application/json"):
    import base64
    import json

    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return {"ok": True, "status": 200, "statusText": "OK", "contentType": content_type,
            "filename": "result.xlsx", "base64": base64.b64encode(raw).decode()}


async def test_api_fallback_binds_current_templates_recursively_without_changing_literals(tmp_path):
    runner = _query_runner(tmp_path, contract=False)
    runner.query_month = date(2026, 10, 1)
    runner.page.result = _api_result({"code": 200, "data": [{"price": 42}]})
    fallback = {"url": "/query?month={previous_month_iso}", "method": "POST",
                "params": {"date": "{date}", "enabled": False},
                "form": {"end": "{previous_month_end}"},
                "json": {"filters": [{"month": "{previous_month_iso}", "limit": 10}],
                         "literal": '{"field": "value"}'}}
    runner.config["api_fallback"] = fallback
    assert await runner._try_api_fallback("result.xlsx", RuntimeError("no download"))
    args = runner.page.arguments[0]
    assert args["url"] == "/query?month=2026-10"
    assert args["params"] == {"date": "2026-10-01", "enabled": False}
    assert args["form"] == {"end": "2026-10-31"}
    assert args["jsonBody"] == {"filters": [{"month": "2026-10", "limit": 10}],
                                "literal": '{"field": "value"}'}
    assert fallback["params"]["date"] == "{date}"


@pytest.mark.parametrize("payload,content_type", [
    ({"code": 500, "message": "business failed", "data": [{"price": 42}]}, "application/json"),
    ({"code": 200, "data": None}, "application/json"),
    ({"code": 200, "data": []}, "application/json"),
    ({"code": 200, "data": [{"other": 42}]}, "application/json"),
    (b"<html>login</html>", "text/html"),
    ({"code": 500, "message": "business failed"}, "application/octet-stream"),
    (b"not a spreadsheet", "application/vnd.ms-excel"),
])
async def test_api_fallback_never_saves_business_errors_or_invalid_shape(tmp_path, payload, content_type):
    runner = _query_runner(tmp_path, contract=False)
    runner.config["api_fallback"] = {"url": "/query", "required_fields": ["price"]}
    runner.page.result = _api_result(payload, content_type)
    events = []
    runner._on_event = events.append
    assert not await runner._try_api_fallback("result.xlsx", RuntimeError("no download"))
    assert not (tmp_path / "result.xlsx").exists()
    assert not any(event.get("status") in ("success", "saved") for event in events)


@pytest.mark.parametrize("rows,valid", [
    ([], False),
    ([["code", "message"], [500, "business failed"]], False),
    ([["price"], [42]], True),
])
async def test_raw_api_workbook_requires_valid_configured_data_shape(tmp_path, rows, valid):
    import io
    from openpyxl import Workbook

    runner = _query_runner(tmp_path, contract=False)
    runner.config["api_fallback"] = {"url": "/query", "required_fields": ["price"]}
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    raw = output.getvalue()
    runner.page.result = _api_result(raw, "application/octet-stream")
    assert await runner._try_api_fallback("result.xlsx", RuntimeError("no download")) is valid
    path = tmp_path / "result.xlsx"
    if valid:
        assert path.read_bytes() == raw
    else:
        assert not path.exists()


@pytest.mark.parametrize("enabled", [False, True])
async def test_api_fallback_never_sends_unresolved_templates(tmp_path, enabled):
    runner = _query_runner(tmp_path, contract=False)
    runner.config["api_fallback"] = {"enabled": enabled, "url": "/query",
                                     "params": {"date": "{unknown_date}"}}
    runner.page.result = _api_result({"code": 200, "data": [{"price": 42}]})
    assert not await runner._try_api_fallback("result.xlsx", RuntimeError("no download"))
    assert runner.page.arguments == []
    assert not (tmp_path / "result.xlsx").exists()


@pytest.mark.parametrize("query_date,valid", [("2026-09-01", False), ("2026-10-01", True)])
async def test_api_fallback_checks_configured_current_response_date(tmp_path, query_date, valid):
    runner = _query_runner(tmp_path, contract=False)
    runner.config["api_fallback"] = {"url": "/query", "params": {"date": "{date}"},
                                     "response_match": {"queryDate": "{date}"}}
    runner.page.result = _api_result({"code": 200, "queryDate": query_date, "data": [{"price": 42}]})
    assert await runner._try_api_fallback("result.xlsx", RuntimeError("no download")) is valid
    assert (tmp_path / "result.xlsx").exists() is valid


@pytest.mark.parametrize("query_date,constraint", [
    ("2026-09-01", {"response_match": {"queryDate": "{date}"}}),
    ("2026-10-01", {"response_match": {"queryDate": "{date}"}}),
    ("2026-10-01", {"response_match": {"payload.queryDate": "{date}"}}),
    ("2026-10-01", {"success_field": "code", "success_value": 200}),
    ("2026-10-01", {"success_value": 200}),
    ("2026-10-01", {"response_path": "payload.rows"}),
])
async def test_raw_workbook_cannot_ignore_explicit_json_response_constraints(tmp_path, query_date, constraint):
    import io
    from openpyxl import Workbook

    runner = _query_runner(tmp_path, contract=False)
    runner.config["api_fallback"] = {"url": "/query", "params": {"date": "{date}"},
                                     "required_fields": ["price"], **constraint}
    wb = Workbook()
    wb.active.append(["queryDate", "price"])
    wb.active.append([query_date, 42])
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    runner.page.result = _api_result(output.getvalue(), "application/octet-stream")
    events = []
    runner._on_event = events.append
    assert not await runner._try_api_fallback("result_{date}.xlsx", RuntimeError("no download"))
    assert not (tmp_path / "result_2026-10-01.xlsx").exists()
    assert not any(event.get("status") in ("success", "saved") for event in events)


async def test_api_fallback_honors_configured_business_contract_and_response_path(tmp_path):
    from openpyxl import load_workbook

    runner = _query_runner(tmp_path, contract=False)
    runner.config["api_fallback"] = {"url": "/query", "success_field": "status", "success_value": "ok",
                                     "response_path": "payload.rows", "required_fields": ["price"]}
    runner.page.result = _api_result({"status": "ok", "payload": {"rows": [{"price": 42}]}})
    assert await runner._try_api_fallback("result.xlsx", RuntimeError("no download"))
    wb = load_workbook(tmp_path / "result.xlsx")
    assert list(wb.active.values) == [("price",), (42,)]
    wb.close()


# ----- 评审缺陷回归：同文档导航 / 空白弹窗 / 截断恢复 / 严格匹配 / 文件名 -----


async def test_same_document_hash_navigation_keeps_validated_query_proof(tmp_path):
    runner = _query_runner(tmp_path)
    await _current_success(runner)
    # SPA 日期控件用 history.replaceState / hash 路由同步 URL：Playwright 同样发
    # framenavigated，但没有新的 document 传输，文档身份未变，查询证明仍有效。
    runner._on_page_frame_navigated(runner.page.main_frame)
    await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    assert runner._validated_current_query()


async def test_cross_document_navigation_invalidates_validated_query_proof(tmp_path):
    runner = _query_runner(tmp_path)
    await _current_success(runner)
    await _navigate_document(runner)
    assert not runner._validated_current_query()
    with pytest.raises(WorkflowPaused):
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    # 新文档里重新校验过的查询才能再次放行导出。
    await _current_success(runner)
    await runner._pause_until_export_ready(timeout=0, settle_ms=0)


@pytest.mark.parametrize("proof", [
    "blank_valid", "blank_other_origin", "blank_public", "blank_unrelated_opener",
])
async def test_blank_popup_origin_deferred_to_first_real_navigation(tmp_path, proof):
    """window.open("") 的导出弹窗在创建时是 about:blank，源校验推迟到首次真实导航。"""
    runner = _query_runner(tmp_path)
    runner.steps = [{"action": "export", "selector": "#export", "timeout": 1000,
                     "query_wait_timeout": 0, "query_settle_ms": 0}]
    runner.config["steps"] = runner.steps
    old_page = _ExportPage()
    runner.pages = [old_page]
    runner.context = old_page.context
    runner._observe_page(old_page)
    old = _QueryRequest(old_page)
    old_page.emit("request", old)
    old_page.emit("requestfailed", old)
    new_page = None
    events, waits = [], []

    async def replace():
        nonlocal new_page
        new_page = _ExportPage(old_page.context,
                               opener=None if proof == "blank_unrelated_opener" else old_page)
        new_page.url = "about:blank"
        await runner._on_new_page(new_page)
        if proof == "blank_unrelated_opener":
            return
        target = ("https://other.invalid/report" if proof == "blank_other_origin"
                  else "https://pm.gd.csg.cn/portal/#/home" if proof == "blank_public"
                  else "https://example.invalid/report")
        new_page.url = target
        navigation = _InitialNavigationRequest(new_page, "/report")
        old_page.context.emit("request", navigation)
        old_page.context.emit("response", SimpleNamespace(request=navigation, status=200))
        navigation.page_available = True
        new_page.emit("framenavigated", new_page.main_frame)
        old_page.context.emit("requestfinished", navigation)
        new_page.emit("requestfinished", navigation)
        await _current_success(runner, page=new_page)

    async def resume():
        waits.append(True)
        if len(waits) > 1:
            return False
        await replace()
        return True

    await runner._execute_steps(events.append, resume, shared=True)
    if proof == "blank_valid":
        assert not runner.failed, runner.failure_reason
        assert len(waits) == 1
        assert new_page in runner.pages
        assert new_page.clicks == ["#export"]
        assert (tmp_path / "query.csv").exists()
    else:
        assert runner.failed
        assert len(waits) == 2
        assert new_page not in runner.pages
        assert new_page.clicks == [] and new_page.download_attempts == 0
        assert not (tmp_path / "query.csv").exists()
    assert old_page.clicks == [] and not old_page.closed and not new_page.closed
    assert len(runner._page_errors) >= 1
    assert all(not item.get("_resolved") for item in runner._page_errors)
    assert not old_page.handlers and not new_page.handlers


async def test_page_close_evicts_its_active_query_requests(tmp_path):
    runner = _query_runner(tmp_path)
    page = _ExportPage()
    other = _ExportPage(page.context)
    runner.pages = [page, other]
    runner._observe_page(page)
    runner._observe_page(other)
    kept = _QueryRequest(other, "/query?date=2026-10-01")
    other.emit("request", kept)
    for _ in range(130):
        page.emit("request", _QueryRequest(page, "/query?date=2026-10-01"))
    assert len(runner._query_active_requests) == 128
    assert runner._query_safety_truncated
    # 页面突然关闭：没有 finished/failed 的请求也必须被清出去，截断随之解除。
    page.closed = True
    page.emit("close", page)
    assert kept in runner._query_active_requests
    assert all(runner._request_owner_page(request) is not page
               for request in runner._query_active_requests)
    assert not runner._query_safety_truncated
    runner._release_page_observers()


async def test_body_overflow_truncation_recovers_after_drain(tmp_path):
    runner = _query_runner(tmp_path)
    page = runner.page
    runner._observe_page(page)
    responses = []
    try:
        for _ in range(130):
            request = _QueryRequest(page)
            response = _DeferredQueryResponse(request, {"code": 200, "data": [{"price": 42}]})
            responses.append(response)
            page.emit("request", request)
            page.emit("response", response)
            await asyncio.wait_for(response.reading.wait(), timeout=1)
            page.emit("requestfinished", request)
        assert runner._query_body_overflow == 2
        assert runner._query_safety_truncated
        with pytest.raises(WorkflowPaused):
            await runner._pause_until_export_ready(timeout=0, settle_ms=0)
        for response in responses:
            response.completed.set()
        await runner._drain_response_captures(timeout=2)
        assert runner._query_body_overflow == 0
        # 溢出条件消失后，人工弹窗恢复不再被整轮粘死的截断标志禁用。
        assert not runner._query_safety_truncated
        await _current_success(runner)
        await runner._pause_until_export_ready(timeout=0, settle_ms=0)
    finally:
        runner._release_page_observers()
        pending = list(runner._response_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("value,matches", [(1, True), (True, False), (1.0, False)])
async def test_query_json_match_requires_strict_type_and_value(tmp_path, value, matches):
    runner = _query_runner(tmp_path)
    runner.config["query_result"] = {"url": "/query", "method": "POST",
                                     "json": {"date": "{date}", "n": 1}}
    request = _QueryRequest(runner.page, "/query")
    request.method = "POST"
    request.post_data = json.dumps({"date": "2026-10-01", "n": value})
    endpoint, matched = runner._query_request_matches(request)
    assert endpoint
    assert matched is matches


@pytest.mark.parametrize("template", ["日清算_{typo}.xlsx", "日清算_{0}.xlsx", "日清算_{date:%Q}.xlsx"])
def test_render_save_as_raises_on_unknown_placeholder(template):
    with pytest.raises(WorkflowExecutionError, match="占位符"):
        render_save_as(template, date(2026, 10, 1))


async def test_save_as_directory_warning_ignores_whitespace_padding(tmp_path):
    from loguru import logger

    runner = _query_runner(tmp_path)
    messages = []
    sink = logger.add(messages.append, level="WARNING")
    try:
        assert runner._save_as_name("  日清算_{date}.xlsx  ") == "日清算_2026-10-01.xlsx"
        assert not [message for message in messages if "含目录" in message]
        assert runner._save_as_name("sub/日清算_{date}.xlsx") == "日清算_2026-10-01.xlsx"
        assert len([message for message in messages if "含目录" in message]) == 1
    finally:
        logger.remove(sink)
