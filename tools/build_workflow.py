"""交互式工作流构建器 —— 像 F12 元素检查器一样逐个点选元素，手动指定动作类型。

用法:
    python tools/build_workflow.py -o workflows/my.yml -n "现货采集"

启动后浏览器右侧出现步骤面板：
  1. 鼠标在页面移动 → 元素蓝色高亮 + 蓝色标签显示选择器
  2. 按键盘快捷键选择动作：
     C = 点击     H = 悬停     D = 日期选择
     F = 填入     E = 导出     X = 提取文字
  3. 高亮的元素被捕获为步骤，右侧面板实时显示
  4. 面板中可：拖拽排序 / 删除
  5. 完成后点「完成」保存并关闭浏览器
"""

import asyncio
import json
import os
import re
import sys
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from recon_tool.stealth import connect_or_launch

# ── 类型元数据推断（复用 workflow_types 逻辑）──
def _infer_workflow_meta(steps: list[dict], description: str = "") -> dict:
    """从步骤列表推断 type / needs_login / start_from / category。"""
    first_action = steps[0].get("action", "") if steps else ""

    # type: 看是否有数据操作（export/extract/pick_date/extract_table/extract_echarts）
    data_actions = {"export", "extract", "extract_table", "extract_echarts", "extract_calendar",
                    "pick_date", "pick_date_range", "collect_api", "intercept_api"}
    has_data_ops = any(s.get("action") in data_actions for s in steps)

    if has_data_ops:
        wf_type = "indicator"
    elif first_action in ("login", "navigate"):
        wf_type = "login"
    else:
        wf_type = "indicator"

    # needs_login
    desc_lower = description.lower()
    if "无需登录" in desc_lower or "无需登陆" in desc_lower or "公开" in desc_lower:
        needs_login = False
    else:
        needs_login = True  # 默认需要登录

    # start_from
    if wf_type == "login":
        start_from = "main"
    elif not needs_login:
        start_from = "main"
    else:
        has_dashboard_click = any(
            s.get("action") in ("click", "hover")
            and any(kw in str(s.get("selector", "")).lower()
                   for kw in ["工作台", "dashboard", "portal", "powerenterprise"])
            for s in steps
        )
        start_from = "main" if has_dashboard_click else "dashboard"

    # category (从步骤文本推测)
    category = ""
    for s in steps:
        # 检查 selector 和 save_as 两个字段
        sel = str(s.get("selector", ""))
        sav = str(s.get("save_as", ""))
        combined = sel + " " + sav
        for kw, cat in [
            ("结算", "结算管理"), ("清算", "结算管理"),
            ("现货", "现货交易"), ("出清", "现货交易"),
            ("披露", "信息披露"), ("负荷", "现货交易"),
            ("节点电价", "现货交易"), ("电价", "现货交易"),
        ]:
            if kw in combined:
                category = cat
                break
        if category:
            break

    return {
        "type": wf_type,
        "needs_login": needs_login,
        "start_from": start_from,
        "category": category,
    }


def _gen_step_comment(step: dict, idx: int) -> str:
    """为步骤生成描述性注释。优先使用用户手写的 _note。"""
    # 用户手写备注优先
    if step.get("_note"):
        return step["_note"]

    action = step.get("action", "")
    selector = step.get("selector", "")
    value = step.get("value", "")
    save_as = step.get("save_as", "")
    ms = step.get("ms", 0)
    text = step.get("_text", "")

    comments = {
        "login": "导航到目标站点",
        "navigate": f"导航到 {step.get('url', '?')}",
        "click": f"点击 {text or selector}",
        "hover": f"悬停在 {text or selector}",
        "fill": f"填入 {value} → {selector}" if value else f"填入 → {selector}",
        "pick_date": f"选择日期 {value} → {selector}",
        "select": f"下拉选择 {value} → {selector}" if value else f"下拉选择 → {selector}",
        "export": f"导出 → {save_as}",
        "extract": f"提取文本 → {save_as}",
        "wait": f"等待 {ms}ms" if ms else f"等待元素 {selector}",
        "check": f"勾选 {selector}",
        "captcha": f"识别验证码 → {step.get('captcha_input', '?')}",
        "pause": f"暂停: {step.get('message', '请手动操作')}",
        "screenshot": f"截图保存: {step.get('name', 'screenshot.png')}",
        "dismiss_modal": "关闭弹窗",
        "switch_tab": f"切换到标签页 {step.get('index', '?')}",
        "close_tab": f"关闭标签页 {step.get('index', '?')}",
    }
    return comments.get(action, f"{action}: {selector}")

# ── 注入用的 XPath countMatches + genSelector ──
_COUNTMATCHES_JS = r"""
function countMatches(sel) {
  try {
    if (sel.startsWith('text=')) {
      const txt = sel.slice(5).replace(/\\'/g, "'");
      // use XPath contains() for broader match
      const xp = "//*[contains(normalize-space(.), '" + txt.replace(/'/g, "\\'") + "')]";
      return document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength;
    }
    if (sel.includes(':has-text(')) {
      const m = sel.match(/^([\w.\-#\[\]=^~|*]+):has-text\('(.+?)'\)$/);
      if (m) {
        const css = m[1], txt = m[2].replace(/\\'/g, "'");
        // use CSS part first, then filter by textContent (more reliable than XPath)
        const els = document.querySelectorAll(css);
        let n = 0;
        for (const el of els) {
          if ((el.textContent||'').includes(txt)) n++;
        }
        return n;
      }
    }
    return document.querySelectorAll(sel).length;
  } catch(e) { return 0; }
}
"""

