"""控制接口浏览器可见性策略测试。"""

from __future__ import annotations

import pytest

from spider_gd.gateway.api.server import _resolve_browser_visibility


@pytest.mark.unit
def test_headless_request_uses_offscreen_visible_browser():
    headless, minimized = _resolve_browser_visibility("true", "false")

    assert headless is False
    assert minimized is True


@pytest.mark.unit
def test_minimized_request_uses_offscreen_visible_browser():
    headless, minimized = _resolve_browser_visibility("false", "true")

    assert headless is False
    assert minimized is True


@pytest.mark.unit
def test_visible_request_stays_visible():
    headless, minimized = _resolve_browser_visibility("false", "false")

    assert headless is False
    assert minimized is False
