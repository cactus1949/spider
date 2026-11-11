"""验证码长度不对时换图重试，并且只走低痕迹点击。"""

from __future__ import annotations

import asyncio
import base64
import random
from pathlib import Path

import pytest
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import (
    WorkflowRunner,
    captcha_refresh_count,
    captcha_refresh_delay_s,
    captcha_self_check_rounds,
)

from spider_gd.core.crawler.exceptions import CaptchaError


class _CaptchaPage(DomProbeFakePage):
    """验证码图和输入框都可见。点击图片会换 src。"""

    def __init__(self) -> None:
        super().__init__()
        self.src = "data:image/png;base64,QQ=="
        self.clicked: list[str] = []

    def probe_visible(self, selector: str, index: int | None = None) -> bool:
        return True

    def probe_count(self, selector: str) -> int:
        return 1

    def probe_attr(self, selector: str, name: str) -> str | None:
        if name == "src" and "img" in selector:
            return self.src
        return ""

    def apply_click(self, selector: str, index: int | None) -> None:
        self.clicked.append(selector)
        if "img" in selector:
            token = base64.b64encode(bytes([len(self.clicked) + 1])).decode("ascii")
            self.src = f"data:image/png;base64,{token}"

    async def screenshot(self, **_kwargs):
        return None


class _LengthThenOk:
    calls = 0

    def recognize(self, _image: bytes) -> str:
        _LengthThenOk.calls += 1
        if _LengthThenOk.calls == 1:
            raise CaptchaError("识别结果长度异常: raw='kry'")
        return "ab12"


@pytest.mark.unit
def test_refresh_interval_is_rate_limited():
    assert captcha_refresh_count({}) == 0
    assert captcha_refresh_count({"refresh_before": 3}) == 3
    assert captcha_refresh_count({"pre_refresh": 2}) == 2
    rng = random.Random(1)
    assert 0.8 <= captcha_refresh_delay_s({}, rng) <= 1.5
    assert captcha_refresh_delay_s({"refresh_interval_ms": 100}, random.Random(1)) >= 0.8
    assert captcha_self_check_rounds({}) == 3
    assert captcha_self_check_rounds({"self_check_rounds": None}) == 3
    assert captcha_self_check_rounds({"self_check_rounds": "x"}) == 3
    assert captcha_self_check_rounds({"self_check_rounds": 1}) == 1
    assert captcha_self_check_rounds({"retry": 5}) == 5
    assert captcha_self_check_rounds({"retry": 5, "self_check_rounds": 2}) == 2


@pytest.mark.unit
async def test_short_captcha_refreshes_with_low_profile_click(monkeypatch, tmp_path: Path):
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    _LengthThenOk.calls = 0
    monkeypatch.setattr(captcha_solver, "CaptchaSolver", _LengthThenOk)

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _CaptchaPage()
    runner = WorkflowRunner({"name": "登录", "steps": []}, download_dir=str(tmp_path))
    runner.pages = [page]
    runner._pointer_settle_s = 0
    await runner._do_captcha({
        "captcha_img": "img.captcha",
        "captcha_input": "#code",
        "refresh_before": 1,
        "self_check_rounds": 1,
        "refresh_interval_ms": 800,
    })
    assert _LengthThenOk.calls == 2
    image_clicks = [item for item in page.clicked if "img" in item]
    # 预刷新 1 次，不合格后再刷新 2 次。
    assert len(image_clicks) == 3
    assert page.mouse_events.count("down") == page.mouse_events.count("up")
    assert page.mouse_events
    assert "ab12" in page.typed or page.keys


class _ScriptedSolver:
    def __init__(self, results: list[str | Exception]) -> None:
        self._results = list(results)
        self.calls = 0

    def recognize(self, _image: bytes) -> str:
        self.calls += 1
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _runner(tmp_path: Path, page: _CaptchaPage) -> WorkflowRunner:
    runner = WorkflowRunner({"name": "登录", "steps": []}, download_dir=str(tmp_path))
    runner.pages = [page]
    runner._pointer_settle_s = 0
    return runner


@pytest.mark.unit
async def test_valid_code_does_not_refresh_again(monkeypatch, tmp_path: Path):
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    solver = _ScriptedSolver(["Ab12"])
    monkeypatch.setattr(captcha_solver, "CaptchaSolver", lambda: solver)

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _CaptchaPage()
    await _runner(tmp_path, page)._do_captcha({
        "captcha_img": "img.captcha",
        "captcha_input": "#code",
    })
    assert solver.calls == 1
    assert [item for item in page.clicked if "img" in item] == []
    assert "Ab12" in page.typed


@pytest.mark.unit
async def test_each_failed_round_refreshes_twice(monkeypatch, tmp_path: Path):
    import spider_gd.core.crawler.captcha_solver as captcha_solver

    solver = _ScriptedSolver([
        CaptchaError("验证码自检未通过: 'zwe'"),
        CaptchaError("验证码自检未通过: '验证码'"),
        "a1b2",
    ])
    monkeypatch.setattr(captcha_solver, "CaptchaSolver", lambda: solver)

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _CaptchaPage()
    await _runner(tmp_path, page)._do_captcha({
        "captcha_img": "img.captcha",
        "captcha_input": "#code",
        "self_check_rounds": 3,
    })
    assert solver.calls == 3
    assert [item for item in page.clicked if "img" in item] == ["img.captcha"] * 4
    assert "a1b2" in page.typed


@pytest.mark.unit
async def test_default_three_rounds_then_stop(monkeypatch, tmp_path: Path):
    import spider_gd.core.crawler.captcha_solver as captcha_solver
    from workflow_runner import WorkflowExecutionError

    solver = _ScriptedSolver([CaptchaError("验证码自检未通过: '汉字啊'")] * 4)
    monkeypatch.setattr(captcha_solver, "CaptchaSolver", lambda: solver)

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _CaptchaPage()
    with pytest.raises(WorkflowExecutionError, match="自检3轮"):
        await _runner(tmp_path, page)._do_captcha({
            "captcha_img": "img.captcha",
            "captcha_input": "#code",
        })
    assert solver.calls == 4
    assert [item for item in page.clicked if "img" in item] == ["img.captcha"] * 6


@pytest.mark.unit
async def test_legacy_retry_is_the_round_count(monkeypatch, tmp_path: Path):
    import spider_gd.core.crawler.captcha_solver as captcha_solver
    from workflow_runner import WorkflowExecutionError

    solver = _ScriptedSolver([CaptchaError("验证码自检未通过: 'zwe'")] * 3)
    monkeypatch.setattr(captcha_solver, "CaptchaSolver", lambda: solver)

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _CaptchaPage()
    with pytest.raises(WorkflowExecutionError, match="自检2轮"):
        await _runner(tmp_path, page)._do_captcha({
            "captcha_img": "img.captcha",
            "captcha_input": "#code",
            "retry": 2,
        })
    assert solver.calls == 3
    assert [item for item in page.clicked if "img" in item] == ["img.captcha"] * 4


@pytest.mark.unit
async def test_unchanged_src_is_not_polled_tightly(monkeypatch, tmp_path: Path):
    class _Stuck(_CaptchaPage):
        def apply_click(self, selector: str, index: int | None) -> None:
            self.clicked.append(selector)

    async def _no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _Stuck()
    runner = _runner(tmp_path, page)
    await runner._refresh_captcha_image("img.captcha", {"refresh_interval_ms": 800}, "stuck")
    attr_reads = [item for item in page.probes if item[0] == "attr"]
    assert len(attr_reads) <= 8