_GENSELECTOR_JS = r"""
function genSelector(el) {
  if (!el || el === document.body || el === document.documentElement) return 'body';
  const tag = el.tagName.toLowerCase();
  const esc = s => s.replace(/'/g,"\\'").replace(/"/g,'\\"');

  let label = '';
  (function walk(n, d) {
    if (d > 5 || label) return;
    if (n.nodeType === 3) {
      const t = n.textContent.trim().replace(/\s+/g,' ');
      if (/[\u4e00-\u9fff]/.test(t) && t.length <= 15 && (!label || t.length < label.length)) label = t;
    }
    if (n.childNodes) for (const c of n.childNodes) walk(c, d+1);
  })(el, 0);
  if (!label) {
    const t = (el.textContent||'').trim().replace(/\s+/g,' ').slice(0,20);
    if (/[\u4e00-\u9fff]/.test(t)) label = t.slice(0, 12);
  }
  const lbl = label ? esc(label) : '';

  function parentPrefix() {
    const p = el.closest('li, .ant-menu-submenu, .el-submenu, nav, .ant-row, .topBg, .header, [class*="top"], [class*="Top"]');
    if (!p) return '';
    const pt = p.tagName.toLowerCase();
    const pc = (p.className||'').toString().split(/\s+/).filter(c =>
      c && c !== 'ant-menu-submenu-active' && c !== 'ant-menu-submenu-open' &&
      !c.startsWith('el-menu') && !c.startsWith('el-') && !c.startsWith('is-') &&
      c.length>=2 && c.length<35
    ).slice(0,2).join('.');
    return pc ? pt+'.'+pc : pt;
  }

  // 1) text=label
  if (lbl && lbl.length >= 1 && lbl.length <= 15) {
    const s = 'text='+lbl;
    if (countMatches(s) === 1) return s;
    if (countMatches(s) >= 2) {
      const pre = parentPrefix();
      if (pre) { const s2 = pre+' '+s; if (countMatches(s2) === 1) return s2; }
    }
  }

  // 2) has-text on menu components
  if (lbl && /[\u4e00-\u9fff]/.test(lbl)) {
    for (const cls of ['.ant-menu-submenu-title','.el-submenu__title','.ant-menu-item','.el-menu-item']) {
      if (el.closest(cls)) { const s = cls+":has-text('"+lbl+"')"; if (countMatches(s) === 1) return s; }
    }
  }

  // 3) button/a has-text
  if ((tag==='button'||tag==='a') && lbl && lbl.length>=1 && lbl.length<25) {
    if (!/^[\u00d7\u2715\u2716\u274c\u2b06\u2b07\u25b6\u276f\u2026+.\-]$/u.test(lbl) && lbl.length>1) {
      const s = tag+":has-text('"+lbl+"')"; if (countMatches(s) === 1) return s;
    }
  }

  // 4) #id
  if (el.id) { try { const s='#'+CSS.escape(el.id); if(document.querySelectorAll(s).length===1)return s; }catch(e){} }

  // 5) class
  if (el.className && typeof el.className === 'string') {
    const cls = el.className.trim().split(/\s+/).filter(c =>
      c && !/^el[-_]/.test(c) && !/^is-/.test(c) && !/^ant-menu/.test(c) && c.length>=2 && c.length<35
    );
    for (let k=Math.min(cls.length,3); k>=1; k--) {
      const s=tag+'.'+cls.slice(0,k).join('.'); if(document.querySelectorAll(s).length===1)return s;
    }
  }

  // 6) tag:has-text
  if (lbl && lbl.length>=2) { const s=tag+":has-text('"+lbl+"')"; if(countMatches(s)===1)return s; }

  // 7) fallback path
  const path=[]; let cur=el;
  while (cur && cur!==document.body && path.length<4) {
    let seg=cur.tagName.toLowerCase();
    if (cur.id) { seg='#'+CSS.escape(cur.id); path.unshift(seg); break; }
    const cl=(cur.className||'').toString().split(/\s+/)
      .filter(c=>c&&!c.startsWith('el-')&&!c.startsWith('ant-')&&c.length>=2&&c.length<25).slice(0,1).join('.');
    if(cl) seg+='.'+cl;
    path.unshift(seg); cur=cur.parentElement;
  }
  return path.join(' > ');
}
"""

