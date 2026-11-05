"""指标失败后的重试、400 只复位一次，以及续跑跳过已成功项。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import task_runner as task_runner_module
from task_retry import FailureKind, classify_failure, slot_keys_for_item
from task_runner import TaskRunner


@pytest.mark.unit
def test_classify_retry_continue_and_stop():
    assert classify_failure("等待元素超时：.export") is FailureKind.RETRYABLE
    assert classify_failure("下载超时") is FailureKind.RETRYABLE
    assert classify_failure("仍有 2 个同源查询请求在途") is FailureKind.RETRYABLE
    assert classify_failure("未确认业务标签页") is FailureKind.RETRYABLE
    assert classify_failure("导航到工作台失败") is FailureKind.RETRYABLE
    assert classify_failure("网关形态 HTTP 400") is FailureKind.GATEWAY_400
    assert classify_failure("数字证书连接失败") is FailureKind.FATAL
    assert classify_failure("指标不存在: spot") is FailureKind.FATAL
    assert classify_failure("敏感参数未配置或未替换: ${CERT_PASSWORD}") is FailureKind.FATAL
    assert classify_failure("用户取消") is FailureKind.FATAL
    assert classify_failure("导出文件内容为空") is FailureKind.LOCAL


class _Page:
    def __init__(self) -> None:
        self.url = "https://pm.gd.csg.cn/portal/#/home"
        self.gotos: list[str] = []
        self.context = None

    async def goto(self, url, **_kwargs) -> None:
        self.gotos.append(url)
        self.url = url

    async def evaluate(self, script, arg=None):
        return "工作台" in str(script)

    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]
        page.context = self

    def on(self, *_args, **_kwargs) -> None:
        return None

    def remove_listener(self, *_args, **_kwargs) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.page = _Page()
        self._context = _Context(self.page)
        self._browser = object()
        self._cancel_requested = False
        self._active_runner = None

    @asynccontextmanager
    async def execution(self, _name, on_event=None):
        def emit(event):
            if on_event:
                on_event(event)

        yield emit

    async def check_auth(self, *_args, **_kwargs):
        return {"state": "authenticated"}

    async def require_auth(self, **_kwargs) -> bool:
        return True

    async def wait_continue(self) -> bool:
        return True


class _ScriptedRunner:
    reasons: list[str] = []  # noqa: RUF012
    made: list[str] = []  # noqa: RUF012

    def __init__(self, cfg, **_kwargs) -> None:
        self.cfg = cfg
        self.failed = False
        self.failure_reason = ""
        self.pages = []
        self._current_page_idx = 0
        self._on_new_page = lambda *_args, **_kwargs: None
        _ScriptedRunner.made.append(str(cfg.get("_control_name") or cfg.get("name") or ""))

    async def _execute_steps(self, _on_event, _wait, *, shared: bool = False) -> None:
        reason = _ScriptedRunner.reasons.pop(0)
        self.failed = bool(reason)
        self.failure_reason = reason


def _runner(tmp_path: Path, **config) -> TaskRunner:
    base = {
        "name": "日常",
        "indicators": ["a", "b"],
        "target_dates": ["2026-11-01"],
        "indicator_retry": 1,
    }
    base.update(config)
    return TaskRunner(
        base,
        download_dir=str(tmp_path),
        workflow_loader=lambda name: {
            "name": name, "steps": [], "needs_login": False, "_control_name": name,
        },
    )


@pytest.mark.unit
async def test_retryable_failure_continues_to_next_indicator(monkeypatch, tmp_path: Path):
    _ScriptedRunner.reasons = ["等待元素超时：.x", "等待元素超时：.x", ""]
    _ScriptedRunner.made = []
    monkeypatch.setattr(task_runner_module, "WorkflowRunner", _ScriptedRunner)
    session = _Session()
    events: list[dict] = []
    await _runner(tmp_path).run(events.append, session=session)
    names = [item["event"] for item in events]
    assert "task_done" in names
    assert "task_error" not in names
    done = next(item for item in events if item["event"] == "task_done")
    assert done["success"] == 1
    assert done["failed"] == 1
    assert session.page.gotos
    assert "powerEnterprise" in session.page.gotos[0]


@pytest.mark.unit
async def test_single_http_400_resets_once_then_second_stops(monkeypatch, tmp_path: Path):
    _ScriptedRunner.reasons = ["网关形态 HTTP 400", ""]
    _ScriptedRunner.made = []
    monkeypatch.setattr(task_runner_module, "WorkflowRunner", _ScriptedRunner)
    session = _Session()
    events: list[dict] = []
    await _runner(tmp_path, indicators=["a"]).run(events.append, session=session)
    assert any(item["event"] == "task_done" for item in events)
    assert not any(item["event"] == "task_error" for item in events)
    assert len(session.page.gotos) == 1

    _ScriptedRunner.reasons = ["HTTP 400 网关拦截", "HTTP 400 网关拦截"]
    _ScriptedRunner.made = []
    session = _Session()
    events = []
    await _runner(tmp_path, indicators=["a"]).run(events.append, session=session)
    errors = [item for item in events if item["event"] == "task_error"]
    assert errors
    assert "再次" in errors[0]["error"]
    assert "task_done" not in [item["event"] for item in events]
    assert len(session.page.gotos) == 1


@pytest.mark.unit
async def test_fatal_error_stops_remaining_indicators(monkeypatch, tmp_path: Path):
    _ScriptedRunner.reasons = ["数字证书连接失败", ""]
    _ScriptedRunner.made = []
    monkeypatch.setattr(task_runner_module, "WorkflowRunner", _ScriptedRunner)
    events: list[dict] = []
    await _runner(tmp_path).run(events.append, session=_Session())
    assert any(item["event"] == "task_error" for item in events)
    assert _ScriptedRunner.made == ["a"]


@pytest.mark.unit
async def test_resume_skips_succeeded_indicator_date(monkeypatch, tmp_path: Path):
    from datetime import date

    keys = slot_keys_for_item({"indicator": "a", "mode": "replay", "dates": [date(2026, 11, 1)]})
    _ScriptedRunner.reasons = [""]
    _ScriptedRunner.made = []
    monkeypatch.setattr(task_runner_module, "WorkflowRunner", _ScriptedRunner)
    events: list[dict] = []
    await _runner(tmp_path, _skip_slots=keys).run(events.append, session=_Session())
    skipped = [item for item in events if item.get("status") == "skip"]
    assert skipped
    assert skipped[0]["name"] == "a"
    assert _ScriptedRunner.made == ["b"]
    assert any(item["event"] == "task_done" and item["skipped"] == 1 for item in events)
