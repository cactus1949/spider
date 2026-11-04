"""测试双网卡监督：自动重连与公网降级/回升。"""

from __future__ import annotations

import pytest

from spider_gd.infrastructure.network.supervisor import LinkMode, NetworkSupervisor
from spider_gd.interfaces.network import ValidateReport


class _Validator:
    def __init__(self, reports: list[ValidateReport]) -> None:
        self._reports = reports
        self._i = 0

    def validate(self, domain: str, ips: list[str]) -> ValidateReport:
        r = self._reports[min(self._i, len(self._reports) - 1)]
        self._i += 1
        return r


def _ok() -> ValidateReport:
    return ValidateReport(public_ok=True, private_ok=True, routes_ok=True)


@pytest.mark.unit
async def test_public_down_degrades_to_offline():
    """公网连续失败达到阈值（默认 2）才降级，而非单次失败。"""
    pub_down = ValidateReport(public_ok=False, private_ok=True, routes_ok=True)
    validator = _Validator([pub_down, pub_down])
    modes: list[LinkMode] = []

    async def _on_change(m):
        modes.append(m)

    sup = NetworkSupervisor(validator, target_domain="x", on_mode_change=_on_change)
    state1 = await sup.check_once()  # streak=1，未达阈值
    assert state1.mode is LinkMode.ONLINE
    state2 = await sup.check_once()  # streak=2，达到阈值，降级
    assert state2.mode is LinkMode.DEGRADED_OFFLINE
    assert modes == [LinkMode.DEGRADED_OFFLINE]


@pytest.mark.unit
async def test_public_recovers_back_to_online():
    """公网降级后恢复正常，模式回升为 ONLINE。"""
    pub_down = ValidateReport(public_ok=False, private_ok=True, routes_ok=True)
    validator = _Validator([pub_down, pub_down, _ok()])
    modes: list[LinkMode] = []

    async def _on_change(m):
        modes.append(m)

    sup = NetworkSupervisor(validator, target_domain="x", on_mode_change=_on_change)
    await sup.check_once()  # streak=1，未降级
    await sup.check_once()  # streak=2，降级
    state = await sup.check_once()  # 恢复
    assert state.mode is LinkMode.ONLINE
    assert modes == [LinkMode.DEGRADED_OFFLINE, LinkMode.ONLINE]


@pytest.mark.unit
async def test_public_single_jitter_no_degrade():
    """公网单次瞬态抖动不应触发降级。"""
    pub_down = ValidateReport(public_ok=False, private_ok=True, routes_ok=True)
    validator = _Validator([pub_down, _ok()])
    modes: list[LinkMode] = []

    async def _on_change(m):
        modes.append(m)

    sup = NetworkSupervisor(validator, target_domain="x", on_mode_change=_on_change)
    state1 = await sup.check_once()  # 单次失败，未达阈值
    assert state1.mode is LinkMode.ONLINE
    assert state1.public_fail_streak == 1
    state2 = await sup.check_once()  # 恢复，streak 清零
    assert state2.mode is LinkMode.ONLINE
    assert state2.public_fail_streak == 0
    assert modes == []  # 从未发生模式切换


@pytest.mark.unit
async def test_public_consecutive_failures_degrades():
    """公网连续失败达到阈值才降级，自定义 public_degrade_after=3。"""
    pub_down = ValidateReport(public_ok=False, private_ok=True, routes_ok=True)
    validator = _Validator([pub_down] * 4)
    modes: list[LinkMode] = []

    async def _on_change(m):
        modes.append(m)

    sup = NetworkSupervisor(
        validator, target_domain="x", on_mode_change=_on_change, public_degrade_after=3
    )
    await sup.check_once()  # streak=1
    assert sup.state.mode is LinkMode.ONLINE
    await sup.check_once()  # streak=2
    assert sup.state.mode is LinkMode.ONLINE
    await sup.check_once()  # streak=3，降级
    assert sup.state.mode is LinkMode.DEGRADED_OFFLINE
    assert modes == [LinkMode.DEGRADED_OFFLINE]
    # streak 清零后重新累计
    await sup.check_once()  # streak=1（前一次已降级，但 streak 继续累加）
    assert sup.state.mode is LinkMode.DEGRADED_OFFLINE  # 已在降级态，无新的回调
    assert modes == [LinkMode.DEGRADED_OFFLINE]


@pytest.mark.unit
async def test_private_failure_triggers_reconnect():
    down = ValidateReport(public_ok=True, private_ok=False, routes_ok=True)
    validator = _Validator([down, down])
    reconnects: list[int] = []

    async def _reconnect() -> bool:
        reconnects.append(1)
        return True

    sup = NetworkSupervisor(
        validator, target_domain="x", reconnect=_reconnect, reconnect_after_failures=2
    )
    await sup.check_once()  # streak=1，未触发
    assert reconnects == []
    await sup.check_once()  # streak=2，触发重连
    assert reconnects == [1]
    assert sup.state.private_fail_streak == 0  # 重连成功后清零