# ── 完整的注入脚本 ──
_BUILDER_JS = (
    "(function(){\n"
    "if (window.__spider_builder__) {\n"
    "  try { const s = localStorage.getItem('__spider_steps__'); if (s) window.__spider_builder__.steps = JSON.parse(s); } catch(e) {}\n"
    "  if (window.__spider_update_sidebar__) window.__spider_update_sidebar__();\n"
    "  return;\n"
    "}\n"
    "const state = { steps: [], highlightEl: null, mode: 'click', _lastEl: null, _dragIdx: -1 };\n"
    "try { const s = localStorage.getItem('__spider_steps__'); if (s) state.steps = JSON.parse(s); } catch(e) {}\n"
    "window.__spider_builder__ = state;\n"
    + _COUNTMATCHES_JS + "\n"
    + _GENSELECTOR_JS + "\n"
    + r"""
/* ===== 高亮层 ===== */
const hl = document.createElement('div');
hl.id='__spider_hl__';
hl.style.cssText='position:fixed;pointer-events:none;z-index:2147483646;border:2px solid rgba(0,122,255,.6);border-radius:6px;background:rgba(0,122,255,.06);display:none;transition:all .1s ease;';
document.body.appendChild(hl);

const lblEl = document.createElement('div');
lblEl.id='__spider_lbl__';
lblEl.style.cssText='position:fixed;pointer-events:none;z-index:2147483647;background:rgba(0,122,255,.9);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);color:#fff;font-size:11px;padding:3px 10px;border-radius:6px 6px 6px 0;display:none;font-family:"SF Mono",Menlo,monospace;white-space:nowrap;max-width:400px;overflow:hidden;text-overflow:ellipsis;box-shadow:0 2px 8px rgba(0,122,255,.25);';
document.body.appendChild(lblEl);

/* 拖拽占位已改用 border-top 内联方式，不再需要单独元素 */


function doHighlight(el) {
  if (!el || el === document.body || el === document.documentElement ||
      el.closest('#__spider_sidebar__') || el === state._lastEl) return;
  state.highlightEl = el; state._lastEl = el;
  const r = el.getBoundingClientRect();
  hl.style.display = 'block';
  hl.style.left = r.left+'px'; hl.style.top = r.top+'px';
  hl.style.width = r.width+'px'; hl.style.height = r.height+'px';
  const sel = genSelector(el);
  lblEl.style.display = 'block';
  lblEl.textContent = sel;
  lblEl.style.left = Math.max(0,r.left)+'px';
  lblEl.style.top = Math.max(0,r.top-24)+'px';
}
function hideHighlight() {
  hl.style.display = 'none'; lblEl.style.display = 'none';
  state.highlightEl = null; state._lastEl = null;
}

/* mouseover tracking */
document.addEventListener('mouseover', e => {
  if (e.target.closest('#__spider_sidebar__')) { hideHighlight(); return; }
  let el = e.target;
  if (!el || el.nodeType !== 1) return;
  const m = el.closest(
    'button, a, li, td, th, .ant-menu-submenu-title, .ant-menu-item, '+
    '.el-submenu__title, .el-menu-item, .el-tab-pane, .query-tab, [role="menuitem"], '+
    '.ant-btn, .el-button, nav span, h1, h2, h3, h4, label, .style-box-head, .xhsb-item, '+
    '.ant-dropdown-menu-item, .ant-menu-submenu, [class*="submenu"], .el-submenu, '+
    '.ant-menu-title-content, span'
  );
  if (m) el = m;
  doHighlight(el);
}, {passive:true});

document.addEventListener('mouseleave', () => { hideHighlight(); }, {passive:true});

/* keydown shortcuts */
document.addEventListener('keydown', e => {
  const tag = (e.target.tagName||'').toUpperCase();
  if (tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||e.target.isContentEditable) return;
  const key = e.key.toUpperCase();
  const map = {C:'click', H:'hover', D:'pick_date', S:'select', F:'fill', E:'export', X:'extract', L:'extract_calendar', W:'wait'};
  const act = map[key];
  if (!act) return;
  e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
  if (act === 'wait') { state.steps.push({action:'wait', ms:2000, _time:Date.now()}); updateSidebar(); return; }
  if (!state.highlightEl) { flashNoTarget(); return; }
  state.mode = act; captureCurrent(); updateSidebar();
}, true);

function flashNoTarget() {
  const d = document.getElementById('__spider_mode__');
  if (!d) return;
  d.style.background='rgba(255,59,48,.08)'; d.style.color='#ff3b30';
  setTimeout(()=>{d.style.background='';d.style.color='';}, 600);
}

function snapHtmlNode(el) {
  const node = { tag: el.tagName.toLowerCase() };
  if (el.id) node.id = el.id;
  const cls = (el.className || '').toString().trim();
  if (cls) node.class = cls.split(/\s+/).filter(function(c){ return c; }).slice(0, 8).join(' ');
  Array.from(el.attributes || []).forEach(function(attr) {
    if (attr.name.indexOf('data-v-') === 0) node[attr.name] = attr.value;
  });
  return node;
}

function isSectionContainer(el) {
  if (!el || el === document.body || el === document.documentElement) return false;
  const selectors = [
    '.style-box-head', '.ant-card', '.ant-card-body', '.ant-table-wrapper',
    '.el-dialog', '.el-dialog__body', '.el-form', '.el-table', '.el-table__body-wrapper',
    'form', 'main', '[role="dialog"]', '[role="main"]',
    '.ant-modal-content', '.ant-drawer-content', '.content-box', '.page-content',
    'nav', '.ant-row', '.query-tab', '.topBg'
  ];
  for (var i = 0; i < selectors.length; i++) {
    try { if (el.matches(selectors[i])) return true; } catch(e) {}
  }
  return false;
}

function captureHtmlContext(el) {
  var path = [];
  var cur = el;
  var sectionEl = null;
  var fallbackEl = null;
  var depth = 0;
  var MAX_DEPTH = 15;
  var FALLBACK_DEPTH = 5;

  while (cur && cur !== document.body && cur !== document.documentElement && depth <= MAX_DEPTH) {
    path.push(snapHtmlNode(cur));
    if (depth === FALLBACK_DEPTH) fallbackEl = cur;
    if (!sectionEl && isSectionContainer(cur)) sectionEl = cur;
    cur = cur.parentElement;
    depth++;
  }

  var section = sectionEl || fallbackEl || el;
  return {
    html: el.outerHTML || '',
    section: section ? section.outerHTML : '',
    path: path
  };
}

function captureCurrent() {
  const el = state.highlightEl; if (!el) return;
  const sel = genSelector(el);
  const text = (el.textContent||'').trim().replace(/\s+/g,' ').slice(0,30);
  const htmlCtx = captureHtmlContext(el);
  const now = Date.now();
  const step = {
    action: state.mode,
    selector: sel,
    _text: text,
    _tag: el.tagName.toLowerCase(),
    _time: now,
    _html: htmlCtx.html,
    _html_section: htmlCtx.section,
    _html_path: htmlCtx.path,
  };
  if (state.mode === 'pick_date') { step.value = 'yesterday'; }
  if (state.mode === 'select')     { step.value = ''; }
  if (state.mode === 'fill')       { step.value = ''; }
  if (state.mode === 'export')     { step.save_as = 'export_{date}.xlsx'; }
  if (state.mode === 'extract')    { step.save_as = 'extracted_{date}.json'; }
  if (state.mode === 'extract_calendar') { step.save_as = '交易日历{month_iso}.csv'; }
  const action = state.mode;
  state.mode = 'click';

  // 页面重载/重复注入可能给同一元素绑两次点击监听，表现为同一时刻连续两步。
  // 只拦“同一动作 + 同一元素 + 同一文本 + 400ms 内”的重复，不拦用户刻意连点。
  const last = state.steps.length ? state.steps[state.steps.length - 1] : null;
  if (last && last.action === action && last.selector === sel && last._text === step._text
      && now - (last._time || 0) < 400) {
    return;
  }
  state.steps.push(step);
}

/* sidebar */
function updateSidebar() {
  let sb = document.getElementById('__spider_sidebar__');
  if (!sb) {
    sb = document.createElement('div'); sb.id = '__spider_sidebar__';
    sb.innerHTML = '<style>'+
      '/* ── macOS Claude Design 侧边栏 ── */'+
      '#__spider_sidebar__{position:fixed;top:12px;right:12px;width:348px;max-height:calc(100vh - 24px);z-index:2147483645;'+
        'background:rgba(255,255,255,0.88);backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);'+
        'color:#1d1d1f;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display",sans-serif;'+
        'font-size:13px;border-radius:14px;box-shadow:0 8px 40px rgba(0,0,0,.12),0 0 0 .5px rgba(0,0,0,.08);'+
        'display:flex;flex-direction:column;overflow:hidden;}'+
      '/* ── 头部 ── */'+
      '.sp-hd{padding:16px 18px 12px;border-bottom:.5px solid rgba(0,0,0,.08);display:flex;align-items:center;justify-content:space-between;'+
        'user-select:none;-webkit-user-select:none;}'+
      '.sp-hd .title{font-weight:600;font-size:15px;color:#1d1d1f;letter-spacing:-.3px;display:flex;align-items:center;gap:6px;}'+
      '.sp-hd .title::before{content:\'\';display:inline-block;width:8px;height:8px;border-radius:50%;background:#007aff;box-shadow:0 0 0 2px rgba(0,122,255,.2);}'+
      '.sp-hd .badge{background:rgba(0,122,255,.1);color:#007aff;border-radius:20px;padding:2px 10px;font-size:11px;font-weight:600;letter-spacing:.2px;}'+
      '/* ── 折叠按钮 ── */'+
      '.sp-toggle{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;'+
        'cursor:pointer;color:rgba(0,0,0,.35);flex-shrink:0;'+
        'order:-1;margin-right:8px;}'+
      '.sp-toggle:hover{background:rgba(0,0,0,.06);color:rgba(0,0,0,.6);}'+
      '/* ── 折叠状态 ── */'+
      '.sp-collapsed{width:44px!important;padding:12px 10px!important;overflow:hidden;}'+
      '.sp-collapsed .sp-hd{padding:0!important;border-bottom:none!important;justify-content:center!important;}'+
      '.sp-collapsed .sp-hd .title,.sp-collapsed .sp-hd .badge,'+
      '.sp-collapsed .sp-list,.sp-collapsed .sp-kbd,.sp-collapsed .sp-ft,'+
      '.sp-collapsed #__spider_drop_end__{display:none!important;}'+
      '.sp-collapsed .sp-toggle{order:0;margin:0 auto;}'+
      '.sp-collapsed .sp-toggle svg{transform:rotate(180deg);}'+
      '/* ── 步骤列表 ── */'+
      '.sp-list{flex:1;overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:6px;'+
        'scrollbar-width:thin;scrollbar-color:rgba(0,0,0,.12) transparent;}'+
      '.sp-list::-webkit-scrollbar{width:4px;}'+
      '.sp-list::-webkit-scrollbar-track{background:transparent;}'+
      '.sp-list::-webkit-scrollbar-thumb{background:rgba(0,0,0,.12);border-radius:20px;}'+
      '/* ── 步骤卡片 ── */'+
      '.sp-step{background:rgba(255,255,255,.7);border-radius:10px;padding:10px 12px;'+
        'display:flex;align-items:flex-start;gap:10px;border:.5px solid rgba(0,0,0,.06);'+
        'transition:all .2s cubic-bezier(.4,0,.2,1);cursor:default;position:relative;'+
        'box-shadow:0 1px 3px rgba(0,0,0,.03);}'+
      '.sp-step:hover{background:rgba(255,255,255,.95);border-color:rgba(0,0,0,.1);'+
        'box-shadow:0 2px 8px rgba(0,0,0,.06);transform:translateY(-1px);}'+
      '.sp-step .idx{color:rgba(0,0,0,.25);font-size:11px;min-width:20px;padding-top:2px;cursor:grab;'+
        'font-weight:500;font-variant-numeric:tabular-nums;text-align:center;}'+
      '.sp-step .body{flex:1;min-width:0;}'+
      '/* ── 动作标签 ── */'+
      '.sp-step .act{display:inline-block;padding:2px 8px;border-radius:5px;font-size:10px;font-weight:600;'+
        'letter-spacing:.2px;margin-bottom:4px;text-transform:lowercase;}'+
      '/* ── 选择器文字 ── */'+
      '.sp-step .sel{color:rgba(0,0,0,.5);font-family:"SF Mono",Menlo,monospace;font-size:11px;word-break:break-all;'+
        'line-height:1.5;letter-spacing:-.1px;}'+
      '/* ── 备注输入框 ── */'+
      '.sp-note{margin-top:5px;width:100%;box-sizing:border-box;'+
        'background:rgba(0,0,0,.03);color:#1d1d1f;border:.5px solid rgba(0,0,0,.08);border-radius:6px;'+
        'padding:5px 8px;font-size:11px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;'+
        'outline:none;transition:all .2s;}'+
      '.sp-note:focus{background:rgba(255,255,255,.9);border-color:rgba(0,122,255,.3);'+
        'box-shadow:0 0 0 3px rgba(0,122,255,.08);}'+
      '.sp-note::placeholder{color:rgba(0,0,0,.25);}'+
      '/* ── 删除按钮 ── */'+
      '.sp-del{color:rgba(0,0,0,.2);cursor:pointer;font-size:15px;padding:0 4px;flex-shrink:0;'+
        'line-height:1;transition:all .15s;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;}'+
      '.sp-del:hover{background:rgba(255,59,48,.1);color:#ff3b30;}'+
      '/* ── 拖拽指示 ── */'+
      '.sp-dragover{border-top:2px solid rgba(0,122,255,.4)!important;margin-top:2px;}'+
      '/* ── 底部操作区 ── */'+
      '.sp-ft{padding:12px 14px;border-top:.5px solid rgba(0,0,0,.08);display:flex;gap:8px;position:relative;}'+
      '.sp-ft button{flex:1;border:none;border-radius:8px;padding:9px 0;font-size:12px;cursor:pointer;font-weight:600;'+
        'letter-spacing:-.1px;transition:all .2s cubic-bezier(.4,0,.2,1);font-family:inherit;}'+
      '.sp-ft .btn-export{background:#007aff;color:#fff;}'+
      '.sp-ft .btn-export:hover{background:#0066d6;transform:scale(1.02);box-shadow:0 2px 8px rgba(0,122,255,.3);}'+
      '.sp-ft .btn-clear{background:rgba(0,0,0,.04);color:rgba(0,0,0,.5);}'+
      '.sp-ft .btn-clear:hover{background:rgba(255,59,48,.08);color:#ff3b30;}'+
      '/* ── 状态信息区 ── */'+
      '.sp-info{padding:8px 18px 10px;font-size:10px;color:rgba(0,0,0,.35);border-top:.5px solid rgba(0,0,0,.05);'+
        'line-height:1.7;letter-spacing:.2px;}'+
      '.sp-info b{color:rgba(0,0,0,.55);font-weight:600;}'+
      '/* ── 键盘快捷键提示 ── */'+
      '.sp-kbd{padding:8px 14px;border-top:.5px solid rgba(0,0,0,.05);display:flex;flex-wrap:wrap;gap:5px;align-items:center;}'+
      '.sp-kbd .kbd-item{display:inline-flex;align-items:center;gap:3px;}'+
      '.sp-kbd .kbd-key{display:inline-flex;align-items:center;justify-content:center;'+
        'min-width:18px;height:18px;padding:0 4px;border-radius:4px;'+
        'background:rgba(0,0,0,.06);color:rgba(0,0,0,.45);font-size:10px;font-weight:600;'+
        'font-family:"SF Mono",Menlo,monospace;letter-spacing:.5px;'+
        'border:.5px solid rgba(0,0,0,.08);}'+
      '.sp-kbd .kbd-label{font-size:10px;color:rgba(0,0,0,.3);white-space:nowrap;}'+
      '.sp-kbd .kbd-mode{font-size:10px;color:rgba(0,0,0,.5);font-weight:500;margin-right:8px;'+
        'display:inline-flex;align-items:center;gap:4px;}'+
      '.sp-kbd .kbd-target{font-size:10px;color:rgba(0,0,0,.35);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:140px;margin-left:auto;}'+
      '/* ── 导出提示消息 ── */'+
      '#__spider_export_msg__{font-size:11px;font-weight:500;letter-spacing:-.1px;}'+
    '</style>'+
    '<div class="sp-hd"><span class="title">工作流步骤</span><span class="badge" id="__spider_count__">0</span>'+
    '<span class="sp-toggle" onclick="(function(){var s=document.getElementById(\'__spider_sidebar__\');s.classList.toggle(\'sp-collapsed\');})()" title="收起侧边栏">'+
      '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" style="display:block;"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'+
    '</span></div>'+
    '<div class="sp-list" id="__spider_list__"></div>'+
    '<div id="__spider_drop_end__" style="flex:0 0 auto;min-height:32px;margin:0 14px;transition:all .15s;border-radius:8px;" '+
      'ondragover="event.preventDefault();var d=document.getElementById(\'__spider_drop_end__\');d.style.minHeight=\'42px\';d.style.background=\'rgba(0,122,255,.06)\';d.style.border=\'2px dashed rgba(0,122,255,.25)\'" '+
      'ondragleave="var d=document.getElementById(\'__spider_drop_end__\');d.style.minHeight=\'32px\';d.style.background=\'\';d.style.border=\'none\'" '+
      'ondrop="event.preventDefault();var d=document.getElementById(\'__spider_drop_end__\');d.style.minHeight=\'32px\';d.style.background=\'\';d.style.border=\'none\';window.__spider_drop_end__()"></div>'+
    '<div class="sp-kbd" id="__spider_mode__"></div>'+
    '<div class="sp-ft">'+
      '<button class="btn-export" onclick="window.__spider_complete__()">完成</button>'+
      '<button class="btn-clear" onclick="window.__spider_clear__()">清空</button>'+
    '</div>';
    document.body.appendChild(sb);
  }

  const colors = {click:'#007aff', hover:'#ff9f0a', pick_date:'#af52de', select:'#5e5ce6', fill:'#30b0c7', export:'#ff375f', extract:'#30b158', extract_calendar:'#ff8c42', wait:'#ac8e68'};
  const modeMap = {click:'点击 C', hover:'悬停 H', pick_date:'日期 D', select:'下拉 S', fill:'填入 F', export:'导出 E', extract:'提取 X', extract_calendar:'日历 L', wait:'等待2s W'};

  document.getElementById('__spider_list__').innerHTML = state.steps.map((s,i) => {
    const c = colors[s.action]||'#86868b';
    const extra = s.action==='pick_date' ? ' <span style="color:#cba6f7">= '+ (s.value||'?') +'</span>' :
                  s.action==='wait' ? ' <span style="color:#fab387">'+s.ms+'ms</span>' :
                  (s.action==='export' || s.action==='extract' || s.action==='extract_calendar') ? ' → '+(s.save_as||'?') : '';
    const noteVal = (s._note || '').replace(/'/g, '&#39;');
    return '<div class="sp-step" draggable="true" '+
      'ondragstart="window.__spider_drag_start__(event,'+i+')" '+
      'ondragend="window.__spider_drag_end__()" '+
      'ondragover="event.preventDefault();window.__spider_drag_over__(event,'+i+')" '+
      'ondragleave="window.__spider_drag_leave__(event)" '+
      'ondrop="event.preventDefault();window.__spider_drop__(event,'+i+')">'+
      '<span class="idx">'+(i+1)+'</span>'+
      '<div class="body">'+
        '<span class="act" style="background:'+c+'22;color:'+c+'">'+s.action+'</span>'+extra+
        '<div class="sel">'+(s.selector || (s.action==='wait'?'—':'?'))+'</div>'+
        '<input class="sp-note" type="text" placeholder="添加备注…" value="'+noteVal+'" '+
          'data-idx="'+i+'" '+
          'onfocus="event.stopPropagation();" '+
          'onclick="event.stopPropagation();" '+
          'onkeydown="event.stopPropagation();" '+
          'oninput="window.__spider_note_input__(event,'+i+')"/>'+
      '</div>'+
      '<span class="sp-del" title="删除" data-idx="'+i+'">✕</span>'+
    '</div>';
  }).join('');

  // 删除事件委托
  const delBtns = document.querySelectorAll('#__spider_list__ .sp-del');
  delBtns.forEach(btn => {
    btn.onclick = function(e) {
      e.stopPropagation();
      const idx = parseInt(this.getAttribute('data-idx'));
      state.steps.splice(idx, 1);
      updateSidebar();
    };
  });

  // 备注输入事件通过 oninput 绑定（在 HTML 中已设置）
  window.__spider_note_input__ = function(e, idx) {
    if (state.steps[idx]) {
      state.steps[idx]._note = e.target.value;
      try { localStorage.setItem('__spider_steps__', JSON.stringify(state.steps)); } catch(e) {}
    }
  };

  document.getElementById('__spider_count__').textContent = state.steps.length;

  const curSel = state.highlightEl ? genSelector(state.highlightEl) : '';
  const curText = state.highlightEl ? (state.highlightEl.textContent||'').trim().replace(/\s+/g,' ').slice(0,24) : '';
  const modeColor = colors[state.mode]||'#86868b';
  const modeLabel = (modeMap[state.mode]||state.mode).replace(/ .*/, '');  // 只要中文名
  const modeKey = (modeMap[state.mode]||'').split(' ')[1]||'';  // 快捷键字母

  // 生成快捷键提示
  const allKeys = [
    {key:'C', label:'点击', color:colors.click},
    {key:'H', label:'悬停', color:colors.hover},
    {key:'D', label:'日期', color:colors.pick_date},
    {key:'S', label:'下拉', color:colors.select},
    {key:'F', label:'填入', color:colors.fill},
    {key:'E', label:'导出', color:colors.export},
    {key:'X', label:'提取', color:colors.extract},
    {key:'L', label:'日历', color:colors.extract_calendar},
    {key:'W', label:'等待', color:colors.wait},
  ];

  let kbdHtml = '<span class="kbd-mode" style="color:'+modeColor+'">'+modeLabel+'</span>';
  allKeys.forEach(function(k) {
    const active = k.key === modeKey ? ';background:'+k.color+';color:#fff;border-color:'+k.color : '';
    kbdHtml += '<span class="kbd-item"><kbd class="kbd-key" style="'+active+'">'+k.key+'</kbd>'+
      '<span class="kbd-label">'+k.label+'</span></span>';
  });
  if (curSel) {
    kbdHtml += '<span class="kbd-target">'+curText+'</span>';
  }

  document.getElementById('__spider_mode__').innerHTML = kbdHtml;

  // 持久化
  try { localStorage.setItem('__spider_steps__', JSON.stringify(state.steps)); } catch(e) {}
}

/* 拖拽 */
var __spider_scroll_timer__ = null;
window.__spider_drag_start__ = function(e, i) {
  state._dragIdx = i;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', '');
  // 给被拖拽的步骤添加拖拽中样式
  var steps = document.querySelectorAll('#__spider_list__ .sp-step');
  if (steps[i]) steps[i].style.opacity = '0.4';
};
window.__spider_drag_end__ = function() {
  state._dragIdx = -1;
  if (__spider_scroll_timer__) { clearInterval(__spider_scroll_timer__); __spider_scroll_timer__ = null; }
  document.querySelectorAll('.sp-step').forEach(function(el) {
    el.style.opacity = '';
    el.classList.remove('sp-dragover');
  });
};
window.__spider_drag_over__ = function(e, i) {
  e.preventDefault();
  var list = document.getElementById('__spider_list__');
  var steps = list.querySelectorAll('.sp-step');
  steps.forEach(function(el) { el.classList.remove('sp-dragover'); });
  // 在目标步骤前显示插入线
  if (i >= 0 && i < steps.length) {
    steps[i].classList.add('sp-dragover');
  }

  // ── 自动滚动 ──
  var rect = list.getBoundingClientRect();
  var y = e.clientY;
  var topZone = rect.top + 40;
  var bottomZone = rect.bottom - 40;

  if (__spider_scroll_timer__) { clearInterval(__spider_scroll_timer__); __spider_scroll_timer__ = null; }

  if (y < topZone && list.scrollTop > 0) {
    // 靠近顶部 → 向上滚动
    __spider_scroll_timer__ = setInterval(function() {
      list.scrollTop -= 8;
      if (list.scrollTop <= 0) { clearInterval(__spider_scroll_timer__); __spider_scroll_timer__ = null; }
    }, 30);
  } else if (y > bottomZone && list.scrollTop + list.clientHeight < list.scrollHeight) {
    // 靠近底部 → 向下滚动
    __spider_scroll_timer__ = setInterval(function() {
      list.scrollTop += 8;
      if (list.scrollTop + list.clientHeight >= list.scrollHeight) {
        clearInterval(__spider_scroll_timer__); __spider_scroll_timer__ = null;
      }
    }, 30);
  }
};
window.__spider_drag_leave__ = function(e) {
  if (__spider_scroll_timer__) { clearInterval(__spider_scroll_timer__); __spider_scroll_timer__ = null; }
};
window.__spider_drop__ = function(e, toIdx) {
  if (__spider_scroll_timer__) { clearInterval(__spider_scroll_timer__); __spider_scroll_timer__ = null; }
  if (state._dragIdx < 0 || state._dragIdx === toIdx) return;
  var item = state.steps.splice(state._dragIdx, 1)[0];
  var adjustIdx = state._dragIdx < toIdx ? toIdx - 1 : toIdx;
  state.steps.splice(adjustIdx, 0, item);
  state._dragIdx = -1;
  updateSidebar();
};
window.__spider_drop_end__ = function() {
  // 拖到列表末尾空白区域 → 移到最后一个位置
  if (__spider_scroll_timer__) { clearInterval(__spider_scroll_timer__); __spider_scroll_timer__ = null; }
  if (state._dragIdx < 0) return;
  var item = state.steps.splice(state._dragIdx, 1)[0];
  state.steps.push(item);
  state._dragIdx = -1;
  updateSidebar();
};

window.__spider_clear__ = function() { state.steps = []; updateSidebar(); };
window.__spider_complete__ = function() {
  // 检查是否有步骤
  if (!state.steps.length) {
    var ft = document.querySelector('.sp-ft');
    if (ft) {
      var warn = document.createElement('div');
      warn.textContent = '没有步骤，请先捕获';
      warn.style.cssText = 'color:#ff3b30;font-size:11px;font-weight:500;padding:4px 0;text-align:center;position:absolute;bottom:100%;left:0;right:0;';
      ft.style.position = 'relative';
      ft.appendChild(warn);
      setTimeout(function(){ if(warn.parentNode) warn.remove(); }, 2000);
    }
    return;
  }
  state._exportRequested = true;
  state._finishRequested = true;
  var ft = document.querySelector('.sp-ft');
  if (ft) {
    var msg = document.createElement('div');
    msg.id = '__spider_export_msg__';
    msg.textContent = '正在保存...';
    msg.style.cssText = 'color:rgba(0,0,0,.4);font-size:11px;font-weight:500;padding:4px 0;text-align:center;position:absolute;bottom:100%;left:0;right:0;';
    ft.style.position = 'relative';
    var old = document.getElementById('__spider_export_msg__');
    if (old) old.remove();
    ft.appendChild(msg);
  }
};

// Python 端调用此函数显示保存结果
window.__spider_show_saved__ = function(paths, finishing) {
  var msg = document.getElementById('__spider_export_msg__');
  if (msg) {
    msg.textContent = finishing ? '已保存，正在关闭...' : ('已保存到: ' + paths.join(', '));
    msg.style.color = '#30b158';
    if (!finishing) setTimeout(function(){ if(msg.parentNode) msg.remove(); }, 8000);
  }
};
window.__spider_update_sidebar__ = updateSidebar;

// Watchdog: SPA 导航会销毁 DOM，定时检查并重建侧边栏
setInterval(function() {
  if (!document.getElementById('__spider_sidebar__') || !document.getElementById('__spider_hl__')) {
    if (document.body) updateSidebar();
  }
}, 500);

})();
"""
)


