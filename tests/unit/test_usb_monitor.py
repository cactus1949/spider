"""测试 U-Key USB 监控（注入 list_ids_fn，无需真实 WMI）。"""

from __future__ import annotations

import pytest

from spider_gd.infrastructure.network.usb_monitor import UsbMonitor


@pytest.mark.unit
def test_poll_once_detects_insertion():
    states = [{"148F:5370", "0BDA:8153"}, {"148F:5370", "0BDA:8153", "1A2B:3C4D"}]
    mon = UsbMonitor(vid_pid="auto", list_ids_fn=lambda: states.pop(0))
    mon.poll_once()  # 初始化基线
    added, removed = mon.poll_once()
    assert added == {"1A2B:3C4D"}
    assert removed == set()


@pytest.mark.unit
def test_poll_once_detects_removal():
    states = [{"148F:5370", "1A2B:3C4D"}, {"148F:5370"}]
    mon = UsbMonitor(vid_pid="auto", list_ids_fn=lambda: states.pop(0))
    mon.poll_once()
    added, removed = mon.poll_once()
    assert added == set()
    assert removed == {"1A2B:3C4D"}


@pytest.mark.unit
def test_vid_pid_filter_only_matches_ukey():
    states = [{"148F:5370"}, {"148F:5370", "9999:8888"}]
    mon = UsbMonitor(vid_pid="148F:5370", list_ids_fn=lambda: states.pop(0))
    mon.poll_once()
    added, _ = mon.poll_once()
    assert added == set()  # 新增的不是目标 U-Key


@pytest.mark.unit
def test_list_devices_delegates_to_injected_fn():
    mon = UsbMonitor(list_ids_fn=lambda: {"AAAA:BBBB"})
    assert mon.list_devices() == {"AAAA:BBBB"}
