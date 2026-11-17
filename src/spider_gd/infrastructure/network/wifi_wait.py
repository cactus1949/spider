"""等待已保存的随身热点出现。有总时限，扫描一次比一次疏。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class WifiWaitResult:
    """一次有界等待的结果。

    Args:
        ok: 已经连上保存的热点。
        status: ``connected``、``missing`` 或 ``cancelled``。
        reason: 没连上时给人看的原因。
        attempts: 扫描次数。

    Examples:
        >>> WifiWaitResult(True, "connected", "", 1).ok
        True
    """

    ok: bool
    status: str
    reason: str
    attempts: int


def wifi_wait_budget(unattended: bool) -> float:
    """无人值守有总时限。有人在旁边时限更长，到点后改为暂停而不是一直扫。

    Args:
        unattended: 定时任务这类没有人守着的执行为 True。

    Returns:
        float: 秒。

    Examples:
        >>> wifi_wait_budget(True)
        45.0
        >>> wifi_wait_budget(False)
        90.0
    """
    return 45.0 if unattended else 90.0


def wait_for_saved_ssid(
    ssid: str,
    *,
    visible: Callable[[], list[str]],
    connect: Callable[[], bool],
    timeout_sec: float,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    cancelled: Callable[[], bool] | None = None,
    initial_delay: float = 2.0,
    max_delay: float = 8.0,
) -> WifiWaitResult:
    """反复扫描并连接用户已经保存的热点。

    不改路由，也不碰没保存过的网络。扫不到就睡一觉再扫，睡眠时间翻倍，但不超过 ``max_delay``。
    到总时限还没有就停，不会一直等。

    Args:
        ssid: 场站里保存的热点名称。
        visible: 返回当前扫到的热点名称。
        connect: 用已保存的密码去连。连上返回 True。
        timeout_sec: 整段等待的上限，单位秒。
        clock: 返回单调时间。
        sleeper: 睡眠函数。测试里可以只记账。
        cancelled: 返回 True 时立刻停止。
        initial_delay: 第一次没扫到之后等多久。
        max_delay: 单次等待的上限。

    Returns:
        WifiWaitResult: 连上、超时或取消。

    Examples:
        >>> now = {"t": 0.0}
        >>> def clock():
        ...     return now["t"]
        >>> def sleeper(seconds):
        ...     now["t"] += seconds
        >>> def visible():
        ...     return ["YJ"] if now["t"] else []
        >>> result = wait_for_saved_ssid(
        ...     "YJ", visible=visible, connect=lambda: True, timeout_sec=30,
        ...     clock=clock, sleeper=sleeper,
        ... )
        >>> result.ok and result.attempts == 2
        True
    """
    target = ssid.strip()
    if not target:
        return WifiWaitResult(False, "missing", "未配置 WiFi", 0)
    deadline = clock() + max(0.0, timeout_sec)
    delay = max(0.5, initial_delay)
    attempts = 0
    while True:
        if cancelled and cancelled():
            return WifiWaitResult(False, "cancelled", "已取消", attempts)
        attempts += 1
        names = {item.strip() for item in visible() if item and item.strip()}
        if target in names and connect():
            return WifiWaitResult(True, "connected", "", attempts)
        now = clock()
        if now >= deadline:
            return WifiWaitResult(
                False, "missing", "未搜到已保存的热点，请检查设备电源", attempts,
            )
        remaining = min(delay, deadline - now)
        slept = 0.0
        while slept < remaining - 1e-9:
            if cancelled and cancelled():
                return WifiWaitResult(False, "cancelled", "已取消", attempts)
            step = min(0.5, remaining - slept)
            sleeper(step)
            slept += step
        delay = min(max_delay, delay * 2)
