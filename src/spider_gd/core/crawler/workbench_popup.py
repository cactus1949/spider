"""工作台浮层弹窗：一次读取，只关不改业务状态的按钮。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

__all__ = [
    "POPUP_PAUSE_MESSAGE",
    "POPUP_READ_JS",
    "close_workbench_popups",
    "read_workbench_popup",
]

POPUP_PAUSE_MESSAGE = "工作台有弹窗未能自动关闭"
_MAX_CLOSES = 5

# 一次读出最上层对话框和可点的安全按钮。不创建元素句柄，也不循环重试。
POPUP_READ_JS = r"""() => {
  const compact = (text) => String(text || '').replace(/\s+/g, '');
  const visible = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (Number(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width >= 8 && rect.height >= 8;
  };
  const zIndexOf = (el) => {
    const value = Number(getComputedStyle(el).zIndex);
    return Number.isFinite(value) ? value : 0;
  };
  const viewW = window.innerWidth || 1;
  const viewH = window.innerHeight || 1;
  const viewArea = viewW * viewH;
  const safeLabel = new Set(['关闭', '取消', '我知道了', '知道了', '×', '✕', '✖', 'x', 'X']);
  const dialogs = [];
  const nodes = document.body ? Array.from(document.body.querySelectorAll('*')) : [];
  for (const el of nodes) {
    if (!visible(el)) continue;
    const style = getComputedStyle(el);
    if (style.position !== 'fixed' && style.position !== 'absolute') continue;
    const rect = el.getBoundingClientRect();
    const cover = (rect.width * rect.height) / viewArea;
    const z = zIndexOf(el);
    const dialog = rect.width >= 220 && rect.height >= 72 && cover >= 0.03 && cover <= 0.9;
    if (!dialog) continue;
    if (z < 20 && cover < 0.08) continue;
    dialogs.push({ el, z });
  }
  if (!dialogs.length) return null;
  dialogs.sort((a, b) => b.z - a.z);
  const box = dialogs[0].el;
  const text = String(box.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500);
  const controls = Array.from(box.querySelectorAll('button, [role="button"], a'));
  let button = null;
  for (const control of controls) {
    if (!visible(control)) continue;
    const raw = control.innerText || control.getAttribute('aria-label') || control.getAttribute('title') || '';
    const label = compact(raw);
    const aria = compact(control.getAttribute('aria-label') || '');
    const cls = String(control.className || '');
    const icon = label.length <= 2 && /close|关闭/.test(cls + aria);
    if (!safeLabel.has(label) && !safeLabel.has(aria) && !icon) continue;
    const rect = control.getBoundingClientRect();
    button = {
      label: label || aria || '关闭',
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      safe: true,
    };
    break;
  }
  return { text, button };
}"""


async def read_workbench_popup(page) -> dict | None:
    """读当前页最上层的工作台弹窗。没有弹窗或页面不能执行脚本时返回 None。

    Args:
        page: 绑定的页面。只调用一次 ``evaluate``。

    Returns:
        dict | None: ``text`` 是弹窗原文，``button`` 是可关闭按钮的坐标。

    Examples:
        >>> POPUP_PAUSE_MESSAGE
        '工作台有弹窗未能自动关闭'
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = evaluate(POPUP_READ_JS)
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    if not str(result.get("text") or "").strip() and not result.get("button"):
        return None
    return result


async def close_workbench_popups(
    read: Callable[[], Awaitable[dict | None]],
    click,
    pause,
    sleep,
    log,
    *,
    limit: int = _MAX_CLOSES,
) -> int:
    """按从上层到下层关掉工作台弹窗。

    每轮只读一次。点完由调用方等待后再读。没有安全关闭按钮、点完仍在，
    或超过 ``limit`` 个，都暂停，不再点别的按钮。

    Args:
        read: 读取当前最上层弹窗。
        click: 按给定坐标点击。
        pause: 暂停并等待人工处理。
        sleep: 两次读取之间的等待。
        log: 记下被关掉的弹窗原文。
        limit: 最多自动关闭的个数。

    Returns:
        int: 实际点击关闭的次数。

    Examples:
        >>> _MAX_CLOSES
        5
    """
    closed = 0
    previous = None
    while closed < limit:
        popup = await read()
        if not popup:
            return closed
        text = str(popup.get("text") or "").strip()
        button = popup.get("button")
        safe = isinstance(button, dict) and button.get("safe") is True
        mark = (text, None if not isinstance(button, dict) else round(float(button.get("y") or 0), 1))
        if previous is not None and mark == previous:
            await pause(POPUP_PAUSE_MESSAGE)
            return closed
        if not safe:
            await pause(POPUP_PAUSE_MESSAGE)
            return closed
        log(text)
        await click(button)
        closed += 1
        previous = mark
        await sleep()
    leftover = await read()
    if leftover:
        await pause(POPUP_PAUSE_MESSAGE)
    return closed
