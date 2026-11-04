"""测试 U-Key 管理：插入自登录与有效期告警。"""

from __future__ import annotations

from datetime import date

import pytest

from spider_gd.core.crawler.ukey_manager import UKeyManager
from spider_gd.infrastructure.network.usb_monitor import UsbMonitor


def _monitor(devices: set[str]) -> UsbMonitor:
    return UsbMonitor(list_ids_fn=lambda: set(devices))


@pytest.mark.unit
def test_is_present():
    assert UKeyManager(_monitor({"1234:5678"})).is_present() is True
    assert UKeyManager(_monitor(set())).is_present() is False


@pytest.mark.unit
def test_days_until_expiry():
    mgr = UKeyManager(_monitor(set()), valid_until="2026-07-01")
    assert mgr.days_until_expiry(now=date(2026, 6, 23)) == 8
    assert mgr.days_until_expiry(now=date(2026, 7, 10)) == -9


@pytest.mark.unit
async def test_check_expiry_warns_within_window():
    alerts: list[tuple[str, str]] = []

    async def _hook(title, msg):
        alerts.append((title, msg))

    mgr = UKeyManager(
        _monitor(set()), valid_until="2026-07-01", warn_days=30, alert_hook=_hook
    )
    days = await mgr.check_expiry(now=date(2026, 6, 23))
    assert days == 8
    assert len(alerts) == 1
    assert "到期" in alerts[0][0]


@pytest.mark.unit
async def test_check_expiry_no_alert_when_far():
    alerts: list[tuple[str, str]] = []

    async def _hook(title, msg):
        alerts.append((title, msg))

    mgr = UKeyManager(
        _monitor(set()), valid_until="2026-12-31", warn_days=30, alert_hook=_hook
    )
    await mgr.check_expiry(now=date(2026, 6, 23))
    assert alerts == []


@pytest.mark.unit
async def test_wait_and_login_triggers_on_present():
    logged_in = []

    async def _login():
        logged_in.append(True)

    mgr = UKeyManager(_monitor({"1234:5678"}))
    ok = await mgr.wait_and_login(_login, max_polls=1)
    assert ok is True
    assert logged_in == [True]


@pytest.mark.unit
async def test_wait_and_login_gives_up_without_ukey():
    async def _login():
        raise AssertionError("不应被调用")

    mgr = UKeyManager(_monitor(set()))
    ok = await mgr.wait_and_login(_login, max_polls=2)
    assert ok is False
