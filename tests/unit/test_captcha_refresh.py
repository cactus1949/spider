"""验证码长度不对时换图重试，并且只走低痕迹点击。"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowRunner, captcha_refresh_count, captcha_refresh_delay_s

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
            self.src = "data:image/png;base64,Qg=="

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
        "retry": 3,
        "refresh_before": 1,
        "refresh_interval_ms": 800,
    })
    assert _LengthThenOk.calls == 2
    image_clicks = [item for item in page.clicked if "img" in item]
    assert len(image_clicks) >= 2
    assert page.mouse_events.count("down") == page.mouse_events.count("up")
    assert page.mouse_events
    assert "ab12" in page.typed or page.keys