def _calc_date(value: str) -> str:
    if value == "today": return "today"
    elif value == "yesterday": return "yesterday"
    elif re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        try:
            d = date.fromisoformat(value)
            diff = (date.today() - d).days
            if diff == 0: return "today"
            elif diff == 1: return "yesterday"
            else: return f"-{diff}d"
        except ValueError: return value
    return value


def _dedupe_raw_steps(raw: list[dict], window_ms: int = 500) -> list[dict]:
    """去掉同一次点击被重复捕获的步骤。

    注入脚本在页面重载/重复注入时可能给同一个 click 绑两次监听，表现为连续两步
    ``action/selector/_text`` 完全相同且 ``_time`` 相同（或相差几毫秒）。
    这里只合并“同一动作 + 同一元素 + 极短时间窗”的相邻步骤，不影响用户刻意连点。
    """
    deduped: list[dict] = []
    for step in raw:
        if deduped:
            prev = deduped[-1]
            same_target = (
                step.get("action") == prev.get("action")
                and step.get("selector") == prev.get("selector")
                and step.get("_text") == prev.get("_text")
            )
            prev_time = prev.get("_time")
            step_time = step.get("_time")
            same_moment = (
                isinstance(prev_time, (int, float))
                and isinstance(step_time, (int, float))
                and abs(step_time - prev_time) <= window_ms
            )
            if same_target and same_moment:
                continue
        deduped.append(step)
    return deduped


