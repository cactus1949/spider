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

# 一次读出最上层、挡着操作的对话框。只看当前文档，不扫其他 frame。
# 带文字的顶栏、绝对定位的页面主体、透明度归零或点不中的残留都不算。
POPUP_READ_JS = r"""() => {
  const compact = (text) => String(text || '').replace(/\s+/g, '');
  const viewW = window.innerWidth || 1;
  const viewH = window.innerHeight || 1;
  const viewArea = viewW * viewH;
  const safeLabel = new Set(['关闭', '取消', '我知道了', '知道了', '×', '✕', '✖', 'x', 'X']);
  const modalRe = /ant-modal|ant-drawer|el-dialog|el-drawer|el-message-box/;
  const maskRe = /v-modal|modal-mask|dialog-wrap/;
  const styleOf = (el) => getComputedStyle(el);
  const blocked = (el) => {
    if (styleOf(el).pointerEvents === 'none') return true;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = styleOf(node);
      if (style.display === 'none' || style.visibility === 'hidden') return true;
      if (Number(style.opacity) < 0.05) return true;
      const hidden = node.getAttribute('aria-hidden') === 'true' || node.hasAttribute('hidden');
      if (hidden) return true;
    }
    return false;
  };
  const overlap = (rect) => {
    const left = Math.max(rect.left, 0);
    const top = Math.max(rect.top, 0);
    const right = Math.min(rect.right, viewW);
    const bottom = Math.min(rect.bottom, viewH);
    const width = Math.max(0, right - left);
    const height = Math.max(0, bottom - top);
    return { left, top, width, height, area: width * height };
  };
  const modalLike = (el) => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const role = String(node.getAttribute('role') || '').toLowerCase();
      if (role === 'dialog' || role === 'alertdialog') return true;
      if (node.getAttribute('aria-modal') === 'true') return true;
      const cls = String(node.className || '');
      if (modalRe.test(cls) || maskRe.test(cls)) return true;
    }
    return false;
  };
  const safeButton = (box) => {
    const controls = Array.from(box.querySelectorAll('button, [role="button"], a'));
    for (const control of controls) {
      if (blocked(control)) continue;
      const raw = control.innerText
        || control.getAttribute('aria-label')
        || control.getAttribute('title')
        || '';
      const label = compact(raw);
      const aria = compact(control.getAttribute('aria-label') || '');
      const cls = String(control.className || '');
      const icon = label.length <= 2 && /close|关闭/.test(cls + aria);
      if (!safeLabel.has(label) && !safeLabel.has(aria) && !icon) continue;
      const rect = control.getBoundingClientRect();
      const seen = overlap(rect);
      if (seen.width < 8 || seen.height < 8) continue;
      return {
        label: label || aria || '关闭',
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        safe: true,
      };
    }
    return null;
  };
  const backdropOf = (box, seen) => {
    const probes = [[8, 8], [viewW - 8, 8], [8, viewH - 8], [viewW - 8, viewH - 8]];
    for (const pair of probes) {
      const px = pair[0];
      const py = pair[1];
      const inside = px >= seen.left && px <= seen.left + seen.width
        && py >= seen.top && py <= seen.top + seen.height;
      if (inside) continue;
      const outside = document.elementFromPoint(px, py);
      if (!outside || outside === box || box.contains(outside)) continue;
      for (let node = outside; node && node.nodeType === 1; node = node.parentElement) {
        const style = styleOf(node);
        const layered = style.position === 'fixed' || style.position === 'absolute';
        if (!layered || blocked(node)) continue;
        const area = overlap(node.getBoundingClientRect()).area / viewArea;
        if (area >= 0.2 || modalLike(node)) return true;
      }
    }
    return false;
  };
  const dialogs = [];
  const nodes = document.body ? Array.from(document.body.querySelectorAll('*')) : [];
  for (const el of nodes) {
    if (blocked(el)) continue;
    const style = styleOf(el);
    if (style.position !== 'fixed' && style.position !== 'absolute') continue;
    const seen = overlap(el.getBoundingClientRect());
    if (seen.width < 220 || seen.height < 72) continue;
    const cover = seen.area / viewArea;
    if (cover < 0.03 || cover > 0.9) continue;
    const evidence = modalLike(el);
    const bar = seen.width > viewW * 0.92 && seen.height < viewH * 0.25;
    if (bar && !evidence) continue;
    const cx = seen.left + seen.width / 2;
    const cy = seen.top + seen.height / 2;
    const target = document.elementFromPoint(cx, cy);
    if (!target || (target !== el && !el.contains(target))) continue;
    const button = safeButton(el);
    if (!evidence && !button && !backdropOf(el, seen)) continue;
    const z = Number(style.zIndex);
    dialogs.push({ el, button, z: Number.isFinite(z) ? z : 0 });
  }
  if (!dialogs.length) return null;
  dialogs.sort((a, b) => b.z - a.z);
  const chosen = dialogs[0];
  const text = String(chosen.el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500);
  if (!text && !chosen.button) return null;
  return { text, button: chosen.button };
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
        y_mark = None
        if isinstance(button, dict):
            y_mark = round(float(button.get("y") or 0), 1)
        mark = (text, y_mark)
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
