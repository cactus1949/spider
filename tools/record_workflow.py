"""交互式工作流录制器 —— Playwright CDP + 浏览器注入，直接输出 spider-gd YAML。

用法:
    python tools/record_workflow.py --url https://pm.gd.csg.cn/portal/#/home -o workflows/my.yml -n "测试"

启动后会在浏览器左下角显示一个录制控制面板，操作完成后点「停止录制」即可。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from recon_tool.stealth import connect_or_launch


# ── JavaScript 注入：事件录制 ────────────────────────────────────────────
_RECORDER_JS = r"""
(() => {
  if (window.__spider_recorder__) return;
  window.__spider_recorder__ = { events: [], running: false, _lastHover: '' };

  /* ── 选择器生成：优先 text= 和 has-text ── */
  function uniqueSelector(el) {
    if (!el || el === document.body || el === document.documentElement) return 'body';
    const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 50);
    const ownText = (Array.from(el.childNodes)
      .filter(n => n.nodeType === 3)
      .map(n => n.textContent)
      .join('') || '').trim().replace(/\s+/g, ' ');
    const tag = el.tagName.toLowerCase();
    const safeText = (t) => t.replace(/'/g, "\\'");

    // 1) link / menu-item / tab → text= 最优先
    if (ownText && ownText.length <= 20 && ownText.length >= 1) {
      if (/[\u4e00-\u9fff]/.test(ownText) && ownText.length <= 10) {
        return `text=${safeText(ownText)}`;
      }
    }
    // 兜底用完整 text
    if (text && text.length <= 20 && /[\u4e00-\u9fff]/.test(text) && text.length <= 12) {
      const sel = `text=${safeText(text)}`;
      if (document.querySelectorAll(sel).length === 1) return sel;
    }

    // 2) button / a 有文本 → has-text
    if ((tag === 'button' || tag === 'a') && text && text.length < 25) {
      if (!/^[\s\u00A0×✕✖❌⬆⬇▶❯…+]$/u.test(text) && text.length > 1) {
        return `${tag}:has-text('${safeText(text.slice(0, 20))}')`;
      }
    }

    // 3) 有 ID → #id
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
      return '#' + CSS.escape(el.id);
    }

    // 4) class 选择器（排除 element-ui 的随机 class）
    if (el.className && typeof el.className === 'string') {
      const cls = el.className.trim().split(/\s+/).filter(c =>
        c && !/^el[-_]/.test(c) && !/^is-/.test(c) && c.length >= 2 && c.length < 40
      );
      if (cls.length > 0) {
        for (let k = cls.length; k >= 1; k--) {
          const sel = tag + '.' + cls.slice(0, k).join('.');
          if (document.querySelectorAll(sel).length === 1) return sel;
        }
      }
    }

    // 5) 兜底：tag + 有意义的属性
    const role = el.getAttribute('role');
    const aria = el.getAttribute('aria-label');
    if (ownText && ownText.length < 20 && /[\u4e00-\u9fff]/.test(ownText)) {
      const parent = el.closest('li, td, .el-menu-item, nav a');
      if (parent) {
        const ptext = (parent.textContent || '').trim().slice(0, 20);
        if (ptext && /[\u4e00-\u9fff]/.test(ptext)) {
          return `text=${safeText(ptext.slice(0, 10))}`;
        }
      }
    }

    // 6) 最后才用 nth-child 路径（尽量短）
    let path = [];
    let cur = el;
    while (cur && cur !== document.body && path.length < 3) {
      let seg = cur.tagName.toLowerCase();
      if (cur.id) { seg = '#' + CSS.escape(cur.id); path.unshift(seg); break; }
      path.unshift(seg);
      cur = cur.parentElement;
    }
    return path.join(' > ');
  }

  /* ── 事件记录 ── */
  function record(action, detail) {
    if (!window.__spider_recorder__.running) return;
    window.__spider_recorder__.events.push({
      action,
      ...detail,
      url: location.href,
      time: Date.now()
    });
    updatePanel();
  }

  function updatePanel() {
    const panel = document.getElementById('__spider_panel__');
    if (!panel) return;
    const count = window.__spider_recorder__.events.length;
    const status = window.__spider_recorder__.running ? '● 录制中' : '○ 已停止';
    panel.querySelector('.spider-count').textContent = `${status} · ${count} 步`;
  }

  /* ── mouseenter / hover ── */
  document.addEventListener('mouseenter', (e) => {
    if (!window.__spider_recorder__.running) return;
    const el = e.target.closest('li, .el-menu-item, .el-submenu, nav a, [aria-haspopup], .ant-menu-item, .ant-menu-submenu-title');
    if (!el || el.closest('#__spider_panel__')) return;
    const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 20);
    if (!text || text.length === 0) return;
    // 去重：不重复记录同一个 hover
    if (text === window.__spider_recorder__._lastHover) return;
    window.__spider_recorder__._lastHover = text;
    const sel = uniqueSelector(el);
    record('hover', { selector: sel, text });
  }, true);

  /* ── click ── */
  document.addEventListener('click', (e) => {
    if (e.target.closest('#__spider_panel__')) return;
    const el = e.target.closest('button, a, [role="button"], .el-menu-item, .el-tab-pane, .query-tab, li, td, .ant-menu-item, .el-submenu__title');
    if (!el) return;
    const selector = uniqueSelector(el);
    const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    // 忽略 body 级别的空白点击
    if (!text && el.tagName === 'BODY') return;
    record('click', { selector, text });
    // 如果点击后文本变化了（路由跳转），清除 hover 缓存
    window.__spider_recorder__._lastHover = '';
  }, true);

  /* ── input/change ── */
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!el || el.closest('#__spider_panel__')) return;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      const selector = uniqueSelector(el);
      const value = el.value || '';
      const cls = (el.className || '').toString();
      const ph = (el.placeholder || '').toString();
      const isDate = el.type === 'date' || cls.includes('el-date') || cls.includes('date-picker') ||
                     ph.includes('日期') || ph.includes('date') || ph.includes('选择日期');
      record(isDate ? 'pick_date' : 'fill', { selector, value, text: value });
    }
  }, true);

  // 录制面板 CSS/HTML
  const panelCSS = `
    #__spider_panel__ { position:fixed; bottom:16px; left:16px; z-index:2147483647;
      background:#1a1a2e; color:#e0e0e0; border-radius:10px; padding:12px 16px;
      font-family:-apple-system,sans-serif; font-size:13px; box-shadow:0 4px 24px rgba(0,0,0,.4);
      display:flex; align-items:center; gap:10px; user-select:none; }
    #__spider_panel__ .spider-dot { width:10px; height:10px; border-radius:50%;
      background:#ff4757; animation:spider-pulse 1s infinite; flex-shrink:0; }
    #__spider_panel__ .spider-dot.stopped { background:#666; animation:none; }
    @keyframes spider-pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
    #__spider_panel__ .spider-count { font-size:12px; opacity:.85; min-width:100px; }
    #__spider_panel__ button { border:none; border-radius:6px; padding:4px 12px; cursor:pointer;
      font-size:12px; color:#fff; }
    #__spider_panel__ .spider-stop { background:#ff4757; }
    #__spider_panel__ .spider-stop:hover { background:#ff6b81; }
  `;

  const panel = document.createElement('div');
  panel.id = '__spider_panel__';
  panel.innerHTML = `
    <style>${panelCSS}</style>
    <div class="spider-dot"></div>
    <span class="spider-count">⏸ 已停止</span>
    <button class="spider-stop" onclick="window.__spider_stop__()">停止录制</button>
  `;
  document.body.appendChild(panel);

  window.__spider_stop__ = () => {
    window.__spider_recorder__.running = false;
    const dot = document.querySelector('#__spider_panel__ .spider-dot');
    if (dot) dot.classList.add('stopped');
    updatePanel();
  };

  // 暴露 updatePanel 到全局以便外部调用
  window.__spider_update_panel__ = updatePanel;
})();
"""


# ── Python 端 ────────────────────────────────────────────────────────────

def _calc_date(value: str) -> str:
    """计算日期相对值"""
    if value == "today":
        return "today"
    elif value == "yesterday":
        return "yesterday"
    elif re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        # 转为相对日期
        try:
            d = date.fromisoformat(value)
            diff = (date.today() - d).days
            if diff == 0:
                return "today"
            elif diff == 1:
                return "yesterday"
            else:
                return f"-{diff}d"
        except ValueError:
            return value
    return value


def _clean_events(events: list[dict]) -> list[dict]:
    """后处理：将原始事件转成 spider-gd workflow steps。"""
    steps: list[dict] = []
    seen_selectors: set[str] = set()  # 避免同一步骤重复
    first_url = True

    for ev in events:
        action = ev.get("action", "")
        selector = ev.get("selector", "")
        text = (ev.get("text") or "").strip()
        url = ev.get("url", "")
        value = ev.get("value", "")

        # ── 忽略：空白/无意义事件 ──
        if action == "click" and not selector:
            continue
        if action == "click" and selector == "body":
            continue

        # ── hover: 在下一步 click 之前插入 ──
        if action == "hover":
            if selector and text and selector not in seen_selectors:
                # 只保留中文菜单的 hover（过滤掉无意义的 mouseenter）
                if re.search(r'[\u4e00-\u9fff]', text) and len(text) <= 15:
                    steps.append({"action": "hover", "selector": selector})
                    seen_selectors.add(selector)
            continue

        # ── navigate: 只保留第一次（login），其余忽略（click 自然触发跳转）──
        if action == "navigate":
            if first_url and url:
                steps.append({"action": "login", "url": url})
                first_url = False
            # 后续 navigate 都是 click 触发的副作用，忽略
            continue

        # ── click ──
        if action == "click":
            # 去重：连续相同 selector 只保留一次
            if steps and steps[-1].get("selector") == selector:
                continue

            # ── 导出按钮 ──
            if re.search(r'导出|export|下载|download', text, re.IGNORECASE):
                steps.append({"action": "export", "selector": selector,
                              "save_as": f"export_{'{date}'}.xlsx"})
                seen_selectors.add(selector)
                continue

            # ── 查询按钮 → click + wait ──
            if re.search(r'查询|搜索|search', text, re.IGNORECASE):
                steps.append({"action": "click", "selector": selector})
                steps.append({"action": "wait", "ms": 3000})
                seen_selectors.add(selector)
                continue

            # ── 查看按钮 ──
            if re.search(r'查看|详情|detail|view', text, re.IGNORECASE):
                steps.append({"action": "click", "selector": selector})
                steps.append({"action": "wait", "ms": 2000})
                seen_selectors.add(selector)
                continue

            # ── Tab 切换 ──
            if re.search(r'实际信息|预测信息|日前|实时|月度|年度|电力用户|发电企业|售电公司', text):
                steps.append({"action": "click", "selector": selector})
                steps.append({"action": "wait", "ms": 1500})
                seen_selectors.add(selector)
                continue

            # ── 菜单项（中文短文本，可能在导航菜单中）──
            if re.search(r'[\u4e00-\u9fff]', text) and len(text) <= 12:
                steps.append({"action": "click", "selector": selector})
                steps.append({"action": "wait", "ms": 2500})
                seen_selectors.add(selector)
                continue

            # ── 普通 click ──
            steps.append({"action": "click", "selector": selector})
            seen_selectors.add(selector)
            continue

        # ── pick_date ──
        if action == "pick_date":
            val = _calc_date(value)
            # 去重
            if steps and steps[-1].get("action") == "pick_date" and steps[-1].get("value") == val:
                continue
            steps.append({"action": "pick_date", "selector": selector, "value": val})
            continue

        # ── fill ──
        if action == "fill":
            if not value or len(value) > 50:
                continue
            steps.append({"action": "fill", "selector": selector, "value": value})
            continue

    return steps


async def record_workflow(start_url: str, output: Path, name: str = "录制", description: str = ""):
    """启动浏览器，注入录制脚本，等待用户操作完成后输出 YAML。"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser, chrome_proc, _ = await connect_or_launch(p, headless=False)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )

        pages: list = []
        _stopped = False

        def _on_new_page(new_page):
            pages.append(new_page)
            logger.info("新标签页 #{}", len(pages) - 1)

        context.on("page", _on_new_page)

        page = await context.new_page()
        pages.append(page)

        # 导航到起始页
        await page.goto(start_url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)

        # 注入录制脚本
        await page.evaluate(_RECORDER_JS)

        # 启动录制
        await page.evaluate("() => { window.__spider_recorder__.running = true; window.__spider_update_panel__(); }")
        logger.info("🔴 录制已开始 — 请在浏览器中操作，完成后点击左下角「停止录制」")

        # 同时在新标签页也注入
        def _inject_to_new(page_obj):
            asyncio.ensure_future(_inject_async(page_obj))

        async def _inject_async(p):
            try:
                await p.wait_for_load_state("domcontentloaded", timeout=10000)
                await asyncio.sleep(1)
                await p.evaluate(_RECORDER_JS)
                await p.evaluate("() => { window.__spider_recorder__.running = true; window.__spider_update_panel__(); }")
            except Exception:
                pass

        context.on("page", lambda p: _inject_to_new(p))

        # ── 等待用户点击停止 ──
        # 轮询检查录制状态
        try:
            while True:
                await asyncio.sleep(1)
                try:
                    running = await page.evaluate(
                        "() => window.__spider_recorder__ && window.__spider_recorder__.running"
                    )
                except Exception:
                    running = False

                if not running and not _stopped:
                    _stopped = True
                    logger.info("⏹ 录制已停止，正在收集数据...")
                    await asyncio.sleep(0.5)  # 等最后的事件写入
                    break
        except KeyboardInterrupt:
            logger.info("手动中断")
            # 尝试停止录制
            try:
                await page.evaluate("() => { if(window.__spider_stop__) window.__spider_stop__(); }")
            except Exception:
                pass
            _stopped = True

        # ── 收集所有页面的事件 ──
        all_events: list[dict] = []
        for i, p in enumerate(pages):
            try:
                events = await p.evaluate(
                    "() => window.__spider_recorder__ ? window.__spider_recorder__.events : []"
                )
                logger.info("标签页 #{}: {} 个事件", i, len(events))
                all_events.extend(events)
            except Exception as exc:
                logger.warning("无法读取标签页 #{}: {}", i, exc)

        # ── 清理和转换 ──
        steps = _clean_events(all_events)

        # ── 生成 YAML ──
        workflow = {
            "name": name,
            "description": description or f"从录制自动生成 — {date.today().isoformat()}",
            "steps": steps,
        }

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(yaml.dump(workflow, allow_unicode=True, default_flow_style=False, sort_keys=False),
                          encoding="utf-8")

        logger.success("✅ 录制完成: {} 个步骤 → {}", len(steps), output)

        # 打印摘要
        for i, s in enumerate(steps):
            a = s.get("action", "?")
            sel = s.get("selector", "")
            url = s.get("url", "")
            val = s.get("value", "")
            if a == "login":
                logger.info("  {}. login → {}", i + 1, url)
            elif a == "navigate":
                logger.info("  {}. navigate → {}", i + 1, url)
            elif a == "click":
                logger.info("  {}. click {}", i + 1, sel)
            elif a == "pick_date":
                logger.info("  {}. pick_date {} = {}", i + 1, sel, val)
            elif a == "export":
                logger.info("  {}. export {}", i + 1, sel)
            elif a == "fill":
                logger.info("  {}. fill {}", i + 1, sel)
            elif a == "wait":
                logger.info("  {}. wait {}ms", i + 1, s.get("ms", ""))
            else:
                logger.info("  {}. {} {}", i + 1, a, sel)

        # 关闭浏览器
        await context.close()
        await browser.close()
        if chrome_proc:
            chrome_proc.terminate()

    return steps


# ── CLI ──────────────────────────────────────────────────────────────────
def main():
    import argparse

    parser = argparse.ArgumentParser(description="交互式工作流录制器")
    parser.add_argument("--url", default="https://pm.gd.csg.cn/portal/#/home", help="起始 URL")
    parser.add_argument("-o", "--output", required=True, help="输出 YAML 路径")
    parser.add_argument("-n", "--name", default="录制", help="工作流名称")
    parser.add_argument("-d", "--description", default="", help="工作流描述")
    args = parser.parse_args()

    output = Path(args.output)
    asyncio.run(record_workflow(args.url, output, args.name, args.description))


if __name__ == "__main__":
    main()