def _clean_steps(raw: list[dict], start_url: str = "", pages_info: list[dict] | None = None) -> list[dict]:
    """清洗原始步骤数据，添加合理的默认值、超时、注释。

    pages_info: [{"page_idx":0, "url":"...", "title":"..."}] 用于追踪步骤所属页面
    """
    steps = []
    for s in _dedupe_raw_steps(raw):
        a = s.get("action", "click")
        step = {"action": a}
        # 保留原始元数据（用于注释生成）
        if s.get("_text"):
            step["_text"] = s["_text"]
        if s.get("_tag"):
            step["_tag"] = s["_tag"]
        if s.get("_page_idx") is not None:
            step["_page_idx"] = s["_page_idx"]
        if s.get("_note"):
            step["_note"] = s["_note"]
        if s.get("_html"):
            step["_html"] = s["_html"]
        if s.get("_html_section"):
            step["_html_section"] = s["_html_section"]
        if s.get("_html_path"):
            step["_html_path"] = s["_html_path"]

        if a == "wait":
            step["ms"] = s.get("ms", 2000)
            steps.append(step)
            continue
        if a == "pause":
            step["message"] = s.get("message", "请手动操作后继续...")
            steps.append(step)
            continue
        if a == "screenshot":
            step["name"] = s.get("name", f"screenshot_{int(time.time())}.png")
            steps.append(step)
            continue

        step["selector"] = s.get("selector", "")
        if a == "pick_date":
            step["value"] = _calc_date(s.get("value", "yesterday"))
            if not s.get("timeout"):
                step["timeout"] = 15000
        elif a == "select":
            step["value"] = s.get("value", "")
            if not s.get("timeout"):
                step["timeout"] = 10000
        elif a == "fill":
            step["value"] = s.get("value", "")
        elif a == "export":
            step["save_as"] = s.get("save_as", "export_{date}.xlsx")
            if not s.get("timeout"):
                step["timeout"] = 60000
        elif a == "extract":
            step["save_as"] = s.get("save_as", "extracted_{date}.json")
        elif a == "extract_calendar":
            step["save_as"] = s.get("save_as", "交易日历{month_iso}.csv")
        elif a == "click":
            if not s.get("timeout"):
                step["timeout"] = 10000
            if s.get("_new_tab"):
                step["expect"] = "new_tab"
        elif a == "hover":
            if not s.get("timeout"):
                step["timeout"] = 5000
        elif a == "check":
            pass

        steps.append(step)

    # 自动插入 login 步骤
    has_nav = any(s["action"] in ("login", "navigate") for s in steps)
    if not has_nav:
        steps.insert(0, {"action": "login", "url": start_url or "https://pm.gd.csg.cn/portal/#/home"})

    # 自动插入 wait 步骤 + 推断 expect:new_tab
    result = []
    for i, s in enumerate(steps):
        result.append(s)

        # 推断 expect: new_tab（如果下一步骤在另一个页面）
        if s["action"] in ("click",) and i + 1 < len(steps):
            next_s = steps[i + 1]
            s_pg = s.get("_page_idx")
            n_pg = next_s.get("_page_idx")
            if s_pg is not None and n_pg is not None and s_pg != n_pg:
                if "expect" not in s:
                    s["expect"] = "new_tab"
        if s["action"] in ("click", "hover", "select", "pick_date") and i + 1 < len(steps):
            next_a = steps[i + 1]["action"]
            if next_a in ("click", "hover", "select", "pick_date", "fill", "export", "extract",
                          "extract_table", "extract_calendar", "check"):
                if not (result and result[-1].get("action") == "wait"):
                    wait_ms = 1500
                    if s["action"] == "click" and next_a == "click":
                        sel = str(s.get("selector", ""))
                        if any(kw in sel for kw in ["menu", "submenu", "tab", "导航"]):
                            wait_ms = 800
                    result.append({"action": "wait", "ms": wait_ms})

    return result


def _build_rich_yaml(name: str, description: str, steps: list[dict], meta: dict,
                    pages_info: list[dict], raw_steps: list[dict],
                    start_url: str, build_time: str) -> str:
    """构建富含注释和信息的工作流 YAML 文本。"""

    lines = []
    # ── 头部注释 ──
    lines.append(f"# ═══════════════════════════════════════════════════════════════")
    lines.append(f"# 工作流: {name}")
    lines.append(f"# 构建时间: {build_time}")
    lines.append(f"# 起始 URL: {start_url}")
    lines.append(f"# 步骤总数: {len(steps)} (原始捕获 {len(raw_steps)})")
    if pages_info:
        lines.append(f"# 涉及页面: {len(pages_info)} 个")
        for pi in pages_info:
            lines.append(f"#   标签页{pi['page_idx']}: {pi.get('title','?')[:60] or pi.get('url','?')[:60]}")
    lines.append(f"#")
    lines.append(f"# 类型推断: type={meta['type']}, needs_login={meta['needs_login']}, start_from={meta['start_from']}")
    if meta.get("category"):
        lines.append(f"# 分类: {meta['category']}")
    if description:
        lines.append(f"# 描述: {description}")
    lines.append(f"# ═══════════════════════════════════════════════════════════════")
    lines.append("")

    # ── 元数据 ──
    wf_dict: dict[str, Any] = {
        "name": name,
        "type": meta["type"],
        "needs_login": meta["needs_login"],
        "start_from": meta["start_from"],
    }
    if meta.get("category"):
        wf_dict["category"] = meta["category"]
    wf_dict["description"] = description or f"构建 — {build_time}"

    # 用 yaml.dump 输出元数据部分（不含 steps）
    meta_yaml = yaml.dump(wf_dict, allow_unicode=True, default_flow_style=False, sort_keys=False)
    lines.append(meta_yaml.rstrip())

    # ── 步骤（带注释）──
    lines.append("steps:")
    prev_page_idx = -1
    for i, step in enumerate(steps):
        action = step.get("action", "")
        page_idx = step.get("_page_idx", 0)

        # 页面切换时添加分隔注释
        if page_idx != prev_page_idx and pages_info and page_idx < len(pages_info):
            pi = pages_info[page_idx]
            lines.append(f"  # ── 标签页 {page_idx}: {pi.get('title','')[:50] or pi.get('url','')[:50]} ──")
            prev_page_idx = page_idx

        # 步骤注释
        comment = _gen_step_comment(step, i)
        lines.append(f"  # {i+1}. {comment}")

        # 步骤内容（去除内部元数据字段）
        step_clean = {k: v for k, v in step.items() if not k.startswith("_")}
        step_yaml = yaml.dump([step_clean], allow_unicode=True, default_flow_style=False, sort_keys=False)
        # yaml.dump 输出如:
        #   - action: click
        #     selector: ...
        # 需要整体缩进 2 空格以对齐 "steps:" 下
        for line in step_yaml.strip().split("\n"):
            lines.append(f"  {line}")

    # ── 构建元数据（保留供后续使用）──
    lines.append("")
    lines.append("# ── 构建元数据（供后续完善参考）──")
    lines.append(f"# _build_meta:")
    lines.append(f"#   build_time: {build_time}")
    lines.append(f"#   start_url: {start_url}")
    lines.append(f"#   raw_steps_count: {len(raw_steps)}")
    lines.append(f"#   pages_count: {len(pages_info)}")

    return "\n".join(lines) + "\n"


def _safe_export_name(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)[:64] or "workflow"


def _pack_export_zip(zip_path: Path, files: list[Path]) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in files:
            if fp.exists():
                zf.write(fp, fp.name)
    return zip_path


async def build_workflow(
    start_url: str,
    output: Path,
    name: str,
    description: str,
    *,
    export_dir: Path | None = None,
) -> list[str]:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser, chrome_proc, _ = await connect_or_launch(p, headless=False)
        contexts = browser.contexts
        if contexts:
            ctx = contexts[0]
            pages = list(ctx.pages)
        else:
            ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
            pages = []
        if not pages:
            page = await ctx.new_page()
            pages.append(page)
        else:
            page = pages[0]
        await page.goto(start_url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)
        await page.evaluate(_BUILDER_JS)

        injected = {page}
        # 页面追踪：记录每个标签页的 URL 和标题
        pages_info: list[dict] = [{"page_idx": 0, "url": page.url, "title": await page.title()}]
        # 记录每个步骤发生在哪个标签页
        _last_page_count = len(pages)

        async def _on_new_page(p):
            nonlocal _last_page_count
            if p not in injected:
                pages.append(p)
                injected.add(p)
                try:
                    await p.wait_for_load_state("domcontentloaded", timeout=10000)
                    await asyncio.sleep(1)
                    await p.evaluate(_BUILDER_JS)
                    page_idx = len(pages_info)
                    pages_info.append({
                        "page_idx": page_idx,
                        "url": p.url,
                        "title": await p.title(),
                    })
                    _last_page_count = len(pages)
                    logger.info("🔧 新标签页已注入构建器: {}", p.url)
                except Exception as exc:
                    logger.warning("新标签页注入失败: {}", exc)

        ctx.on("page", _on_new_page)

        logger.info("🔧 构建器就绪  |  鼠标移动→蓝色高亮  |  C点击 H悬停 D日期 S下拉 F填入 E导出 X提取 L日历 W等待")
        logger.info("   支持新开标签页，自动注入")
        logger.info("   📋 右侧面板: 拖拽排序 / 删除步骤")
        logger.info("   📥 完成后点「完成」保存 ZIP 并关闭浏览器")

        _export_count = 0
        exported_zips: list[str] = []
        _disconnected = asyncio.Event()
        browser.on("disconnected", lambda: _disconnected.set())
        _should_finish = False

        while not _disconnected.is_set() and not _should_finish:
            if not browser.is_connected():
                break
            if len(ctx.pages) == 0:
                await asyncio.sleep(2)
                if len(ctx.pages) == 0:
                    break
            await asyncio.sleep(1)
            for c in browser.contexts:
                for p in c.pages:
                    if p not in injected:
                        await _on_new_page(p)
            for p in list(injected):
                try:
                    ok = await p.evaluate("() => !!document.getElementById('__spider_sidebar__')")
                    if not ok:
                        await p.evaluate(_BUILDER_JS)
                        try:
                            cnt = await p.evaluate("() => window.__spider_builder__ ? window.__spider_builder__.steps.length : 0")
                            if cnt > 0:
                                logger.info("🔧 页面重载后恢复 {} 个步骤", cnt)
                        except Exception:
                            pass
                except Exception:
                    pass

            # 检查是否有导出请求
            export_requested = False
            for p in pages:
                try:
                    if await p.evaluate("() => window.__spider_builder__ && window.__spider_builder__._exportRequested"):
                        export_requested = True
                        break
                except Exception:
                    continue

            if not export_requested:
                continue

            # ── 处理导出 ──
            _export_count += 1
            finish_requested = False
            for p in pages:
                try:
                    finish_requested = await p.evaluate(
                        "() => !!(window.__spider_builder__ && window.__spider_builder__._finishRequested)"
                    )
                    if finish_requested:
                        break
                except Exception:
                    continue

            for p in pages:
                try:
                    await p.evaluate(
                        "() => { if (window.__spider_builder__) { "
                        "window.__spider_builder__._exportRequested = false; "
                        "window.__spider_builder__._finishRequested = false; } }"
                    )
                except Exception:
                    pass

            all_raw = []
            for i, p in enumerate(pages):
                try:
                    raw = await p.evaluate("() => window.__spider_builder__ ? window.__spider_builder__.steps : []")
                    for step in raw:
                        step["_page_idx"] = i
                    logger.info("标签页 #{}: {} 个步骤", i, len(raw))
                    all_raw.extend(raw)
                except Exception as exc:
                    logger.warning("无法读取标签页 #{}: {}", i, exc)

            if not all_raw:
                logger.warning("未捕获步骤，跳过导出")
                continue

            # 更新 pages_info
            for pi in pages_info:
                try:
                    p = pages[pi["page_idx"]]
                    pi["title"] = await p.title()
                    pi["url"] = p.url
                except Exception:
                    pass

            steps = _clean_steps(all_raw, start_url, pages_info)
            meta = _infer_workflow_meta(steps, description)
            build_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 多次导出时追加序号
            export_output = output
            if _export_count > 1:
                export_output = output.with_stem(f"{output.stem}_{_export_count}")

            # ── 1. 富信息 YAML ──
            rich_yaml = _build_rich_yaml(
                name, description, steps, meta, pages_info, all_raw, start_url, build_time
            )
            export_output.parent.mkdir(parents=True, exist_ok=True)
            export_output.write_text(rich_yaml, encoding="utf-8")
            logger.success("✅ 富信息 YAML → {} ({} 步骤)", export_output, len(steps))

            # ── 2. 原始数据 JSON ──
            raw_output = export_output.with_suffix(".raw.json")
            export_id = str(uuid.uuid4())
            raw_export = {
                "export_id": export_id,
                "build_time": build_time,
                "start_url": start_url,
                "name": name,
                "description": description,
                "meta": meta,
                "pages_info": pages_info,
                "raw_steps": all_raw,
                "cleaned_steps": steps,
            }
            raw_output.write_text(json.dumps(raw_export, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.success("✅ 原始数据 → {} (完整上下文)", raw_output)

            # ── 3. 精简 YAML ──
            minimal_output = export_output.with_suffix(".min.yml")
            minimal_wf = {
                "name": name,
                "type": meta["type"],
                "needs_login": meta["needs_login"],
                "start_from": meta["start_from"],
                "description": description or f"构建 — {build_time}",
                "steps": [{k: v for k, v in s.items() if not k.startswith("_")} for s in steps],
            }
            if meta.get("category"):
                minimal_wf["category"] = meta["category"]
            minimal_output.write_text(
                yaml.dump(minimal_wf, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
            logger.success("✅ 精简 YAML → {} (可直接运行)", minimal_output)

            logger.info("── 步骤预览 ──")
            for i, s in enumerate(steps):
                logger.info("  {}. {} | {}", i + 1, s.get("action", "?"),
                           s.get("selector", "") or s.get("message", "") or f"wait {s.get('ms','')}ms")

            saved_paths = [str(export_output), str(minimal_output), str(raw_output)]
            if export_dir is not None:
                zip_stem = _safe_export_name(name)
                zip_path = export_dir / f"{zip_stem}.zip"
                _pack_export_zip(zip_path, [export_output, minimal_output, raw_output])
                exported_zips.append(str(zip_path))
                logger.success("✅ ZIP → {}", zip_path)
                saved_paths = [str(zip_path)]

            for p in pages:
                try:
                    await p.evaluate(
                        f"() => {{ if (window.__spider_show_saved__) "
                        f"window.__spider_show_saved__({json.dumps(saved_paths)}, {json.dumps(finish_requested)}) }}"
                    )
                except Exception:
                    pass

            if finish_requested:
                logger.info("✅ 构建完成，关闭浏览器")
                await asyncio.sleep(1.2)
                _should_finish = True

        try:
            await ctx.close()
        except Exception:
            pass
        try:
            if browser.is_connected():
                await browser.close()
        except Exception:
            pass
        if chrome_proc:
            try:
                chrome_proc.terminate()
            except Exception:
                pass

        return exported_zips


def main():
    import argparse
    p = argparse.ArgumentParser(
        description="交互式工作流构建器 —— 像 F12 元素检查器一样逐个点选元素",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python tools/build_workflow.py -o workflows/test.yml -n "测试"
  python tools/build_workflow.py -o workflows/spot.yml -n "现货采集" --url https://pm.gd.csg.cn/portal/#/powerEnterprise
  python tools/build_workflow.py -o workflows/demo.yml -n "演示" -d "演示用工作流"

输出文件:
  {output}.yml      富信息 YAML（含注释、元数据、页面上下文）
  {output}.min.yml  精简 YAML（可直接 workflow_runner 运行）
  {output}.raw.json 完整原始数据（可重新处理）
  --export-dir 指定时额外打包为 {export-dir}/{name}.zip
"""
    )
    p.add_argument("--url", default="https://pm.gd.csg.cn/portal/#/home",
                   help="起始 URL (默认: https://pm.gd.csg.cn/portal/#/home)")
    p.add_argument("-o", "--output", required=True,
                   help="输出文件路径（生成 .yml / .min.yml / .raw.json 三个文件）")
    p.add_argument("-n", "--name", default="构建",
                   help="工作流名称 (默认: 构建)")
    p.add_argument("-d", "--description", default="",
                   help="工作流描述")
    p.add_argument("--export-dir", default="",
                   help="ZIP 导出目录（如 下载路径/录制工坊）")
    p.add_argument("--category", default="",
                   help="工作流分类（自动推断也可手动指定）")
    args = p.parse_args()
    export_dir = Path(args.export_dir) if args.export_dir else None
    asyncio.run(build_workflow(
        args.url, Path(args.output), args.name, args.description, export_dir=export_dir,
    ))


if __name__ == "__main__":
    main()
