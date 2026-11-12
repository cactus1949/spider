"""RPA 工作流执行器 —— YAML 驱动 CDP 操控真实 Chrome。

用法:
    PYTHONPATH=tools:src python tools/workflow_runner.py workflows/spot_export.yml

YAML 格式见下方示例或 workflows/ 目录。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import io
import json
import random
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# 确保 tools/ 与 src/ 在 sys.path
_TOOLS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TOOLS_DIR.parent / "src"
for _path in (_TOOLS_DIR, _SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from calendar_data import (  # noqa: E402
    build_calendar_table,
    calendar_csv_text,
    calendar_rows_from_api,
    merge_calendar_rows,
    normalize_month,
)
from dom_probe import DOM_PROBE_JS  # noqa: E402

from spider_gd.core.crawler.exceptions import CaptchaError, is_playwright_goto_error  # noqa: E402
from spider_gd.core.crawler.shared_browser import (  # noqa: E402
    GD_LOGIN_SUCCESS_SELECTOR,
    GD_WORKBENCH_USER,
    is_public_portal_url,
)
from spider_gd.core.dates import (  # noqa: E402
    format_d_offset,
    pin_step_dates,
    plan_repeat_rounds,
    resolve_repeat_dates,
    step_output_dates,
    weeks_of_month,
)
from spider_gd.core.pipeline.file_versions import (  # noqa: E402
    save_versioned_bytes,
    save_versioned_file,
)
from os.path import commonpath  # noqa: E402

from spider_gd.core.workflow_outputs import OUTPUT_ACTIONS, save_as_filename  # noqa: E402

def render_save_as(save_as: str, current: date | None) -> str:
    """把 save_as 里的日期占位符换成具体文件名。

    Args:
        save_as: 步骤上的文件名模板，可以带 ``{date}`` 等占位符。
        current: 用来替换占位符的日期。为空时用今天。

    Returns:
        str: 去掉目录后的文件名。模板为空时返回空字符串。

    Examples:
        >>> render_save_as("用电量_{date}.xlsx", date(2026, 11, 2))
        '用电量_2026-11-02.xlsx'
        >>> render_save_as("", date(2026, 11, 2))
        ''
    """
    today = current or date.today()
    previous = today.replace(day=1) - timedelta(days=1)
    rendered = str(save_as or "").strip()
    if not rendered:
        return ""
    try:
        rendered = rendered.format(
            date=today.isoformat(),
            date_compact=today.strftime("%Y%m%d"),
            year=today.strftime("%Y"),
            month=today.strftime("%Y%m"),
            month_iso=today.strftime("%Y-%m"),
            previous_month=previous.strftime("%Y%m"),
            previous_month_iso=previous.strftime("%Y-%m"),
        )
    except (KeyError, IndexError, ValueError):
        pass
    return save_as_filename(rendered)


def download_tree_root(download_dir: Path, output_dirs: dict[str, str] | None = None) -> Path:
    """取下载目录和各输出目录的公共父目录，用来折叠存储结构树。

    Args:
        download_dir: 指标默认落盘目录。
        output_dirs: 每个输出自己的目录。

    Returns:
        Path: 这些目录的公共路径。只有一个目录时返回它自己。

    Examples:
        >>> str(download_tree_root(Path("/data/task"), {"s0": "/data/task/wifi/day"}))
        '/data/task'
    """
    paths = [Path(download_dir)]
    for raw in (output_dirs or {}).values():
        if str(raw).strip():
            paths.append(Path(raw))
    if len(paths) == 1:
        return paths[0]
    return Path(commonpath([str(item) for item in paths]))


def relative_folders(path: Path, root: Path) -> list[str]:
    """返回文件相对根目录的父级目录名。

    Args:
        path: 文件路径。
        root: 树的根目录。

    Returns:
        list[str]: 不含文件名的目录分段。不在根目录下时返回空列表。

    Examples:
        >>> relative_folders(Path("/data/a/b/c.xlsx"), Path("/data"))
        ['a', 'b']
        >>> relative_folders(Path("/other/c.xlsx"), Path("/data"))
        []
    """
    try:
        parts = Path(path).resolve().relative_to(Path(root).resolve()).parts
    except ValueError:
        return []
    return list(parts[:-1])


def plan_download_files(
    config: dict,
    *,
    indicator: str,
    download_dir: Path,
    output_dirs: dict[str, str] | None = None,
    repeat_dates: list[date] | None = None,
    output_repeat_dates: dict[str, list[date]] | None = None,
    target_date: date | None = None,
    query_month: str | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """列出这次执行预期会下载的文件，并带上存储目录。

    日期循环里的导出按每一天展开；循环外的导出只出现一次。目录分段对齐任务的存储结构。

    Args:
        config: 工作流配置，读取其中的 ``steps``。
        indicator: 指标 id。
        download_dir: 没有单独目录时的落盘目录。
        output_dirs: 步骤位置到目录的映射，键如 ``s0``、``s3.1``。
        repeat_dates: 任务指定的循环日期。为空时用步骤上的窗口。
        output_repeat_dates: 单个导出另配的日期，键是步骤位置。
        target_date: 非循环导出使用的日期。
        query_month: 按月指标的月份，格式 ``YYYY-MM``。
        today: 展开相对日期的锚点。默认今天。

    Returns:
        list[dict]: 每项含 indicator、output_key、date、label、name、folders。

    Examples:
        >>> files = plan_download_files(
        ...     {"steps": [{"action": "export", "save_as": "用电量_{date}.xlsx", "name": "用电量"}]},
        ...     indicator="power",
        ...     download_dir=Path("/data"),
        ...     output_dirs={"s0": "/data/wifi"},
        ...     target_date=date(2026, 11, 2),
        ...     today=date(2026, 11, 2),
        ... )
        >>> files[0]["name"]
        '用电量_2026-11-02.xlsx'
        >>> files[0]["folders"]
        ['wifi']
        >>> files[0]["output_key"]
        's0'
    """
    anchor = today or date.today()
    dirs = {str(key): str(path) for key, path in (output_dirs or {}).items() if str(path).strip()}
    root = download_tree_root(Path(download_dir), dirs)
    steps = config.get("steps") if isinstance(config, dict) else None
    if not isinstance(steps, list):
        return []
    own_all = dict(output_repeat_dates or {})

    def one(step: dict, position: str, current: date | None, date_text: str) -> dict[str, Any]:
        """组装一个预期文件。"""
        filename = render_save_as(str(step.get("save_as") or ""), current)
        label = str(step.get("name") or "").strip() or filename or "下载文件"
        filename = filename or label
        directory = Path(dirs.get(position) or download_dir)
        return {
            "indicator": indicator,
            "output_key": position,
            "date": date_text,
            "label": label,
            "name": filename,
            "folders": relative_folders(directory / filename, root),
        }

    found: list[dict[str, Any]] = []

    def walk(items: list, prefix: str) -> None:
        """遍历步骤，展开循环里的每一天。"""
        for index, step in enumerate(items):
            if not isinstance(step, dict):
                continue
            position = f"{prefix}.{index}" if prefix else f"s{index}"
            action = str(step.get("action") or "")
            if action == "repeat":
                nested = [item for item in (step.get("steps") or []) if isinstance(item, dict)]
                dates = resolve_repeat_dates(step, config, repeat_dates, anchor)
                local_own = dict(own_all)
                if repeat_dates is None:
                    local_own = {**step_output_dates(nested, position, anchor), **local_own}
                rounds = plan_repeat_rounds(nested, position, dates, local_own)
                for current, indexes in rounds:
                    for nested_index in indexes:
                        nested_step = nested[nested_index]
                        nested_action = str(nested_step.get("action") or "")
                        nested_position = f"{position}.{nested_index}"
                        if nested_action == "repeat":
                            walk(nested_step.get("steps") or [], nested_position)
                        elif nested_action in OUTPUT_ACTIONS:
                            found.append(one(nested_step, nested_position, current, current.isoformat()))
                continue
            if action in ("download_notice_reports", "download_reports") and repeat_dates:
                for current in repeat_dates:
                    found.append(one(step, position, current, current.isoformat()))
                continue
            if action not in OUTPUT_ACTIONS:
                continue
            if query_month and not prefix:
                try:
                    current = date.fromisoformat(f"{query_month}-01")
                except ValueError:
                    current = target_date
                found.append(one(step, position, current, str(query_month)))
                continue
            current = target_date or anchor
            found.append(one(step, position, current, current.isoformat()))

    walk(steps, "")
    return found


# ============================================================
# 步骤类型
# ============================================================

class WorkflowExecutionError(RuntimeError):
    """工作流执行失败。

    用于把“页面错误弹窗/用户中止/步骤异常”统一传播给上层状态机。
    """


class WorkflowPaused(WorkflowExecutionError):
    """已进入暂停。额度耗尽、重复失败或没有安全恢复路径时使用。

    调用方不得把它收成 step_error 后继续点击或导出。
    """


class WorkflowSkipRepeatRound(WorkflowExecutionError):
    """当前 repeat 日期没有数据，跳过本轮并继续下一轮。"""


class WorkflowDateExportFailed(WorkflowExecutionError):
    """单个日期的导出没有拿到文件。

    日期循环捕获它之后继续后面的日期，并在循环结束时汇总失败原因。
    按钮缺失、文档签名被网关拒绝这类整页问题不使用这个异常。
    """


# 业务页用弹窗说明「这一天没有数」时，不应把整次补数判死。
_EXPORT_EMPTY_MARKERS = (
    "暂无数据",
    "无数据",
    "没有数据",
    "未查询到",
    "没有查询到",
    "尚未发布",
    "暂未发布",
    "未发布",
    "暂无记录",
    "无记录",
)

_EXPORT_NOTICE_SELECTORS = (
    ".el-message-box__message",
    ".el-message-box__content",
    ".el-message--error .el-message__content",
    ".el-message--warning .el-message__content",
    ".el-notification__content",
    ".ant-modal-confirm-content",
    ".ant-message-error",
    ".ant-message-warning",
    ".ant-notification-notice-description",
)


def diagnose_export_miss(
    errors: list[dict],
    dialog_text: str = "",
    *,
    timeout_error: str = "",
) -> dict[str, str] | None:
    """判断这次导出为什么没有等到下载。

    还没有任何弹窗、失败请求，并且下载仍在等待时返回 None。
    页面已经说明该日没有数据时，``action`` 为 ``skip``，日期循环进入下一天。
    网关空 HTML 400 时 ``action`` 为 ``gateway``，整次运行应暂停，不要在这份文档上继续点。
    其他请求失败或超时 ``action`` 为 ``fail``：这一天失败，其余日期仍可继续。

    Args:
        errors: 点击导出之后新记下的页面请求错误。
        dialog_text: 当前可见的提示框文字。
        timeout_error: 下载等待已经结束时的原始异常文本。还在等待时留空。

    Returns:
        dict | None: 含 ``action`` 与 ``reason``。不能定性且下载还在等时为 None。

    Examples:
        >>> diagnose_export_miss([], "") is None
        True
        >>> diagnose_export_miss([], "查询结果暂无数据")["action"]
        'skip'
        >>> diagnose_export_miss([{
        ...     "status": 400, "resource_type": "xhr", "content_type": "text/html",
        ...     "body_size": 6, "body_blank": True, "url": "https://pm.gd.csg.cn/pfjs/export",
        ... }], "")["action"]
        'gateway'
        >>> reason = diagnose_export_miss([{
        ...     "status": 0, "resource_type": "xhr", "failure": "net::ERR_ABORTED",
        ...     "content_type": "", "page_url": "https://pm.gd.csg.cn/page",
        ...     "url": "https://pm.gd.csg.cn/pfjs/export",
        ... }], "", timeout_error="Timeout 60000ms exceeded")["reason"]
        >>> "HTTP 0" in reason and "net::ERR_ABORTED" in reason
        True
    """
    text = re.sub(r"\s+", " ", str(dialog_text or "")).strip()
    if text and any(marker in text for marker in _EXPORT_EMPTY_MARKERS):
        return {"action": "skip", "reason": f"该日期没有可导出的数据：{text[:300]}"}

    described = ""
    if errors:
        described = "；".join(WorkflowRunner._describe_page_errors(errors))[:500]
    gateway = [item for item in errors if WorkflowRunner._is_gateway_form(item)]
    if gateway:
        return {
            "action": "gateway",
            "reason": (
                "导出被网关拒绝（HTTP 400 空 HTML，当前文档的请求签名已失效）。"
                f"{described}。"
                "不要刷新当前文档，请取消本次运行，回到门户重新进入业务页后再导出。"
            ),
        }
    if text:
        return {"action": "fail", "reason": f"导出没有产生文件。页面提示：{text[:300]}"}
    if errors and not timeout_error:
        # 下载还在等时，心跳或被取消的无关请求也会是 HTTP 0。
        # 只有导出地址自己失败才提前结束；其余等到下载超时再写进原因。
        kept = []
        for item in errors:
            try:
                status = int(item.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            if status != 0 or _looks_like_export_url(str(item.get("url") or "")):
                kept.append(item)
        errors = kept
        if not errors:
            return None
    if errors:
        statuses = []
        for item in errors:
            try:
                statuses.append(int(item.get("status") or 0))
            except (TypeError, ValueError):
                statuses.append(0)
        if statuses and all(status == 0 for status in statuses):
            reason = (
                "导出请求没有收到 HTTP 响应（HTTP 0），浏览器没有产生下载。"
                f"{described}。"
                "若同一页面的业务接口同时返回 HTTP 400 空 HTML，这是文档签名失效，"
                "需要重新进入业务页；只有这一天失败时会继续补其余日期。"
            )
        else:
            reason = f"导出请求失败，没有收到文件。{described}。"
        return {"action": "fail", "reason": reason}
    if timeout_error:
        return {
            "action": "fail",
            "reason": (
                "导出失败: 等待下载超时，没有捕获到文件、页面提示或失败请求。"
                f"原始错误：{timeout_error[:300]}"
            ),
        }
    return None


def _looks_like_export_url(url: str) -> bool:
    """这个地址像不像本次导出请求。

    Args:
        url: 请求地址，查询串不参与判断。

    Returns:
        bool: 路径里带有导出或下载字样时为 True。

    Examples:
        >>> _looks_like_export_url("https://pm.gd.csg.cn/pfjs/exportItems")
        True
        >>> _looks_like_export_url("https://pm.gd.csg.cn/pfjs/getSwitch")
        False
    """
    path = url.split("?", 1)[0].lower()
    return any(token in path for token in ("export", "download", "downfile"))


class _PointerUnavailableError(RuntimeError):
    """旧输入路径的鼠标轨迹无法完成；默认输入路径遇到此错误会停止。"""


#: 周明细不再轮询日期面板。翻周只做坐标点击，事件从 findTradeCalendar 响应读取。
#: 月视图在没有捕获到该接口时，仍用下面的脚本读一次网格。

#: FullCalendar 交易日历读取脚本。月/周/日视图通用：日期取 ``[data-date]``，
#: 事件取 ``.fc-event``，状态取自定义样式类（tradeBg / NotBg / greyBg）。
_CALENDAR_EXTRACT_JS = r"""
(arg) => {
  const selector = typeof arg === 'string' ? arg : (arg && arg.selector);
  const includeOther = !!(arg && typeof arg === 'object' && arg.includeOther);
  const root = (selector ? document.querySelector(selector) : null)
    || document.querySelector('.calendar-box')
    || document;
  const view = root.querySelector('.fc-timeGridDay-view') ? '日'
    : root.querySelector('.fc-dayGridDay-view') ? '日'
    : root.querySelector('.fc-timeGridWeek-view') ? '周'
    : root.querySelector('.fc-dayGridWeek-view') ? '周'
    : root.querySelector('.fc-dayGridMonth-view') ? '月'
    : '';
  let month = '';
  const picker = root.querySelector('.date-box input')
    || root.querySelector('.el-date-editor input');
  const collectMonth = (value) => {
    const matched = String(value || '').match(/(\d{4})\D+(\d{1,2})/);
    if (matched) return matched[1] + '-' + String(Number(matched[2])).padStart(2, '0');
    return '';
  };
  if (picker && picker.value) month = collectMonth(picker.value);
  if (!month) {
    for (const input of document.querySelectorAll('.calendar-box .date-box input')) {
      month = collectMonth(input.value);
      if (month) break;
    }
  }

  const cells = Array.from(root.querySelectorAll('[data-date]')).filter((el) => (
    el.classList
    && (el.classList.contains('fc-daygrid-day') || el.classList.contains('fc-timegrid-col'))
  ));
  const rows = [];
  for (const cell of cells) {
    if (!includeOther && cell.classList.contains('fc-day-other')) continue;
    const day = cell.getAttribute('data-date') || '';
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;

    const events = Array.from(cell.querySelectorAll('.fc-event'));
    if (!events.length) {
      rows.push({ date: day, event: '', time_range: '', apply_time: '', event_type: '', status: '' });
      continue;
    }
    for (const ev of events) {
      let status = '';
      const weekEl = ev.querySelector('.calendar-week');
      if (weekEl) {
        // 周视图：标题、标的时间、申报时间、正式/灾备信息都在事件卡片里。
        const titleEl = weekEl.querySelector('.titleOmit') || weekEl.querySelector('.item');
        const title = titleEl ? (titleEl.textContent || '').replace(/\s+/g, ' ').trim() : '';
        const typeEl = weekEl.querySelector('.typeSizeFormal');
        const eventType = typeEl ? (typeEl.textContent || '').trim() : '';
        let delivery = '';
        let applyTime = '';
        for (const line of weekEl.querySelectorAll('.list-text')) {
          const text = (line.textContent || '').replace(/\s+/g, ' ').trim();
          if (text.indexOf('标的时间') >= 0) {
            delivery = text.replace(/^.*?[:：]\s*/, '');
          } else if (text.indexOf('申报时间') >= 0) {
            applyTime = text.replace(/^.*?[:：]\s*/, '');
          }
        }
        const statusEl = weekEl.querySelector('.legend-icon');
        const cls = statusEl ? String(statusEl.className) : String(ev.className || '');
        if (/(^|\s)tradeBg(\s|$)/.test(cls)) status = '交易中';
        else if (/(^|\s)NotBg(\s|$)/.test(cls)) status = '未开始';
        else if (/(^|\s)(greyBg|grayBg)(\s|$)/.test(cls)) status = '已结束';
        rows.push({
          date: day, event: title, time_range: delivery,
          apply_time: applyTime, event_type: eventType, status: status,
        });
        continue;
      }

      // 月视图：事件只有标题和状态。
      const main = ev.querySelector('.fc-event-main') || ev;
      const timeEl = ev.querySelector('.fc-event-time');
      const timeText = timeEl ? (timeEl.textContent || '').trim().replace(/\s+/g, ' ') : '';
      let title = (main.textContent || '').trim().replace(/\s+/g, ' ');
      if (timeText && title.indexOf(timeText) === 0) title = title.slice(timeText.length).trim();
      const statusEl = ev.querySelector(
        '[class*="calendar-month"], [class*="calendar-week"], [class*="calendar-day"]'
      );
      const cls = statusEl ? String(statusEl.className) : String(ev.className || '');
      if (/(^|\s)tradeBg(\s|$)/.test(cls)) status = '交易中';
      else if (/(^|\s)NotBg(\s|$)/.test(cls)) status = '未开始';
      else if (/(^|\s)(greyBg|grayBg)(\s|$)/.test(cls)) status = '已结束';
      rows.push({
        date: day, event: title, time_range: timeText,
        apply_time: '', event_type: '', status: status,
      });
    }
  }

  if (!month && rows.length) {
    const counts = {};
    rows.forEach((row) => {
      const key = row.date.slice(0, 7);
      counts[key] = (counts[key] || 0) + 1;
    });
    month = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0] || '';
  }
  return { view: view || '月', month: month, rows: rows };
}
"""


_CAPTCHA_REFRESH_MIN_MS = 800
_CAPTCHA_REFRESH_MAX_MS = 1500


def login_wait_ms(step: dict, config: dict | None = None) -> int:
    """登录提交后等待成功信号的毫秒数。

    步骤或工作流没写 ``login_wait_ms``、写空或小于 1 秒时，用 30 秒。
    成功信号是登录接口成功、新开的工作台页，或当前页离开登录页，谁先到算谁。

    Args:
        step: 点击「立即登录」的步骤。
        config: 工作流配置。步骤没写时再看这里。

    Returns:
        int: 等待毫秒数。

    Examples:
        >>> login_wait_ms({})
        30000
        >>> login_wait_ms({"login_wait_ms": 12000})
        12000
        >>> login_wait_ms({}, {"login_wait_ms": 20000})
        20000
    """
    raw = step.get("login_wait_ms")
    if raw in (None, ""):
        raw = (config or {}).get("login_wait_ms", 30000)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return 30000
    if parsed < 1000:
        return 30000
    return parsed


def interpret_login_submit(status: int, content_type: str, text: str) -> dict[str, str] | None:
    """判断 ``/sso-auth/login`` 的 JSON 是成功还是业务失败。

    现场成功响应是 HTTP 200、``application/json``、``{"code":0,...}``。
    ``code`` 不是 0 或 200 时视为登录失败，并带上 ``message`` / ``msg``。
    没有 ``code`` 或不是 JSON 时不猜，交给页签信号。

    Args:
        status: HTTP 状态码。
        content_type: 响应类型。
        text: 响应正文，只用来读 code 和提示，不保留令牌。

    Returns:
        dict | None: ``outcome`` 为 ``success`` 或 ``failed``。无法判断时为 None。

    Examples:
        >>> interpret_login_submit(200, "application/json", '{"code":0}')
        {'outcome': 'success', 'message': ''}
        >>> interpret_login_submit(200, "application/json", '{"code":1,"message":"验证码错误"}')
        {'outcome': 'failed', 'message': '验证码错误'}
        >>> interpret_login_submit(400, "text/html", "\\r\\n") is None
        True
    """
    if "json" not in str(content_type or "").lower():
        return None
    try:
        payload = json.loads(text or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "code" not in payload:
        return None
    code = payload.get("code")
    message = ""
    for key in ("message", "msg", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            message = value.strip()[:300]
            break
    if str(code) in {"0", "200"} and int(status) < 400:
        return {"outcome": "success", "message": message}
    return {"outcome": "failed", "message": message or f"登录接口返回 code={code}"}


def captcha_self_check_rounds(step: dict) -> int:
    """读取验证码自检失败后，刷新两次再识别的轮数。

    没写 ``self_check_rounds``、写空、写了不能转成正整数时，都是 3。
    旧字段 ``retry`` 仍然有效：没有 ``self_check_rounds`` 时，用它作为轮数，
    这样现场已经写好的登录配置不会报错，也不会被静默丢掉。
    每一轮只在判定不合格之后进行，合格就不再刷新。

    Args:
        step (dict): captcha 步骤。

    Returns:
        int: 至少为 1 的轮数。缺省为 3。

    Examples:
        >>> captcha_self_check_rounds({})
        3
        >>> captcha_self_check_rounds({"self_check_rounds": 2})
        2
        >>> captcha_self_check_rounds({"self_check_rounds": "nope"})
        3
        >>> captcha_self_check_rounds({"self_check_rounds": 0})
        3
        >>> captcha_self_check_rounds({"retry": 5})
        5
        >>> captcha_self_check_rounds({"retry": 5, "self_check_rounds": 2})
        2
    """
    raw = step.get("self_check_rounds", None)
    if raw is None or raw == "":
        raw = step.get("retry", None)
    if raw is None or raw == "":
        return 3
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return 3
    if parsed < 1:
        return 3
    return parsed


def captcha_refresh_count(step: dict) -> int:
    """读取验证码识别前要主动刷新的次数。

    ``refresh_before`` 与旧字段 ``pre_refresh`` 都认，默认 0（不预先刷新）。
    刷新必须走低痕迹点击，并且两次之间至少间隔 800 毫秒，避免瑞数 400。

    Args:
        step (dict): captcha 步骤。

    Returns:
        int: 非负次数。写了非法数字时按 0。

    Examples:
        >>> captcha_refresh_count({"refresh_before": 2})
        2
        >>> captcha_refresh_count({"pre_refresh": 1})
        1
        >>> captcha_refresh_count({})
        0
    """
    raw = step.get("refresh_before", step.get("pre_refresh", 0))
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def captcha_refresh_delay_s(step: dict, rng: random.Random | None = None) -> float:
    """这次刷新之前要等待的秒数，带抖动，并且不会低于 0.8 秒。

    没写 ``refresh_interval_ms`` / ``pre_refresh_interval_ms`` 时，在 800 到 1500 毫秒之间取。
    写了更小的间隔也会抬到 800 毫秒：点得太密会触发瑞数 HTTP 400。

    Args:
        step (dict): captcha 步骤。
        rng (random.Random | None): 随机源。空则用全局随机，测试可传入固定种子。

    Returns:
        float: 等待秒数，不小于 0.8。

    Examples:
        >>> captcha_refresh_delay_s({}, random.Random(1)) >= 0.8
        True
        >>> captcha_refresh_delay_s({"refresh_interval_ms": 200}, random.Random(1)) >= 0.8
        True
    """
    picker = rng or random
    raw = step.get("refresh_interval_ms", step.get("pre_refresh_interval_ms"))
    if raw is None:
        delay_ms = picker.uniform(_CAPTCHA_REFRESH_MIN_MS, _CAPTCHA_REFRESH_MAX_MS)
    else:
        try:
            base = float(raw)
        except (TypeError, ValueError):
            base = float(_CAPTCHA_REFRESH_MIN_MS)
        base = max(base, float(_CAPTCHA_REFRESH_MIN_MS))
        delay_ms = base * picker.uniform(0.9, 1.1)
        delay_ms = max(delay_ms, float(_CAPTCHA_REFRESH_MIN_MS))
    return delay_ms / 1000


class WorkflowRunner:
    """解析 YAML 工作流，通过 CDP 操控 Chrome 逐步执行。"""

    def __init__(
        self,
        config: dict,
        download_dir: str = "./downloads",
        headless: bool = False,
        minimized: bool = False,
        target_date: str | date | None = None,
        repeat_dates: list | None = None,
        query_month: str | date | None = None,
        output_dirs: dict[str, str] | None = None,
        output_repeat_dates: dict[str, list] | None = None,
    ):
        self.config = config
        self.name = config.get("name", "unnamed")
        self.steps = config.get("steps", [])
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.output_dirs = {
            str(key): str(path)
            for key, path in (output_dirs or {}).items()
            if str(path).strip()
        }
        self._active_output_dir = ""
        self.headless = headless
        self.minimized = minimized
        self.target_date = self._coerce_target_date(target_date or config.get("target_date"))
        raw_repeat = repeat_dates if repeat_dates is not None else config.get("repeat_dates")
        if raw_repeat is None:
            self.repeat_dates = None
        else:
            from spider_gd.core.dates import normalize_target_dates
            self.repeat_dates = normalize_target_dates(raw_repeat)
        raw_output_dates = (
            output_repeat_dates if output_repeat_dates is not None else config.get("output_repeat_dates")
        )
        from spider_gd.core.dates import normalize_output_dates
        self.output_repeat_dates = normalize_output_dates(raw_output_dates)
        raw_month = query_month if query_month is not None else config.get("query_month")
        if raw_month:
            from spider_gd.core.dates import normalize_target_months
            self.query_month = normalize_target_months([raw_month])[0]
        else:
            self.query_month = None

        # 运行时状态
        self.browser = None
        self.context = None
        self.chrome_proc = None
        self.pages: list = []
        self._current_page_idx = 0
        self._stopped = False
        self._paused = False
        self._shared_session = None
        self._protected_pages: set = set()
        self._failed = False
        self._failure_reason = ""
        self._skip_remaining_login_steps = False
        # 显式登录提交后，后续认证请求的 4xx/5xx 不能再按未登录前的探测请求忽略。
        self._login_submit_seen = False

        # 页面观测。网关 4xx 只允许关掉本次新开的标签再重放，不刷新当前文档。
        self._page_observers: dict[Any, dict[str, Any]] = {}
        self._response_tasks: set[asyncio.Task] = set()
        self._page_errors: list[dict[str, Any]] = []
        # findTradeCalendar 的响应正文。周明细从这里取事件，不再刮日历 DOM。
        self._calendar_payloads: list[dict[str, Any]] = []
        self._calendar_shown_monday: date | None = None
        self._calendar_watch_baseline = 0
        self._calendar_payload_baseline = 0
        self._calendar_week_clicked = False
        self._calendar_ignored_requests: set[int] = set()
        self._calendar_saved_paths: set[str] = set()
        self._console_errors: list[dict[str, Any]] = []
        self._page_exceptions: list[str] = []
        self._auth_traffic: list[dict[str, Any]] = []
        self._login_submit_verdicts: list[dict[str, Any]] = []
        self._login_watch_at = 0.0
        self._login_generation = 0
        self._login_listeners: list[tuple[Any, Any]] = []
        self._login_listen_tasks: set[asyncio.Task] = set()
        self._login_origin_page = None
        self._login_error_baseline = 0
        self._failed_requests: list[str] = []
        # 被网关空 HTML 400 拒绝过的文档。同一份文档继续点只会继续 400，
        # 必须换新文档；执行器在这里停下来，不替用户重试。
        self._poisoned_pages: set = set()
        self._inflight: set = set()
        self._retired_pages: set = set()
        self._tab_checkpoints: dict = {}
        self._executing_step_index: int | None = None
        self._websocket_handlers: list = []
        self._last_network_activity = 0.0
        self._trace_browser_actions = str(config.get("trace_browser_actions", False)).lower() in ("true", "1", "yes")
        # 给 hover 引起的菜单和布局变化留出稳定时间，再复测落点。
        self._pointer_settle_s = 0.45
        # 旧输入模式（原生 locator.click / page.fill / ElementHandle）已彻底移除：
        # 它们会让网关标记文档并返回 400。显式关闭安全输入直接报错，不允许降级。
        for _legacy_key in ("human_input", "low_profile_input"):
            if _legacy_key in config and str(config.get(_legacy_key)).strip().lower() in (
                "false", "0", "off", "no",
            ):
                raise WorkflowExecutionError(
                    f"配置项 {_legacy_key}=false 不再支持：旧输入模式已移除，会触发网关400"
                )
        self._pointer_pos: tuple[float, float] | None = None
        self._action_phase = ""
        self._action_request_phases: dict[Any, dict] = {}
        self._action_trace_file = self.download_dir / "_diagnostics" / f"browser_actions_{time.time_ns()}.jsonl"
        self._recovery_budget: dict[int, int] = {}
        self._recovery_attempts = 0
        self._replay_index: int | None = None
        self._recovery_block_reason = ""
        self._page_recovery_enabled = str(config.get("page_recovery", "auto")).lower() != "off"
        try:
            self._max_recoveries = int(config.get("max_page_recoveries", 3))
        except (TypeError, ValueError):
            self._max_recoveries = 3

        # 回调
        self._on_event: callable = lambda event: None
        self._wait_continue: callable = None  # async () -> bool, 返回 True 继续 / False 跳过

    @property
    def failed(self) -> bool:
        """本次执行是否失败/中止。"""
        return self._failed

    @property
    def failure_reason(self) -> str:
        """失败/中止原因，供 TaskRunner/SSE 最终状态使用。"""
        return self._failure_reason

    def _mark_failed(self, reason: str) -> None:
        self._failed = True
        if not self._failure_reason:
            self._failure_reason = reason

    @staticmethod
    def _site_unreachable_message(exc: BaseException) -> str:
        msg = str(exc)
        if "ERR_CONNECTION_CLOSED" in msg or "ERR_CONNECTION_RESET" in msg:
            hint = "连接被关闭/重置，常见原因是随身 WiFi 或专网链路中断、网关切换、站点主动断开。"
        elif "ERR_INTERNET_DISCONNECTED" in msg or "ERR_ADDRESS_UNREACHABLE" in msg:
            hint = "本机网络不可达，请优先检查随身 WiFi 在线状态、网卡连接和专网路由。"
        elif "ERR_NAME_NOT_RESOLVED" in msg:
            hint = "域名解析失败，请检查 DNS、专网解析和本机 hosts/路由规则。"
        elif "ERR_PROXY_CONNECTION_FAILED" in msg:
            hint = "代理连接失败，请检查代理/VPN/专网代理配置。"
        elif "ERR_TIMED_OUT" in msg or "ERR_CONNECTION_TIMED_OUT" in msg:
            hint = "连接超时，请检查专网路由、随身 WiFi 信号、目标站点是否维护。"
        else:
            hint = "目标站点不可达，请检查随身 WiFi、专网路由、DNS 和 U-Key/证书服务。"
        return f"⚠️ 目标站点不可达：{hint}\n原始错误：{msg}"

    async def _capture_failure_screenshot(self, prefix: str) -> str:
        """保存失败现场截图，失败时返回空字符串，不影响主流程错误传播。"""
        try:
            ts = time.strftime("%Y%m%d_%H%M%S")
            safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix).strip("_") or "failure"
            path = self.download_dir / "_failures" / f"{safe_prefix}_{ts}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(path), full_page=True)
            logger.info("  失败现场截图: {}", path)
            return str(path)
        except Exception as exc:
            logger.debug("  失败截图保存失败: {}", exc)
            return ""

    def _save_as_name(self, save_as: str) -> str:
        """展开 save_as 占位符并只保留文件名。

        save_as 只管文件名，目录由任务存储结构（output_dirs）或 download_dir 决定。
        历史写法 ``/子目录/file.xlsx`` 的目录部分会被忽略并告警。

        占位符:
          {date}         → 2026-06-28 (ISO)
          {date_compact} → 20260628
          {year}         → 2026
          {month}        → 202606
          {month_iso}    → 2026-06
          {previous_month} → 上月，例如 202605
          {previous_month_iso} → 上月，例如 2026-05
        """
        today = self.target_date or date.today()
        _month_start, month_end = self._settlement_month_bounds()
        rendered = str(save_as or "").strip().format(
            date=today.isoformat(),
            date_compact=today.strftime("%Y%m%d"),
            year=today.strftime("%Y"),
            month=today.strftime("%Y%m"),
            month_iso=today.strftime("%Y-%m"),
            previous_month=month_end.strftime("%Y%m"),
            previous_month_iso=month_end.strftime("%Y-%m"),
        )
        name = save_as_filename(rendered)
        if name != rendered:
            logger.warning(
                "  save_as 含目录，已忽略：{}；目录请在任务存储结构中配置", save_as,
            )
        return name

    def _output_base(self) -> Path:
        return Path(self._active_output_dir) if self._active_output_dir else self.download_dir

    def _resolve_save_path(self, save_as: str, default_name: str = "output.xlsx") -> Path:
        """save_as → 当前输出目录下的文件路径。"""
        name = self._save_as_name(save_as) or self._save_as_name(default_name)
        resolved = self._output_base() / name
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _export_path(self, save_as: str, original_name: str) -> Path:
        """浏览器导出：文件名主体用 save_as，扩展名以站点实际下载为准。

        没写 save_as 时保留站点原名。
        """
        original = save_as_filename(original_name)
        name = self._save_as_name(save_as)
        if not name:
            return self._output_base() / (original or "download")
        site_suffix = Path(original).suffix
        if site_suffix and Path(name).suffix.lower() != site_suffix.lower():
            logger.warning(
                "  save_as 扩展名与站点下载不一致，按站点保存：{} → {}",
                name, original,
            )
            name = Path(name).stem + site_suffix
        return self._output_base() / name

    # ----- 属性 -----

    @property
    def page(self):
        """当前活跃 page。如果当前 page 已关闭则回退到任意存活的 page。"""
        if not self.pages:
            return None
        if self._shared_session is not None:
            if self._current_page_idx >= len(self.pages) or self.pages[self._current_page_idx].is_closed():
                raise WorkflowExecutionError("已绑定的业务页面被关闭，禁止切换到其他页面继续")
            if not self.browser.is_connected():
                raise WorkflowExecutionError("浏览器连接已断开")
        # 确保 _current_page_idx 不越界
        if self._current_page_idx >= len(self.pages):
            self._current_page_idx = len(self.pages) - 1
        page = self.pages[self._current_page_idx]
        # 检查 page 是否已关闭
        try:
            if page.is_closed():
                # 回退到第一个未关闭的 page
                for i, p in enumerate(self.pages):
                    try:
                        if not p.is_closed():
                            self._current_page_idx = i
                            logger.info("  当前 page 已关闭，回退到标签页 #{}", i)
                            return p
                    except Exception:
                        pass
                return None
        except Exception:
            pass
        return page

    # ----- 主入口 -----

    async def run(self, on_event=None, wait_continue=None, *,
                  browser=None, context=None, pages=None, session=None) -> None:
        """启动浏览器，执行所有步骤。

        on_event: 进度回调，接收 dict，如 {"event":"step_start","step":1,"total":6,...}
        wait_continue: 异步回调，返回 True/False，用于人工介入时等待

        **Shared browser 模式**（传入 browser/context/pages）：
        - 复用已有浏览器，不创建/关闭
        - 适合 TaskRunner 多指标共用同一浏览器
        """
        if browser is not None and context is not None:
            # -- Shared browser 模式 --
            self.browser = browser
            self.context = context
            self.chrome_proc = None  # 不负责关闭进程
            self.pages = list(pages) if pages else []
            self._current_page_idx = len(self.pages) - 1 if self.pages else 0
            await self._execute_steps(on_event, wait_continue, shared=True)
        else:
            # -- Standalone 模式（原有逻辑） --
            from spider_gd.core.crawler.shared_browser import get_shared_browser
            session = session or get_shared_browser()
            async with session.execution(self.config.get("_control_name", self.name), on_event) as emit:
                session.inject_into(self)
                session._active_runner = self
                self.context.on("page", self._on_new_page)
                try:
                    self._on_event = emit
                    self._wait_continue = session.wait_continue
                    if self.config.get("needs_login", True):
                        if self._explicit_login_run():
                            # 显式登录不因 manual/reuse_only 先停住。未登录就执行登录步骤。
                            auth = await session.check_auth()
                            if auth["state"] == "authenticated":
                                self._skip_remaining_login_steps = True
                            elif auth["state"] != "unauthenticated":
                                await session.require_auth(allow_auto=False)
                                self._skip_remaining_login_steps = True
                        else:
                            # 指标只复用已登录会话。未登录暂停，不在这里自动登录。
                            await session.require_auth(allow_auto=False)
                    await self._execute_steps(emit, session.wait_continue, shared=True)
                finally:
                    self.context.remove_listener("page", self._on_new_page)

    async def _execute_steps(self, on_event, wait_continue, *, shared: bool = False) -> None:
        """执行所有步骤（shared 模式下不关闭浏览器）。"""
        if on_event:
            self._on_event = on_event
        if wait_continue:
            self._wait_continue = wait_continue

        self._emit_download_plan()
        if not shared:
            self._emit("browser_start")
        try:
            for page in self.pages:
                self._observe_page(page)
            i = 0
            while i < len(self.steps):
                step = self.steps[i]
                self._active_output_dir = self.output_dirs.get(f"s{i}", "")
                if self._stopped:
                    self._mark_failed("用户中止")
                    self._emit("aborted", message="用户中止")
                    break
                summary = self._step_summary(step)
                self._emit("step_start", step=i + 1, total=len(self.steps),
                           action=step.get("action", ""), summary=summary)
                try:
                    if self._should_skip_login_step(step):
                        logger.info("  已在工作台，跳过登录步骤: {}", step.get("action", ""))
                        self._emit("step_done", step=i + 1)
                        i += 1
                        continue
                    if (self._shared_session and self.config.get("needs_login", True)
                            and i > 0 and self.steps[i - 1].get("_login_step")
                            and not step.get("_login_step")):
                        await self._shared_session.require_auth(
                            page=self.page,
                        )
                    self._observe_page(self.page)
                    if await self._maybe_recover(i, stage="before"):
                        i = self._replay_index
                        continue
                    self._raise_if_recovery_blocked()
                    self._executing_step_index = i
                    await self._exec_step(step)
                    if await self._maybe_recover(i, stage="after"):
                        i = self._replay_index
                        continue
                    self._raise_if_recovery_blocked()
                    if self._stopped:
                        self._mark_failed("用户中止")
                        self._emit("aborted", message="用户中止")
                        break
                    self._emit("step_done", step=i + 1)
                    i += 1
                except WorkflowPaused as exc:
                    self._mark_failed(str(exc))
                    logger.warning("步骤 {}/{} 已暂停: {}", i + 1, len(self.steps), exc)
                    break
                except Exception as exc:
                    # 请求错误可能在等待期间才到达；先交给暂停/恢复机制，
                    # 避免把网关拒绝误报成普通的元素超时。
                    if step.get("action") == "wait":
                        await self._drain_response_captures()
                        if (self._unacknowledged_page_errors()
                                and await self._maybe_recover(i, stage="after")):
                            i = self._replay_index
                            continue
                    self._mark_failed(str(exc))
                    if is_playwright_goto_error(exc):
                        self._emit("site_unreachable",
                                   step=i + 1, error=str(exc),
                                   message=self._site_unreachable_message(exc),
                                   diagnosis=self._site_unreachable_message(exc))
                    elif self._unacknowledged_page_errors():
                        rejected = "；".join(
                            self._describe_page_errors(self._unacknowledged_page_errors())
                        )[:500]
                        self._emit(
                            "gateway_rejected",
                            step=i + 1,
                            error=rejected,
                            message="已记录 HTTP 错误。不会把它当成成功后继续",
                        )
                    self._emit("step_error", step=i + 1, error=str(exc))
                    if str(step.get("action") or "") in OUTPUT_ACTIONS:
                        self._note_file("failed", reason=str(exc))
                    logger.error("步骤 {}/{} 失败: {}", i + 1, len(self.steps), exc)
                    await self._dump_page_diagnostics(i + 1)
                    self._emit("aborted", message=f"步骤 {i + 1} 失败: {exc}")
                    break
            if (self._shared_session and self.config.get("type") == "login"
                    and not self.config.get("_partial_run") and not self._failed and not self._stopped):
                await self._shared_session.require_auth(
                    page=self.page,
                )
        except WorkflowPaused as exc:
            self._mark_failed(str(exc))
            logger.warning("工作流已暂停: {}", exc)
        except Exception as exc:
            self._mark_failed(str(exc))
            self._emit("workflow_error", error=str(exc))
            logger.error("工作流异常: {}", exc)
        finally:
            self._release_page_observers()
            pending = list(self._response_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._response_tasks.clear()
            if not self._failed and not self._stopped:
                self._emit("workflow_done", name=self.name)

    def _emit(self, event: str, **kwargs):
        data = {"event": event, **kwargs}
        try:
            self._on_event(data)
        except Exception:
            pass

    async def _abort_login_retry(self, reason: str) -> None:
        await self._pause_login_error(reason)

    def _explicit_login_run(self) -> bool:
        """用户直接跑登录工作流，或任务把登录步骤标成这次要执行的登录。"""
        return self.config.get("type") == "login" or any(
            isinstance(step, dict) and step.get("_login_step") for step in self.steps
        )

    def _should_skip_login_step(self, step: dict) -> bool:
        return (
            self._skip_remaining_login_steps
            and step.get("action") != "ensure_login"
            and (self.config.get("type") == "login" or step.get("_login_step"))
        )

    async def _is_logged_in_dashboard(self) -> bool:
        """判断当前页是否已进入工作台，避免已登录时继续点击登录按钮。"""
        if not self.page:
            return False
        if is_public_portal_url(self._page_url(self.page)):
            return False
        for selector in (
            GD_WORKBENCH_USER,
            ".ant-menu-horizontal",
            ".el-menu",
            ".el-submenu",
            "text=现货交易",
            "text=工作台",
        ):
            try:
                success = self.page.locator(selector).first
                if await success.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _account_marker_visible(self) -> bool:
        """页面上是否已经有登录后才出现的账号标记。

        门户 ``#/home`` 在已登录时往往不再显示「登 录」，但地址仍是公开首页，
        不能靠菜单判断。用户名或「退出登录」才算已经进了会话。

        Returns:
            bool: 看到账号标记时为 True。

        Examples:
            >>> "退出登录" in "a:has-text('退出登录')"
            True
        """
        for selector in (
            GD_WORKBENCH_USER,
            "text=退出登录",
            "a:has-text('退出登录')",
        ):
            try:
                if await self._dom_visible(selector, timeout=300):
                    return True
            except Exception:
                continue
        return False

    async def exec_step(self, step: dict) -> None:
        """执行单个步骤并发出 start/done 事件。公开方法，供外部单步播放使用。"""
        await self._exec_step(step)

    # ----- 新标签页回调 -----

    async def _on_new_page(self, new_page) -> None:
        if self._shared_session is not None:
            opener = await new_page.opener()
            if opener not in self.pages:
                return
        if new_page in self.pages:
            return
        logger.info("  检测到新标签页 #{}", len(self.pages))
        self.pages.append(new_page)
        self._current_page_idx = len(self.pages) - 1
        self._observe_page(new_page)
        # 新标签若没被选中，标签栏会空着，后台页的签名也容易被拒。
        await self._bring_page_to_front(new_page)

    # ----- 页面观测：网关拒绝 / 控制台异常 / 网络静默 -----

    _STATIC_SUFFIXES = (
        ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
        ".woff", ".woff2", ".ttf", ".map",
    )

    @staticmethod
    def _trace_url(url: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment.split("?", 1)[0]))

    def _write_action_trace(self, **entry) -> None:
        if not self._trace_browser_actions:
            return
        from datetime import datetime

        entry = {"at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                 "step": (self._executing_step_index or 0) + 1, **entry}
        try:
            self._action_trace_file.parent.mkdir(parents=True, exist_ok=True)
            first_write = not self._action_trace_file.exists()
            with self._action_trace_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if first_write:
                logger.info("  浏览器动作诊断文件: {}", self._action_trace_file)
            logger.info("  [browser-action] {}", json.dumps(entry, ensure_ascii=False))
        except OSError as exc:
            logger.warning("  浏览器动作诊断保存失败: {}", exc)

    async def _trace_browser_action(self, phase: str) -> None:
        """可选诊断：仅读取 Context 的 cookie 指纹，不执行页面脚本或改写凭据。"""
        if not self._trace_browser_actions:
            return
        import hashlib

        self._action_phase = phase
        try:
            page = self.page
            context = page.context
            cookies = await asyncio.wait_for(context.cookies([self._page_url(page)]), timeout=1)
            fingerprints = [{"name": item["name"], "domain": item["domain"], "path": item["path"],
                             "sha256": hashlib.sha256(item["value"].encode()).hexdigest()[:12]}
                            for item in cookies]
            self._write_action_trace(kind="action", phase=phase,
                page=self._trace_url(self._page_url(page)), context=str(id(context)),
                bound_page=self._trace_url(self._page_url(self._shared_session.page)) if self._shared_session else "",
                cookies=fingerprints)
        except Exception as exc:
            self._write_action_trace(kind="action", phase=phase, unavailable=type(exc).__name__)

    def _observe_page(self, page) -> None:
        """给页面挂只读观测器：不改写请求，只记录 4xx/5xx、控制台错误与在途请求。"""
        if page is None:
            return
        try:
            if page in self._page_observers:
                return
            if page.is_closed():
                return
            handlers = {
                "request": self._on_page_request,
                "requestfinished": self._on_page_request_done,
                "requestfailed": self._on_page_request_failed,
                "response": self._on_page_response,
                "console": self._on_page_console,
                "pageerror": self._on_page_pageerror,
                "websocket": self._on_page_websocket,
            }
            for name, handler in handlers.items():
                page.on(name, handler)
            self._page_observers[page] = handlers
        except Exception as exc:  # 观测失败不能影响主流程
            logger.debug("  页面观测器挂载失败: {}", exc)

    def _release_page_observers(self) -> None:
        """工作流终态卸下观测器，避免已结束的 runner 继续记下后续请求。"""
        for page, handlers in list(self._page_observers.items()):
            for name, handler in handlers.items():
                with contextlib.suppress(Exception):
                    page.remove_listener(name, handler)
        self._page_observers.clear()
        self._action_request_phases.clear()
        for websocket, handler in self._websocket_handlers:
            with contextlib.suppress(Exception):
                websocket.remove_listener("close", handler)
        self._websocket_handlers.clear()

    def _on_page_request(self, request) -> None:
        try:
            if getattr(request, "resource_type", "") in ("websocket", "eventsource", "preflight"):
                return
            self._inflight.add(request)
            if self._trace_browser_actions:
                self._action_request_phases[request] = {"phase": self._action_phase, "started": time.time()}
                if len(self._action_request_phases) > 100:
                    self._action_request_phases.pop(next(iter(self._action_request_phases)))
            self._last_network_activity = time.monotonic()
        except Exception:
            pass

    def _on_page_request_done(self, request) -> None:
        try:
            self._inflight.discard(request)
            self._last_network_activity = time.monotonic()
        except Exception:
            pass

    def _on_page_request_failed(self, request) -> None:
        self._on_page_request_done(request)
        try:
            failure = getattr(request, "failure", None) or ""
            url = getattr(request, "url", "")[:140]
            self._failed_requests.append(
                f"{time.strftime('%H:%M:%S')} {request.method} {url} FAILED {failure}"
            )
            del self._failed_requests[:-40]
            page = request.frame.page if request.frame else None
            if (page not in self._retired_pages and getattr(request, "resource_type", "") in ("xhr", "fetch")
                    and self._same_origin(request.url, self._page_url(page))):
                self._page_errors.append({"status": 0, "url": request.url,
                    "resource_type": request.resource_type, "page_url": self._page_url(page),
                    "_page": page, "at": time.strftime("%H:%M:%S"), "failure": failure})
        except Exception:
            pass

    def _on_page_websocket(self, websocket) -> None:
        """记录本地证书服务这类 WebSocket 的连接与断开（登录失败排查关键线索）。"""
        try:
            url = websocket.url
            self._failed_requests.append(f"{time.strftime('%H:%M:%S')} WS open {url}")
            del self._failed_requests[:-40]
            def on_close(_ws=None):
                self._failed_requests.append(f"{time.strftime('%H:%M:%S')} WS closed {url}")
                del self._failed_requests[:-40]
            websocket.on("close", on_close)
            self._websocket_handlers.append((websocket, on_close))
        except Exception:
            pass

    @staticmethod
    def _same_origin(left: str, right: str) -> bool:
        from urllib.parse import urlsplit

        try:
            return bool(left) and bool(right) and urlsplit(left).netloc == urlsplit(right).netloc
        except Exception:
            return False

    _GATEWAY_BODY_MAX = 64

    async def _remember_calendar_payload(self, response, request, status: int, url: str, content_type: str) -> None:
        """记下 findTradeCalendar 的响应正文，供周明细直接使用。

        这是网络监听，不在页面里执行脚本。正文只留这一条接口，并且截断。

        Args:
            response: Playwright 响应。
            request: 对应请求，用来找所属页面。
            status: HTTP 状态码。
            url: 请求地址。
            content_type: 响应类型。

        Returns:
            None: 结果追加到 ``_calendar_payloads``。

        Examples:
            >>> "findtradecalendar" in "https://x/findTradeCalendar".lower()
            True
        """
        page = request.frame.page if getattr(request, "frame", None) is not None else None
        text = ""
        try:
            body = await asyncio.wait_for(response.body(), timeout=2)
            text = body[:1_000_000].decode("utf-8", "replace")
        except Exception:
            text = ""
        self._calendar_payloads.append({
            "status": status,
            "url": url,
            "text": text,
            "resource_type": getattr(request, "resource_type", "") or "xhr",
            "content_type": content_type,
            "page_url": self._page_url(page),
            "_page": page,
            "at": time.strftime("%H:%M:%S"),
            # 翻周点击之前就已经在飞的请求，不算这一周的新结果。
            "ignored": id(request) in self._calendar_ignored_requests,
        })
        del self._calendar_payloads[:-30]

    def _on_page_response(self, response) -> None:
        """同步接入点。正文长度在任务里读完再分类，避免挡住事件循环。"""
        try:
            task = asyncio.get_running_loop().create_task(self._capture_page_response(response))
        except RuntimeError:
            return
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _drain_response_captures(self) -> None:
        pending = [task for task in self._response_tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=2)

    async def _capture_page_response(self, response) -> None:
        """先登记错误，再读取小响应形态；正文分类期间同样禁止放行。"""
        try:
            request = response.request
            resource_type = getattr(request, "resource_type", "")
            if resource_type not in ("xhr", "fetch", "document"):
                return
            status, url = int(response.status), response.url
            headers = response.headers or {}
            content_type = headers.get("content-type", "") or ""
            if self._trace_browser_actions:
                origin_page = request.frame.page if request.frame is not None else None
                if self._same_origin(url, self._page_url(origin_page)):
                    self._write_action_trace(kind="response", **self._action_request_phases.pop(request, {}),
                        method=request.method, url=self._trace_url(url), status=status,
                        content_type=content_type, page=self._trace_url(self._page_url(origin_page)),
                        context=str(id(origin_page.context)))
            self._record_auth_traffic(request, response, status, url, content_type, None)
            if "findtradecalendar" in url.split("?", 1)[0].lower():
                await self._remember_calendar_payload(response, request, status, url, content_type)
            if status < 400 or url.split("?", 1)[0].lower().endswith(self._STATIC_SUFFIXES):
                return
            page = request.frame.page if request.frame is not None else None
            page_url = self._page_url(page)
            if page in self._retired_pages or not self._same_origin(url, page_url):
                return
            entry = {
                "status": status, "url": url, "resource_type": resource_type,
                "content_type": content_type, "body_size": None, "body_blank": False,
                "page_url": page_url, "_page": page, "at": time.strftime("%H:%M:%S"),
            }
            self._page_errors.append(entry)
            # 记录出错时页面是否在前台：后台/遮挡会节流页面的签名心跳。
            if page is not None:
                with contextlib.suppress(Exception):
                    entry["page_state"] = str(await asyncio.wait_for(
                        page.evaluate(
                            "document.visibilityState + '/' + (document.hasFocus() ? 'focus' : 'blur')"
                        ),
                        timeout=0.8,
                    ) or "")
            # 大正文不参与短空白响应识别；不记录响应正文及凭据。
            length = str(headers.get("content-length", ""))
            if length.isdigit() and int(length) > self._GATEWAY_BODY_MAX:
                entry["body_size"] = int(length)
            else:
                try:
                    body = await asyncio.wait_for(response.body(), timeout=2)
                    entry.update(body_size=len(body), body_blank=not body.strip())
                except Exception:
                    pass  # 未知正文保留为普通请求错误，必须暂停。
            logger.warning("  页面请求失败: HTTP {} {} bytes={} {} [{}]", status,
                           content_type or "-", entry["body_size"], url.split("?", 1)[0],
                           entry.get("page_state") or "state:?")
            if page is not None and self._is_gateway_form(entry):
                self._poisoned_pages.add(page)
        except Exception:
            pass

    def _arm_login_response_listener(self, page) -> None:
        """在本次点击的页面上监听登录接口。同一个页面不重复挂。

        Args:
            page: 将要点「立即登录」的页面。

        Returns:
            None: 监听挂上后返回。页面不支持监听时直接返回。

        Examples:
            >>> WorkflowRunner._arm_login_response_listener.__name__
            '_arm_login_response_listener'
        """
        if page is None or any(current is page for current, _handler in self._login_listeners):
            return
        generation = self._login_generation

        def handler(response, _generation=generation) -> None:
            self._schedule_login_response(response, _generation)

        try:
            page.on("response", handler)
        except Exception as exc:
            logger.debug("  登录响应监听挂载失败: {}", exc)
            return
        self._login_listeners.append((page, handler))

    def _schedule_login_response(self, response, generation: int) -> None:
        """把登录响应的读取放到独立任务里，避免挡住事件回调。"""
        if generation != self._login_generation:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._capture_login_response(response, generation)
            )
        except RuntimeError:
            return
        self._login_listen_tasks.add(task)
        task.add_done_callback(self._login_listen_tasks.discard)

    async def _capture_login_response(self, response, generation: int) -> None:
        """只在本次监听代次内记录 ``POST /sso-auth/login`` 的 JSON 结论。

        Args:
            response: Playwright 响应。
            generation: 挂监听时的代次。过期后不再写入。

        Returns:
            None: 能判断时追加到 ``_login_submit_verdicts``。

        Examples:
            >>> interpret_login_submit(400, "text/html", "\\r\\n") is None
            True
        """
        if generation != self._login_generation:
            return
        try:
            request = response.request
            if str(getattr(request, "method", "")).upper() != "POST":
                return
            url = str(getattr(response, "url", "") or "")
            if "/sso-auth/login" not in url.split("?", 1)[0]:
                return
            headers = getattr(response, "headers", None) or {}
            content_type = headers.get("content-type", "") or ""
            text = ""
            try:
                body = await asyncio.wait_for(response.body(), timeout=2)
                text = body[:8000].decode("utf-8", "replace")
            except Exception:
                text = ""
            if generation != self._login_generation:
                return
            verdict = interpret_login_submit(int(response.status), content_type, text)
            if verdict is None:
                return
            verdict["at"] = time.monotonic()
            self._login_submit_verdicts.append(verdict)
            del self._login_submit_verdicts[:-10]
        except Exception:
            return

    async def _drain_login_captures(self) -> None:
        """等本次点击已经开始的登录响应读完。"""
        pending = [task for task in self._login_listen_tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=2)

    async def _disarm_login_response_listener(self) -> None:
        """卸下本次登录监听，并让迟到响应无法写入下一次重试。

        Returns:
            None: 监听卸掉后返回。

        Examples:
            >>> WorkflowRunner._disarm_login_response_listener.__name__
            '_disarm_login_response_listener'
        """
        await self._drain_login_captures()
        listeners = list(self._login_listeners)
        self._login_listeners.clear()
        for page, handler in listeners:
            remove = getattr(page, "remove_listener", None)
            if not callable(remove):
                continue
            with contextlib.suppress(Exception):
                remove("response", handler)
        self._login_generation += 1

    def _record_auth_traffic(
        self, request, response, status: int, url: str,
        content_type: str = "", body_size: int | None = None,
    ) -> None:
        """单独记录登录/验证码相关请求，用于区分“业务报错”和“网关拦截”。"""
        try:
            path = url.split("?", 1)[0]
            if not any(marker in path for marker in ("/sso-auth/", "/verify/", "/login", "/captcha")):
                return
            if not content_type:
                content_type = (response.headers or {}).get("content-type", "") or ""
            entry = {
                "at": time.strftime("%H:%M:%S"),
                "method": getattr(request, "method", ""),
                "status": status,
                "content_type": content_type,
                "url": url[:200],
                "path": path[-60:],
            }
            if body_size is not None:
                entry["body_size"] = body_size
            self._auth_traffic.append(entry)
            del self._auth_traffic[:-40]
            if status >= 400:
                logger.warning(
                    "  登录阶段请求: {} {} → HTTP {} {} body_bytes={}",
                    entry["method"], entry["path"], status, entry["content_type"] or "-",
                    body_size if body_size is not None else "-",
                )
        except Exception:
            pass

    def _on_page_console(self, message) -> None:
        try:
            if getattr(message, "type", "") != "error":
                return
            text = (message.text or "").strip()
            if not text:
                return
            self._console_errors.append({"at": time.strftime("%H:%M:%S"), "text": text[:500]})
            del self._console_errors[:-30]
        except Exception:
            pass

    def _on_page_pageerror(self, error) -> None:
        try:
            self._page_exceptions.append(f"{time.strftime('%H:%M:%S')} {error}")
            del self._page_exceptions[:-20]
        except Exception:
            pass

    def _unacknowledged_page_errors(self) -> list[dict]:
        return [item for item in self._page_errors if not item.get("_resolved")]

    def _acknowledge_page_errors(self, errors: list[dict] | None = None) -> None:
        for item in self._page_errors if errors is None else errors:
            item["_resolved"] = True

    @staticmethod
    def _describe_page_errors(errors: list[dict]) -> list[str]:
        """把页面请求错误收成短句。

        HTTP 0 会带上浏览器的 ``failure``（例如 ``net::ERR_ABORTED``）。
        没有这段文字时，日志里只有状态码，分不清连接被重置还是请求被页面取消。

        Args:
            errors: ``_page_errors`` 里的记录。

        Returns:
            list[str]: 每条是一种错误；相同错误合并次数。

        Examples:
            >>> text = WorkflowRunner._describe_page_errors([{
            ...     "status": 0, "resource_type": "xhr",
            ...     "url": "https://pm.gd.csg.cn/export", "failure": "net::ERR_ABORTED",
            ... }])[0]
            >>> text.endswith("failure=net::ERR_ABORTED")
            True
        """
        grouped: dict[str, int] = {}
        for item in errors:
            key = (
                f"HTTP {item.get('status')} {item.get('resource_type') or '-'} "
                f"ct={item.get('content_type') or '-'} "
                f"bytes={item.get('body_size') if item.get('body_size') is not None else '-'} "
                f"page={item.get('page_url') or '-'} "
                f"{str(item.get('url') or '').split('?', 1)[0]}"
            )
            failure = str(item.get("failure") or "").strip()
            if failure:
                key += f" failure={failure}"
            grouped[key] = grouped.get(key, 0) + 1
        return [f"{key}（{count} 次）" if count > 1 else key for key, count in grouped.items()]

    @classmethod
    def _is_gateway_form(cls, entry: dict) -> bool:
        """已观察到的可自动关页形态：同源 xhr/fetch、HTTP 400、text/html、极短空白正文。"""
        if entry.get("resource_type") not in ("xhr", "fetch"):
            return False
        try:
            status = int(entry.get("status") or 0)
        except (TypeError, ValueError):
            return False
        if status != 400:
            return False
        content_type = str(entry.get("content_type") or "").lower()
        if "json" in content_type:
            return False
        if content_type.split(";", 1)[0].strip() != "text/html":
            return False
        size = entry.get("body_size")
        return isinstance(size, int) and 0 <= size <= cls._GATEWAY_BODY_MAX and entry.get("body_blank") is True

    async def _wait_network_quiet(self, idle_ms: int = 600, timeout: int = 15000) -> None:
        """等待页面没有在途请求（含长轮询上限保护）。

        用于“业务数据真的请求完了再操作”的场景：日期切换后立刻点导出、
        或新标签页刚打开就点菜单，都会撞上页面自己的初始化请求。
        """
        if not self._inflight:
            return
        deadline = time.monotonic() + max(timeout, 0) / 1000
        idle_deadline = time.monotonic() + max(idle_ms, 0) / 1000
        last_seen = len(self._inflight)
        while time.monotonic() < deadline:
            if not self._inflight:
                return
            if len(self._inflight) < last_seen:
                idle_deadline = time.monotonic() + max(idle_ms, 0) / 1000
                last_seen = len(self._inflight)
            if time.monotonic() >= idle_deadline:
                return
            await asyncio.sleep(0.1)

    _MANUAL_DOC_HINT = "请回到已登录页重新点现货交易，不要刷新当前文档。"

    def _raise_if_recovery_blocked(self) -> None:
        reason = self._recovery_block_reason
        if not reason:
            return
        self._recovery_block_reason = ""
        raise WorkflowExecutionError(reason)

    def _recovery_checkpoint(self, index: int) -> int:
        """最近一次“打开新标签页”的点击。没有则退回当前步骤，调用方会因此拒绝刷新。"""
        if self.page in self._tab_checkpoints:
            return self._tab_checkpoints[self.page][0]
        for candidate in range(index, -1, -1):
            step = self.steps[candidate]
            if step.get("action") == "click" and step.get("expect") == "new_tab":
                return candidate
        return index

    @staticmethod
    def _page_is_open(page) -> bool:
        if page is None:
            return False
        try:
            return not page.is_closed()
        except Exception:
            return False

    @staticmethod
    def _page_url(page) -> str:
        try:
            return page.url or ""
        except Exception:
            return ""

    def _iter_known_pages(self) -> list:
        """绑定页、上下文里的页，以及浏览器其他上下文里的页。"""
        found: list = []

        def push(page) -> None:
            if page is None or any(page is item for item in found):
                return
            found.append(page)

        for page in list(self.pages):
            push(page)
        with contextlib.suppress(Exception):
            context = self.context
            if context is not None:
                for page in list(getattr(context, "pages", []) or []):
                    push(page)
        with contextlib.suppress(Exception):
            browser = self.browser
            if browser is None:
                return found
            for context in list(getattr(browser, "contexts", []) or []):
                for page in list(getattr(context, "pages", []) or []):
                    push(page)
        return found

    def _same_origin_stale_tabs(self, *, exclude: set) -> list:
        """同一 Context 里的同域业务标签。另一个 Context 不算这份会话的冲突页。"""
        from urllib.parse import urlsplit

        binding = self.pages[0] if self.pages else None
        target = ""
        with contextlib.suppress(Exception):
            target = urlsplit(self._page_url(binding)).netloc
        if not target:
            return []
        binding_context = getattr(binding, "context", None)
        stale = []
        for page in self._iter_known_pages():
            if any(page is item for item in exclude) or not self._page_is_open(page):
                continue
            page_context = getattr(page, "context", None)
            if (
                binding_context is not None
                and page_context is not None
                and page_context is not binding_context
            ):
                continue
            with contextlib.suppress(Exception):
                if urlsplit(self._page_url(page)).netloc == target:
                    stale.append(page)
        return stale

    def _closable_new_tabs(self) -> list:
        """本次新开、且不在运行前保护页里的后续标签。"""
        if len(self.pages) < 2 or not self._page_is_open(self.pages[0]):
            return []
        binding = self.pages[0]
        closable = []
        for page in self.pages[1:]:
            if page is binding or page in self._protected_pages:
                continue
            if not self._page_is_open(page):
                continue
            closable.append(page)
        return closable

    async def _pause_and_wait(self, message: str) -> None:
        """暂停并等待继续。额度只限制自动恢复，不取消错误。

        没有等待回调，或用户取消，就结束这次运行。继续后由调用方再检查一次。
        """
        self._paused = True
        self._emit("error_pause", error=message, message=message)
        self._emit("waiting_input", message="已暂停整个任务，等待人工处理")
        if not self._wait_continue:
            self._mark_failed(message)
            raise WorkflowPaused(message)
        continued = await self._wait_continue()
        if not continued:
            self._stopped = True
            self._mark_failed("用户取消")
            self._emit("aborted", message="用户取消")
            raise WorkflowPaused(message)
        self._paused = False

    async def _pause_for_stale_tabs(self, urls: list[str]) -> None:
        """列出同 Context 的同域旧标签并暂停。不关闭这些页，也不重放。"""
        listed = "；".join(urls)
        message = (
            f"检测到 {len(urls)} 个同域旧标签（{listed}）。"
            "这些页面不会被关闭，也不能带着它们重放。"
            + self._MANUAL_DOC_HINT
        )
        self._emit(
            "stale_tabs_warning",
            count=len(urls),
            pages=urls,
            message=message,
        )
        await self._pause_and_wait(message)

    def _emit_http_rejection(self, index: int, errors: list[dict], *, gateway: bool) -> None:
        described = "；".join(self._describe_page_errors(errors))[:500]
        if gateway:
            message = f"已记录网关形态的同源 HTTP 400（text/html，极短空白正文）：{described}"
        else:
            message = f"已记录非网关形态的 HTTP 错误，暂停诊断，不会自动关闭标签重放：{described}"
        self._emit("gateway_rejected", step=index + 1, error=described, message=message)

    def _recovery_hold_message(self, index: int, errors: list[dict]) -> str:
        described = "；".join(self._describe_page_errors(errors))[:500]
        checkpoint = self._recovery_checkpoint(index)
        if not self._page_recovery_enabled:
            why = "自动页面恢复已关闭"
        elif self._recovery_attempts >= self._max_recoveries:
            why = "自动恢复次数已用完"
        elif self._recovery_budget.get(checkpoint, 0) >= 1:
            why = f"检查点 {checkpoint + 1} 已经重放过一次"
        else:
            why = "没有安全的自动恢复路径"
        return f"{why}，不能把这次错误当成成功后继续。{described}。{self._MANUAL_DOC_HINT}"

    async def _maybe_recover(self, index: int, *, stage: str) -> bool:
        """区分“发现错误”和“是否还允许自动关页重放”。

        返回 True 表示应从检查点重放。没有未处理错误时返回 False。
        重复失败、额度用尽、恢复关闭、形态不明或没有安全路径时暂停，不返回 False。
        """
        self._replay_index = index
        self._recovery_block_reason = ""
        while True:
            await self._drain_response_captures()
            if self._inflight:
                await self._wait_network_quiet(idle_ms=300, timeout=3000)
                await self._drain_response_captures()
            errors = self._unacknowledged_page_errors()
            if not errors:
                return False
            step = self.steps[index]
            if self.config.get("type") == "login" or step.get("_login_step"):
                # 登录前的 logged/info 401 是未登录探测，可以确认后继续表单；
                # 点过“立即登录”后再出现任何业务请求错误，必须告警并暂停，
                # 否则会继续走后续步骤，最终把失败流程误认为完成。
                if self._login_submit_seen:
                    self._emit_http_rejection(index, errors, gateway=False)
                    described = "；".join(self._describe_page_errors(errors))[:500]
                    await self._pause_and_wait(
                        "登录提交后请求异常，已暂停整个任务，不会继续执行后续步骤。"
                        f"{described}。请处理证书/账号/验证码或网络问题后再继续。"
                    )
                    continue
                # 登录提交后的网关空 HTML 400 只暂停。不刷新当前文档，也不重放立即登录。
                gateway = [item for item in errors if self._is_gateway_form(item)]
                if gateway and len(gateway) == len(errors):
                    self._emit_http_rejection(index, gateway, gateway=True)
                    await self._pause_and_wait(
                        "登录提交后出现网关形态的空 HTML 400，已暂停。"
                        "不要刷新当前文档，不要重复点击立即登录。"
                        + self._MANUAL_DOC_HINT
                    )
                    continue
                self._acknowledge_page_errors()
                return False
            gateway = [item for item in errors if self._is_gateway_form(item)]
            other = [item for item in errors if item not in gateway]
            if other:
                self._emit_http_rejection(index, other, gateway=False)
                described = "；".join(self._describe_page_errors(other))[:500]
                await self._pause_and_wait(
                    "请求异常已记录，不会自动关闭标签重放。"
                    f"{described}。JSON 业务错误、401、403、404、429 和 5xx 需要暂停诊断。"
                    + self._MANUAL_DOC_HINT
                )
                continue
            self._emit_http_rejection(index, gateway, gateway=True)
            checkpoint = self._recovery_checkpoint(index)
            can_auto = (
                self._page_recovery_enabled
                and self._recovery_attempts < self._max_recoveries
                and self._recovery_budget.get(checkpoint, 0) < 1
            )
            if not can_auto:
                logger.warning("  {}", self._recovery_hold_message(index, gateway))
                await self._pause_and_wait(self._recovery_hold_message(index, gateway))
                continue
            failed_pages = {item.get("_page") for item in gateway}
            if len(failed_pages) != 1 or None in failed_pages or self.page not in failed_pages:
                await self._pause_and_wait("错误不属于当前唯一业务页面，不能自动关闭标签。" + self._MANUAL_DOC_HINT)
                continue
            replayed = self.steps[checkpoint + 1:index + (stage == "after")]
            if any(item.get("action") not in ("click", "wait", "pick_date", "pick_date_range")
                   for item in replayed):
                await self._pause_and_wait("重放范围含导出或其他有副作用的步骤，禁止自动重做。" + self._MANUAL_DOC_HINT)
                continue
            reason = "；".join(self._describe_page_errors(gateway))[:500]
            if await self._rebuild_document(checkpoint, reason, stage=stage):
                self._recovery_budget[checkpoint] = 1
                self._replay_index = checkpoint
                return True

    async def _rebuild_document(
        self, checkpoint: int, reason: str, *, stage: str = "after"
    ) -> bool:
        """关掉本次新开的非保护标签，回到绑定页。不能安全这样做就暂停并返回 False。"""
        step = self.steps[checkpoint]
        reopens_tab = step.get("action") == "click" and step.get("expect") == "new_tab"
        binding = self.pages[0] if self.pages else None
        binding_open = self._page_is_open(binding)
        proven_popup = self._tab_checkpoints.get(self.page) == (checkpoint, binding)
        closable = [p for p in self._closable_new_tabs() if p is self.page] if reopens_tab and binding_open and proven_popup else []
        hint = self._MANUAL_DOC_HINT
        if not reopens_tab:
            message = f"当前单步检查点不是「点击并打开新标签」，不能自动重放。{reason}。{hint}"
            await self._pause_and_wait(message)
            return False
        if not (reopens_tab and binding_open and closable):
            stale = self._same_origin_stale_tabs(exclude={binding, self.page})
            if stale:
                await self._pause_for_stale_tabs([self._page_url(p)[:120] for p in stale])
            if not reopens_tab:
                detail = "检查点不是「点击并打开新标签」"
            elif not binding_open:
                detail = "绑定页已关闭或不存在"
            else:
                detail = "没有可关闭的新标签"
            message = f"{detail}。{hint}"
            logger.warning("  无法重放现货交易：{}", message)
            await self._pause_and_wait(message)
            return False

        exclude = {binding, *closable}
        stale = self._same_origin_stale_tabs(exclude=exclude)
        if stale:
            urls = [(self._page_url(page) or "(空 URL)")[:120] for page in stale]
            await self._pause_for_stale_tabs(urls)
            stale = self._same_origin_stale_tabs(exclude=exclude)
            if stale:
                message = f"同域旧标签仍在，不会重放。{hint}"
                logger.warning("  {}", message)
                await self._pause_and_wait(message)
                return False

        try:
            entry_visible = await binding.locator(step["selector"]).first.is_visible()
        except Exception:
            entry_visible = False
        if not entry_visible:
            await self._pause_and_wait("绑定门户已无法确认原业务入口，不关闭业务标签。" + hint)
            return False

        logger.warning(
            "  检测到网关形态 HTTP 400（{}），关闭本次新开的标签并从步骤 {}/{} 重新点击",
            reason, checkpoint + 1, len(self.steps),
        )
        for page in closable:
            try:
                self._retired_pages.add(page)
                await page.close()
            except Exception as exc:
                self._retired_pages.discard(page)
                message = f"关闭本次新开的标签失败：{exc}。{hint}"
                logger.warning("  {}", message)
                await self._pause_and_wait(message)
                return False
        retained = [binding]
        for page in self.pages:
            if page is binding or page in closable:
                continue
            if self._page_is_open(page):
                retained.append(page)
        self.pages = retained
        self._current_page_idx = 0
        with contextlib.suppress(Exception):
            await binding.bring_to_front()
        self._recovery_attempts += 1
        self._emit(
            "page_recovery",
            reason=reason,
            url=self._page_url(binding),
            replay_from=checkpoint + 1,
            stage=stage,
            attempt=self._recovery_attempts,
            message=(
                f"已关闭本次新开的标签，回到绑定页并从步骤 {checkpoint + 1} 重新点击。"
                "不刷新当前文档。"
            ),
        )
        self._acknowledge_page_errors([
            item for item in self._page_errors if item.get("_page") in closable
        ])
        with contextlib.suppress(Exception):
            self._observe_page(self.page)
        await self._wait_network_quiet(idle_ms=500, timeout=8000)
        current = self.page
        self._emit("page_recovery_done", step=checkpoint + 1, url=current.url if current else "")
        return True

    async def _save_page_html(self, prefix: str) -> str:
        """把当前页面 HTML 落到失败目录，供这次执行的诊断包带走。

        Args:
            prefix (str): 文件名前缀，只保留安全字符。

        Returns:
            str: 保存路径。页面不存在或写入失败时为空字符串。
        """
        page = self.page
        if page is None:
            return ""
        try:
            html = await page.content()
        except Exception as exc:
            logger.debug("  读取页面 HTML 失败: {}", exc)
            return ""
        if len(html) > 2_000_000:
            html = html[:2_000_000] + "\n<!-- 已截断 -->"
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix).strip("_") or "page"
        path = self.download_dir / "_failures" / f"{safe}.html"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
        except OSError as exc:
            logger.debug("  页面 HTML 保存失败: {}", exc)
            return ""
        logger.info("  页面 HTML 已保存: {}", path)
        return str(path)

    async def _dump_page_diagnostics(self, step_no: int) -> str:
        """失败时把控制台异常、页面异常、4xx 记录落盘，下次定位不用靠猜。"""
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = self.download_dir / "_failures" / f"diagnostics_{stamp}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            page = self.page
            storage_keys: list[str] = []
            run_day = ""
            if page is not None:
                with contextlib.suppress(Exception):
                    storage_keys = await page.evaluate(
                        "() => { const out = []; for (let i = 0; i < localStorage.length; i += 1) "
                        "out.push(localStorage.key(i)); return out; }"
                    )
                with contextlib.suppress(Exception):
                    run_day = await page.evaluate("() => localStorage.getItem('runDay') || ''")
            lines = [
                f"step: {step_no}",
                f"workflow: {self.name}",
                f"url: {page.url if page is not None else ''}",
                f"title: {await page.title() if page is not None else ''}",
                f"runDay: {run_day}",
                f"localStorage keys: {', '.join(storage_keys)}",
                f"page_recovery_enabled: {self._page_recovery_enabled}",
                f"recoveries: {self._recovery_attempts}",
                "",
                "--- gateway 4xx/5xx ---",
                *[
                    f"{item['at']} HTTP {item['status']} {item.get('resource_type') or '-'} "
                    f"ct={item.get('content_type') or '-'} "
                    f"bytes={item.get('body_size') if item.get('body_size') is not None else '-'} "
                    f"page={item.get('page_url') or '-'} {item['url']}"
                    for item in self._page_errors[-20:]
                ],
                "",
                "--- console errors ---",
                *[f"{item['at']} {item['text']}" for item in self._console_errors[-20:]],
                "",
                "--- page exceptions ---",
                *self._page_exceptions[-20:],
            ]
            path.write_text("\n".join(lines), encoding="utf-8")
            await self._save_page_html(f"page_step_{step_no}_{stamp}")
            logger.info("  页面诊断已保存: {}", path)
            return str(path)
        except Exception as exc:
            logger.debug("  页面诊断保存失败: {}", exc)
            return ""

    # ----- 步骤分发 -----

    def _indicator_id(self) -> str:
        """当前指标 id。任务和播放都优先用控制名。

        Returns:
            str: 指标 id。

        Examples:
            >>> runner = WorkflowRunner({"name": "用电量", "_control_name": "power"}, download_dir="/tmp/spider-gd-plan-doctest")
            >>> runner._indicator_id()
            'power'
        """
        return str(self.config.get("_control_name") or self.name)

    def _download_root(self) -> Path:
        """这棵结果树的根目录。

        Returns:
            Path: 下载目录和各输出目录的公共父目录。
        """
        return download_tree_root(self.download_dir, self.output_dirs)

    def _active_output_key(self) -> str:
        """当前正在执行的输出位置。循环里用循环键，否则用顶层步骤号。

        Returns:
            str: 如 ``s3.1``。
        """
        nested = str(getattr(self, "_repeat_output_key", "") or "")
        if nested:
            return nested
        return f"s{getattr(self, '_executing_step_index', 0)}"

    def _file_date_text(self, when: date | None = None) -> str:
        """结果行用来对齐预期文件的日期文本。

        Args:
            when: 指定日期。为空时用当前目标日或按月月份。

        Returns:
            str: ``YYYY-MM-DD``、``YYYY-MM`` 或空字符串。
        """
        target = when or self.target_date
        if isinstance(target, date):
            return target.isoformat()
        return str(self.query_month or "")

    def _emit_download_plan(self) -> None:
        """在步骤开始前抛出预期下载清单。"""
        for item in plan_download_files(
            self.config,
            indicator=self._indicator_id(),
            download_dir=self.download_dir,
            output_dirs=self.output_dirs,
            repeat_dates=self.repeat_dates,
            output_repeat_dates=self.output_repeat_dates,
            target_date=self.target_date,
            query_month=self.query_month,
        ):
            self._emit("file_expected", **item)

    def _note_file(self, status: str, path: Path | None = None, reason: str = "", when: date | None = None) -> None:
        """抛出单个文件的最终状态，供界面挂到预期行上。

        Args:
            status: ``saved``、``empty`` 或 ``failed``。
            path: 实际落盘路径。失败或没数据时可以没有。
            reason: 失败或没数据的说明。
            when: 覆盖当前目标日，按天报告用得到。
        """
        output_key = str(getattr(self, "_active_file_key", "") or "") or self._active_output_key()
        self._emit(
            "file_result",
            status=status,
            indicator=self._indicator_id(),
            output_key=output_key,
            date=self._file_date_text(when),
            name=Path(path).name if path else "",
            folders=relative_folders(Path(path), self._download_root()) if path else [],
            reason=reason,
            path=str(path or ""),
        )

    async def _exec_step(self, step: dict) -> None:
        action = step.get("action", "")
        if action in OUTPUT_ACTIONS:
            self._active_file_key = self._active_output_key()
        trigger_actions = ("click", "pick_date", "pick_date_range", "export", "captcha", "fill", "check")
        if action in trigger_actions and self.page in self._poisoned_pages:
            await self._pause_and_wait(
                "当前文档已被网关以空 HTML 400 拒绝过，继续在这份文档上操作只会继续 400。"
                "请新开一个业务文档，或在浏览器里重新点「现货交易」进入新文档后再继续。"
                + self._MANUAL_DOC_HINT
            )
        if action == "ensure_login":
            await self._do_ensure_login(step)
        elif action == "login":
            await self._do_login(step)
        elif action == "navigate":
            await self._do_navigate(step)
        elif action == "click":
            await self._do_click(step)
        elif action == "wait":
            await self._do_wait(step)
        elif action == "pick_date":
            await self._do_pick_date(step)
        elif action == "export":
            await self._do_export(step)
        elif action == "switch_tab":
            await self._do_switch_tab(step)
        elif action == "close_tab":
            await self._do_close_tab(step)
        elif action == "close_other_tabs":
            await self._do_close_other_tabs(step)
        elif action == "extract":
            await self._do_extract(step)
        elif action == "pause":
            await self._do_pause(step)
        elif action == "screenshot":
            await self._do_screenshot(step)
        elif action == "fill":
            await self._do_fill(step)
        elif action == "check":
            await self._do_check(step)
        elif action == "captcha":
            await self._do_captcha(step)
        elif action == "dismiss_modal":
            await self._do_dismiss_modal(step)
        elif action == "hover":
            await self._do_hover(step)
        elif action == "select":
            await self._do_select(step)
        elif action == "pick_date_range":
            await self._do_pick_date_range(step)
        elif action == "extract_table":
            await self._do_extract_table(step)
        elif action == "extract_echarts":
            await self._do_extract_echarts(step)
        elif action == "extract_calendar":
            await self._do_extract_calendar(step)
        elif action == "extract_calendar_month_weeks":
            await self._do_extract_calendar_month_weeks(step)
        elif action == "select_calendar_week":
            await self._do_select_calendar_week(step)
        elif action == "intercept_api":
            await self._do_intercept_api(step)
        elif action == "collect_api":
            await self._do_collect_api(step)
        elif action == "api_call":
            await self._do_api_call(step)
        elif action in ("download_notice_reports", "download_reports"):
            await self._do_download_notice_reports(step)
        elif action == "repeat":
            await self._do_repeat(step)
        elif action in ("reset_site_storage", "reset_storage"):
            await self._do_reset_site_storage(step)
        else:
            logger.warning("未知步骤类型: {}", action)

    # ----- 具体实现 -----

    async def _do_repeat(self, step: dict) -> None:
        """按日期重复选日期和导出。没有入参时用步骤上的默认窗口。

        某一天导出失败（下载超时、HTTP 0、页面提示）时记下原因并继续后面的日期。
        网关 400 和按钮缺失仍会立刻结束整次运行。循环结束后，只要有日期失败，
        就抛出汇总，避免任务被标成全部成功。

        Args:
            step: ``repeat`` 步骤，含嵌套 ``steps`` 和可选日期窗口。

        Returns:
            None: 每一天都成功或明确无数据时返回。

        Raises:
            WorkflowExecutionError: 至少有一天导出失败。
            WorkflowPaused: 文档签名被网关拒绝，或用户取消暂停。

        Examples:
            >>> WorkflowDateExportFailed("HTTP 0").__class__.__name__
            'WorkflowDateExportFailed'
        """
        nested = [item for item in (step.get("steps") or []) if isinstance(item, dict)]
        dates = resolve_repeat_dates(step, self.config, self.repeat_dates, date.today())
        previous_prefix = getattr(self, "_repeat_output_key", None)
        prefix = previous_prefix or f"s{self._executing_step_index}"
        own_dates = dict(getattr(self, "output_repeat_dates", None) or {})
        if self.repeat_dates is None:
            # 直接执行：导出步骤上写的 start/end 是它自己的默认范围；任务传日期时已在排计划时算过
            own_dates = {**step_output_dates(nested, prefix, date.today()), **own_dates}
        rounds = plan_repeat_rounds(nested, prefix, dates, own_dates)
        if not rounds:
            logger.info("  日期循环没有可执行的日期")
            return
        saved = self.target_date
        failures: list[str] = []
        try:
            for index, (current, indexes) in enumerate(rounds, start=1):
                if self._stopped:
                    break
                self.target_date = current
                logger.info("  日期循环 {}/{}：{}", index, len(rounds), current.isoformat())
                self._emit(
                    "repeat_date",
                    date=current.isoformat(),
                    index=index,
                    total=len(rounds),
                    summary=f"按 {current.isoformat()} 采集（{index}/{len(rounds)}）",
                )
                try:
                    for nested_index in indexes:
                        nested_step = nested[nested_index]
                        key = f"{prefix}.{nested_index}"
                        self._active_output_dir = self.output_dirs.get(key, "")
                        self._repeat_output_key = key
                        pinned = pin_step_dates(nested_step, current)
                        pinned = self._resolve_repeat_placeholders(pinned, current)
                        await self._exec_step(pinned)
                except WorkflowSkipRepeatRound as exc:
                    logger.info("  日期 {} 无数据，跳过本轮：{}", current.isoformat(), exc)
                    self._emit(
                        "repeat_no_data",
                        date=current.isoformat(),
                        message=str(exc),
                        indicator=self._indicator_id(),
                    )
                    continue
                except WorkflowDateExportFailed as exc:
                    logger.error("  日期 {} 导出失败，继续后续日期：{}", current.isoformat(), exc)
                    self._note_file("failed", reason=str(exc), when=current)
                    self._emit(
                        "repeat_date_failed",
                        date=current.isoformat(),
                        message=str(exc),
                        indicator=self._indicator_id(),
                    )
                    failures.append(f"{current.isoformat()}：{exc}")
                    continue
        finally:
            self.target_date = saved
            self._repeat_output_key = previous_prefix
        if failures:
            raise WorkflowExecutionError("部分日期导出失败：" + "；".join(failures))

    @staticmethod
    def _resolve_repeat_placeholders(value: Any, target: date) -> Any:
        """在 repeat 每轮开始前递归替换日期文本，避免占位符进入网页输入框。"""
        if isinstance(value, dict):
            return {key: WorkflowRunner._resolve_repeat_placeholders(item, target) for key, item in value.items()}
        if isinstance(value, list):
            return [WorkflowRunner._resolve_repeat_placeholders(item, target) for item in value]
        if isinstance(value, str):
            return (
                value.replace("{target_ym}", f"{target.year}年{target.month}月")
            )
        return value

    async def _do_ensure_login(self, step: dict) -> None:
        """只验证现有页面并等待手工登录；不导航、不清存储、不提交表单。"""
        while True:
            reason = ""
            expected_origin = step.get("origin", "")
            if expected_origin and not self._same_origin(self._page_url(self.page), expected_origin):
                reason = "请在绑定的浏览器页面手工打开业务门户并登录"
            elif is_public_portal_url(self._page_url(self.page)):
                reason = "当前是门户公开首页或登录页，请先完成登录"
            else:
                try:
                    selector = step.get("success_selector", GD_LOGIN_SUCCESS_SELECTOR)
                    if (selector in ("text=现货交易", ".el-menu", ".ant-menu-horizontal, .el-menu")
                            and self._same_origin(self._page_url(self.page), "https://pm.gd.csg.cn")):
                        selector = GD_LOGIN_SUCCESS_SELECTOR
                    if not await self._dom_visible(selector):
                        reason = "尚未出现门户业务入口，请在当前页面手工完成登录"
                    elif login_error := await self._read_login_error(step):
                        reason = f"页面仍有登录错误提示：{login_error}，请人工处理"
                except Exception:
                    reason = "无法确认绑定页面的登录状态，请检查页面是否仍打开"
            if not reason:
                return
            await self._pause_and_wait(reason + "；完成后点击继续。不要刷新失败页面。")

    async def _do_login(self, step: dict) -> None:
        """打开登录页。显式登录工作流会继续执行点击、填写和验证码。

        指标里的 login 占位，或全局会话是 manual/reuse_only 且这次不是登录流程时，
        只检查现有登录，不提交表单。已经完整登录时跳过后续表单。
        """
        session_blocks_auto = self.config.get("auth_policy") in ("manual", "reuse_only") or (
            self._shared_session and self._shared_session._config["auth_policy"] in ("manual", "reuse_only")
        )
        if session_blocks_auto and not self._explicit_login_run():
            await self._do_ensure_login(step)
            self._skip_remaining_login_steps = True
            return
        url = step.get("url", "https://pm.gd.csg.cn/portal/#/home")
        state_file = step.get("restore_state", None)

        if self._shared_session is not None:
            auth = await self._shared_session.check_auth(self.page)
            if auth["state"] == "authenticated":
                self._skip_remaining_login_steps = True
                return
            if state_file:
                state_file = None  # 已接管会话不允许关闭 Context 后恢复快照
        if state_file and Path(state_file).exists():
            logger.info("  尝试恢复 storage_state: {}", state_file)
            await self.context.close()
            self.context = await self.browser.new_context(
                viewport={"width": 1440, "height": 900},
                accept_downloads=True,
                storage_state=state_file,
            )
            self.context.on("page", self._on_new_page)
            new_page = await self.context.new_page()
            self.pages = [new_page]
            self._current_page_idx = 0
            await new_page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            logger.info("  storage_state 恢复完成")
            return

        # 只导航，不暂停——SPA 页面有 WebSocket，不能用 networkidle
        await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)  # 等 SPA 渲染完
        logger.info("  导航到: {}", url)
        if self.config.get("type") == "login" and (
            await self._login_stage_ok() or await self._account_marker_visible()
        ):
            self._skip_remaining_login_steps = True
            logger.info("  已检测到完整登录状态，跳过后续登录表单步骤")

    async def _do_navigate(self, step: dict) -> None:
        """导航到指定 URL。如果 expect=new_tab，在新标签页打开。

        当前文档已经在目标 URL 上时默认不重载。pm.gd.csg.cn 的请求签名和文档实例绑定，
        重载同一地址会换掉签名，新文档可能暂时或一直没有「现货交易」等入口。
        ``reload: true`` 或 ``skip_if_loaded: false`` 才强制重载。

        Args:
            step (dict): 步骤。``url`` 为目标地址；``expect`` 为 ``new_tab`` 时新开标签。

        Returns:
            None: 导航或确认已在目标页后返回。

        Examples:
            >>> WorkflowRunner._same_document_url(
            ...     "https://pm.gd.csg.cn/portal/#/powerEnterprise",
            ...     "https://pm.gd.csg.cn/portal#/powerEnterprise",
            ... )
            True
        """
        url = step.get("url", "")
        expect = step.get("expect", "")

        if expect == "new_tab":
            new_page = await self.context.new_page()
            self.pages.append(new_page)
            self._current_page_idx = len(self.pages) - 1
            self._observe_page(new_page)
            await new_page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            await self._bring_page_to_front(new_page)
            logger.info("  在新标签页打开: {}", url)
        else:
            current = ""
            with contextlib.suppress(Exception):
                current = self.page.url
            reload = bool(step.get("reload")) or step.get("skip_if_loaded") is False
            if not reload and self._same_document_url(current, url):
                logger.info("  已在目标页面，跳过导航（避免重载换掉请求签名）: {}", current)
            elif not reload and await self._reuse_open_document(url):
                logger.info("  改用已经打开的同地址标签，不在当前文档上重开: {}", url)
            else:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)
                logger.info("  导航到: {}", url)
        await self._wait_network_quiet(idle_ms=step.get("settle_idle_ms", 500),
                                       timeout=step.get("settle_timeout", 8000))

    @staticmethod
    def _same_document_url(current: str, target: str) -> bool:
        if not current or not target:
            return False
        from urllib.parse import urlsplit

        try:
            left, right = urlsplit(current), urlsplit(target)
        except Exception:
            return False
        return (
            left.scheme, left.netloc, left.path.rstrip("/"), left.query, left.fragment
        ) == (
            right.scheme, right.netloc, right.path.rstrip("/"), right.query, right.fragment
        )

    @staticmethod
    def _describe_same_url_tabs(count: int) -> str:
        """地址与绑定页完全相同的其他标签，不能再只报同一个 URL。

        Args:
            count (int): 同域且地址与绑定页相同的未绑定标签数量。

        Returns:
            str: 拼进点击失败原因的句子。数量小于 1 时为空字符串。

        Examples:
            >>> "另一份文档" in WorkflowRunner._describe_same_url_tabs(2)
            True
            >>> WorkflowRunner._describe_same_url_tabs(0)
            ''
        """
        if count < 1:
            return ""
        return (
            f"。同域另有 {count} 个标签，地址与绑定页完全相同，是另一份文档，不是同一个标签。"
            "请在专用浏览器顶部改选已经显示出目标元素的那一个，等它变成当前绑定页后再执行；"
            "地址相同也不会让这一步自动改去点它"
        )

    def _click_miss_message(self, selector: str) -> str:
        """目标不在当前绑定页时，把同域未绑定标签写进失败原因。

        单步从当前绑定页开始，不会改去点另一个标签。空闲时绑定页跟随专用浏览器正在显示的标签。
        地址相同的标签单独说明，避免「绑定页和未绑定页是同一个链接」这种互相矛盾的提示。

        Args:
            selector (str): 未点到的选择器。

        Returns:
            str: 给界面和日志的失败原因。

        Examples:
            >>> WorkflowRunner._describe_same_url_tabs(1).startswith("。同域另有 1 个标签")
            True
        """
        message = f"点击目标未出现或不可操作: {selector}"
        bound = self._page_url(self.page)
        if isinstance(bound, str) and bound:
            message += f"。这一步打在绑定页 {bound} 上"
        same_url = 0
        others = []
        for page in self._same_origin_stale_tabs(exclude={self.page}):
            url = self._page_url(page)
            if not isinstance(url, str) or not url:
                continue
            if self._same_document_url(url, bound):
                same_url += 1
            else:
                others.append(url[:120])
        message += self._describe_same_url_tabs(same_url)
        if not others:
            return message
        return (
            message
            + "。同域还有未绑定的业务标签："
            + "；".join(others)
            + "。请先在专用浏览器里打开有目标元素的标签，等顶部显示该标签后再执行；这一步不会自动改去点它"
        )

    async def _call_optional(self, func, *args, **kwargs) -> None:
        """调用可能是同步、也可能是协程的方法。测试替身不是协程时不能 await。"""
        if not callable(func):
            return
        try:
            result = func(*args, **kwargs)
        except TypeError:
            if not kwargs:
                raise
            result = func(*args)
        if inspect.isawaitable(result):
            await result

    def _bind_page(self, page) -> None:
        """把执行绑定到这个已经打开的页面。

        Args:
            page: 要作为当前页的标签。

        Returns:
            None: 页面进入 ``pages`` 并成为当前下标。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> page = object()
            >>> runner._bind_page(page)
            >>> runner.page is page
            True
        """
        if page not in self.pages:
            self.pages.append(page)
        self._current_page_idx = self.pages.index(page)
        self._observe_page(page)

    async def _reuse_open_document(self, url: str) -> bool:
        """当前文档不是目标地址时，改绑到已经打开的同一份地址。

        在业务页上直接 ``goto`` 门户，会再造一份没有菜单的同地址文档，
        原来那份带「结算管理」的工作台还留在旁边。

        Args:
            url: 步骤要打开的地址。

        Returns:
            bool: 已经改绑到现成标签时为 True。没有现成标签时为 False。

        Examples:
            >>> WorkflowRunner._same_document_url(
            ...     "https://pm.gd.csg.cn/portal/#/powerEnterprise",
            ...     "https://pm.gd.csg.cn/portal/#/powerEnterprise",
            ... )
            True
        """
        matches = []
        for page in self._iter_known_pages():
            if not self._page_is_open(page):
                continue
            if self._same_document_url(self._page_url(page), url):
                matches.append(page)
        workbench = getattr(self, "_workbench_page", None)
        if self.page in matches and (workbench is None or self.page is workbench):
            return False
        if workbench in matches:
            chosen = workbench
        else:
            protected = [page for page in matches if page in self._protected_pages]
            chosen = protected[0] if protected else (matches[0] if matches else None)
        if chosen is None:
            return False
        self._bind_page(chosen)
        await self._bring_page_to_front(chosen)
        return True

    async def _adopt_tab_with_target(self, selector: str) -> bool:
        """当前页没有目标时，改绑到同域里已经显示出该元素的标签。

        Args:
            selector: 要点的选择器。

        Returns:
            bool: 找到并改绑成功时为 True。

        Examples:
            >>> "结算管理" in "text=结算管理"
            True
        """
        current = self.page
        for page in self._iter_known_pages():
            if page is current or not self._page_is_open(page):
                continue
            bound = self._page_url(current)
            if bound and not self._same_origin(self._page_url(page), bound):
                continue
            self._bind_page(page)
            try:
                visible = await self._dom_visible(selector, timeout=300)
            except Exception:
                visible = False
            if not visible:
                continue
            await self._bring_page_to_front(page)
            logger.info("  目标在另一份标签上，已改绑: {}", self._page_url(page))
            return True
        if current is not None:
            self._bind_page(current)
        return False

    async def _drop_duplicate_tabs_missing(self, selector: str) -> None:
        """关掉同地址、且没有当前目标元素的非保护标签。

        Args:
            selector: 刚刚点到的元素。还显示着它的标签会留下。

        Returns:
            None: 当前绑定页保持不变。

        Examples:
            >>> WorkflowRunner._same_document_url("https://a/#/x", "https://a/#/x")
            True
        """
        keep = self.page
        url = self._page_url(keep)
        from workbench_tabs import is_workbench_url

        # 只收重复的门户工作台。业务页留到本次指标结束再关，避免在 pfjy 上多做探测。
        if not is_workbench_url(url):
            return
        workbench = getattr(self, "_workbench_page", None)
        duplicates = [
            page for page in self._iter_known_pages()
            if page is not keep
            and page is not workbench
            and page not in self._protected_pages
            and self._page_is_open(page)
            and self._same_document_url(self._page_url(page), url)
        ]
        for page in duplicates:
            self._bind_page(page)
            try:
                visible = await self._dom_visible(selector, timeout=200)
            except Exception:
                visible = False
            if visible:
                continue
            try:
                close = getattr(page, "close", None)
                if close is None:
                    continue
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning("  关闭重复标签失败: {}", exc)
                continue
            self.pages = [item for item in self.pages if item is not page]
            logger.info("  已关闭没有目标元素的重复标签: {}", url)
        if keep is not None and self._page_is_open(keep):
            self._bind_page(keep)

    async def _bring_page_to_front(self, page) -> None:
        """把绑定页设成窗口里选中的标签。

        只调 Playwright 的 ``bring_to_front``（对应 CDP ``Target.activateTarget``）。
        失败时再试一次 CDP，并写警告。关掉当前标签或新开业务页之后必须调用，
        否则标签栏会没有任何选中项，瑞数也会把后台页的签名判失效。

        Args:
            page: 要激活的页面。空页面直接返回。

        Returns:
            None: 激活结束后返回。页面没有这个方法时也返回。

        Examples:
            >>> inspect.iscoroutinefunction(WorkflowRunner._bring_page_to_front)
            True
        """
        if page is None:
            return
        bring = getattr(page, "bring_to_front", None)
        try:
            await self._call_optional(bring)
            return
        except Exception as exc:
            logger.warning("  绑定页未能带到前台: {}", exc)
        try:
            session = await page.context.new_cdp_session(page)
        except Exception:
            return
        try:
            info = await session.send("Target.getTargetInfo")
            target_id = str((info.get("targetInfo") or {}).get("targetId") or "")
            if target_id:
                await session.send("Target.activateTarget", {"targetId": target_id})
        except Exception as exc:
            logger.warning("  CDP 激活标签失败: {}", exc)
        finally:
            with contextlib.suppress(Exception):
                await session.detach()

    async def _human_move_to(self, box: dict) -> None:
        """按人的轨迹分段移到 box 中心附近并停住；只移动，不按键。

        ``_press_at`` 与 ``_do_hover`` 共用；落点写回 ``_pointer_pos``。
        """
        tx = box["x"] + box["width"] * random.uniform(0.42, 0.58)
        ty = box["y"] + box["height"] * random.uniform(0.42, 0.58)
        start = self._pointer_pos
        if start is None:
            start = (
                max(0.0, tx - random.uniform(40, 90)),
                max(0.0, ty - random.uniform(20, 60)),
            )
        steps = random.randint(7, 12)
        for index in range(1, steps + 1):
            ratio = index / steps
            ease = 1 - (1 - ratio) ** 2  # 先快后慢，接近伸手
            x = start[0] + (tx - start[0]) * ease + random.uniform(-1.2, 1.2)
            y = start[1] + (ty - start[1]) * ease + random.uniform(-1.2, 1.2)
            await self.page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.02, 0.045))
        self._pointer_pos = (tx, ty)
        await asyncio.sleep(random.uniform(0.15, 0.30))

    async def _press_at(self, box: dict) -> None:
        """按人的轨迹移到 box 中心附近，停一下，按住再抬起。

        按下之前出错抛 ``_PointerUnavailableError``，调用方可以换路径；
        已经按下后出错先释放按键再原样抛出，不允许再点一次。
        """
        pressed = False
        try:
            await self._human_move_to(box)
            await self.page.mouse.down()
            pressed = True
            # 人手按住普遍在 150~260ms（现场三次手动点击 225/227/225ms）。
            await asyncio.sleep(random.uniform(0.16, 0.26))
            await self.page.mouse.up()
            pressed = False
        except Exception as exc:
            if pressed:
                with contextlib.suppress(Exception):
                    await self.page.mouse.up()
                raise
            if "Timeout" in type(exc).__name__:
                raise
            raise _PointerUnavailableError(str(exc)) from exc

    # ----- 普通数据探测与输入（不创建元素句柄） -----

    _DOM_PROBE_JS = DOM_PROBE_JS

    async def _dom_probe(self, op: str, selector: str, **extra: Any) -> Any:
        try:
            return await self.page.evaluate(self._DOM_PROBE_JS, {
                "op": op, "selector": selector, **extra,
            })
        except Exception as exc:
            # 不回退到 locator：创建 ElementHandle 的自动预览会触发现场已复现的 400。
            raise WorkflowExecutionError(f"页面探测失败（{op}）：{selector}") from exc

    async def _dom_box(
        self, selector: str, *, timeout: int = 0, index: int | None = None
    ) -> dict | None:
        deadline = time.monotonic() + max(timeout, 0) / 1000
        while True:
            box = await self._dom_probe("box", selector, index=index)
            if isinstance(box, dict) and box.get("width") and box.get("height"):
                return box
            if box is not None:
                raise WorkflowExecutionError(f"页面探测未返回有效坐标：{selector}")
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def _dom_visible(
        self, selector: str, *, timeout: int = 0, index: int | None = None
    ) -> bool:
        deadline = time.monotonic() + max(timeout, 0) / 1000
        while True:
            if await self._dom_probe("visible", selector, index=index):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def _dom_texts(self, selector: str, *, visible_only: bool = False) -> list[str]:
        texts = await self._dom_probe("visible_texts" if visible_only else "texts", selector)
        if not isinstance(texts, list):
            raise WorkflowExecutionError(f"页面探测未返回文字列表：{selector}")
        return [str(item) for item in texts]

    async def _dom_count(self, selector: str) -> int:
        count = await self._dom_probe("count", selector)
        if not isinstance(count, int):
            raise WorkflowExecutionError(f"页面探测未返回元素数量：{selector}")
        return count

    async def _dom_attr(self, selector: str, name: str) -> str | None:
        value = await self._dom_probe("attr", selector, name=name)
        if value is not None and not isinstance(value, str):
            raise WorkflowExecutionError(f"页面探测未返回属性：{selector}")
        return value

    async def _low_profile_click(
        self, selector: str, *, timeout: int = 3000, index: int | None = None
    ) -> bool:
        await self._bring_page_to_front(self.page)
        box = await self._dom_box(selector, timeout=timeout, index=index)
        if box is None:
            return False
        try:
            center = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await self.page.mouse.move(*center)
            self._pointer_pos = center
            await asyncio.sleep(self._pointer_settle_s)
            # hover、滚动或动画可能改变位置；旧坐标不可直接复用。
            box = await self._dom_box(selector, timeout=200, index=index)
            if box is None:
                return False
            await self._press_at(box)
        except Exception as exc:
            if isinstance(exc, WorkflowExecutionError):
                raise
            raise WorkflowExecutionError(f"坐标点击失败：{selector}") from exc
        return True

    async def _low_profile_fill(self, selector: str, value: str) -> bool:
        if not await self._low_profile_click(selector, timeout=3000):
            return False
        if not await self._dom_probe("editable", selector):
            raise WorkflowExecutionError(f"输入框不可编辑：{selector}")
        if not await self._dom_probe("focused", selector):
            raise WorkflowExecutionError(f"输入框未获得焦点：{selector}")
        keyboard = self.page.keyboard
        if await self._dom_probe("has_value", selector):
            await keyboard.press("ControlOrMeta+a")
            await keyboard.press("Delete")
        await keyboard.type(str(value), delay=random.randint(60, 140))
        if not await self._dom_probe("value_matches", selector, value=str(value)):
            raise WorkflowExecutionError(f"输入结果未确认：{selector}")
        return True

    def _login_gateway_notice(self) -> str:
        """本次点击后，登录接口若是瑞数空 HTML 400，返回暂停说明。

        Returns:
            str: 有网关拦截时给出说明。没有时为空字符串。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._login_gateway_notice()
            ''
        """
        for entry in self._page_errors[self._login_error_baseline:]:
            url = str(entry.get("url") or "").split("?", 1)[0]
            if "/sso-auth/login" not in url or not self._is_gateway_form(entry):
                continue
            return (
                "登录提交被网关拦截（HTTP 400 text/html），不是验证码或证书错误。"
                "不要刷新当前文档，不要重复点击立即登录。"
            )
        return ""

    def _fresh_login_verdict(self) -> dict[str, str] | None:
        """取本次点击之后到达的登录接口结论。

        Returns:
            dict | None: 最新一条。还没有时为 None。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._fresh_login_verdict() is None
            True
        """
        fresh = [
            item for item in self._login_submit_verdicts
            if float(item.get("at") or 0) >= self._login_watch_at
        ]
        if not fresh:
            return None
        return fresh[-1]

    def _login_landing_page(self):
        """找这次登录新开的工作台，或已经离开登录页的当前页。

        任务开始前就在的工作台不算这次登录成功。

        Returns:
            object | None: 可以绑定的页面。还没有时为 None。

        Examples:
            >>> from workbench_tabs import is_workbench_url
            >>> is_workbench_url("https://pm.gd.csg.cn/portal/#/powerEnterprise")
            True
        """
        from workbench_tabs import is_workbench_url

        before = getattr(self, "_login_pages_before", set())
        for page in self._iter_known_pages():
            if page in before or not self._page_is_open(page):
                continue
            if is_workbench_url(self._page_url(page)):
                return page
        origin = getattr(self, "_login_origin_page", None)
        url = self._page_url(origin)
        previous = getattr(self, "_login_url_before", "")
        # 只认这次点击的那一页离开登录页。新开的空白标签还不是工作台。
        if url and url != previous and not is_public_portal_url(url):
            return origin
        return None

    async def _adopt_login_workbench(self, page) -> None:
        """绑定并激活登录后的工作台，关掉不受保护的重复工作台。

        Args:
            page: 要绑定的工作台标签。

        Returns:
            None: 绑定完成后返回。

        Examples:
            >>> WorkflowRunner._adopt_login_workbench.__name__
            '_adopt_login_workbench'
        """
        from workbench_tabs import is_workbench_url

        self._bind_page(page)
        self._workbench_page = page
        await self._bring_page_to_front(page)
        session = getattr(self, "_shared_session", None)
        bind = getattr(session, "_bind_page", None) if session is not None else None
        if callable(bind) and getattr(page, "context", None) is not None:
            bind(page)
        for other in list(self._iter_known_pages()):
            if other is page or other in self._protected_pages or not self._page_is_open(other):
                continue
            if not is_workbench_url(self._page_url(other)):
                continue
            close = getattr(other, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning("  关闭重复工作台失败: {}", exc)
                continue
            self.pages = [item for item in self.pages if item is not other]
        self._bind_page(page)
        logger.info("  已绑定登录后的工作台: {}", self._page_url(page))

    async def _await_login_landing(self, step: dict) -> None:
        """等登录成功信号，而不是固定睡几秒再查一次。

        登录接口 ``POST /portal/xhr/sso-auth/login`` 返回 JSON ``code`` 为 0、
        新开 ``portal/#/powerEnterprise``，或当前页离开登录页，三者谁先出现算成功。
        接口明确失败就立刻说明原因。都没有时，到 ``login_wait_ms``（默认 30 秒）再暂停。

        Args:
            step: 点击步骤，可带 ``login_wait_ms``。

        Returns:
            None: 已经绑到工作台，或当前页已经离开登录页。

        Raises:
            WorkflowPaused: 接口报登录失败，或超时仍停在登录页。

        Examples:
            >>> login_wait_ms({})
            30000
        """
        timeout_ms = login_wait_ms(step, self.config)
        deadline = time.monotonic() + timeout_ms / 1000
        logger.info("  等待登录成功信号，最多 {}ms", timeout_ms)
        while True:
            await self._drain_response_captures()
            await self._drain_login_captures()
            verdict = self._fresh_login_verdict()
            if verdict and verdict.get("outcome") == "failed":
                reason = verdict.get("message") or "登录接口返回失败"
                await self._pause_login_error(f"登录未成功：{reason}")
                return
            if gateway := self._login_gateway_notice():
                await self._pause_login_error(gateway)
                return
            landing = self._login_landing_page()
            if landing is not None:
                await self._adopt_login_workbench(landing)
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.8)
        checked = self._page_url(self.page) or "未知页面"
        verdict = self._fresh_login_verdict()
        if verdict and verdict.get("outcome") == "success":
            await self._pause_login_error(
                f"登录接口已成功，但 {timeout_ms}ms 内没有打开工作台（检查页：{checked}）"
            )
            return
        await self._pause_login_error(
            f"登录提交后 {timeout_ms}ms 内没有进入工作台（检查页：{checked}）"
        )

    async def _do_click(self, step: dict) -> None:
        """点击元素。如果 expect=new_tab，等待新标签页打开。"""
        selector = step.get("selector", "")
        expect = step.get("expect", "")
        timeout = step.get("timeout", 10000)

        if not selector:
            logger.warning("  click 缺少 selector")
            return

        tabs_before = set(self._iter_known_pages())
        watch_login = (
            self.config.get("type") == "login" and "立即登录" in str(selector)
        )
        if watch_login:
            self._login_generation += 1
            self._login_watch_at = time.monotonic()
            self._login_submit_verdicts.clear()
            self._login_pages_before = set(self._iter_known_pages())
            self._login_url_before = self._page_url(self.page)
            self._login_origin_page = self.page
            self._login_error_baseline = len(self._page_errors)
            self._arm_login_response_listener(self.page)
        await self._trace_browser_action("click.before")
        try:
            try:
                clicked = await self._low_profile_click(selector, timeout=timeout)
                if not clicked and await self._adopt_tab_with_target(selector):
                    if watch_login:
                        self._arm_login_response_listener(self.page)
                    clicked = await self._low_profile_click(selector, timeout=timeout)
                if not clicked:
                    raise WorkflowExecutionError(self._click_miss_message(selector))
            except Exception as exc:
                if step.get("allow_empty") and step.get("no_data_selector"):
                    no_data_selector = str(step["no_data_selector"])
                    no_data_visible = await self._dom_visible(no_data_selector, timeout=300)
                    if no_data_visible:
                        reason = str(step.get("no_data_text") or no_data_selector)
                        raise WorkflowSkipRepeatRound(reason)
                if (
                    self.config.get("type") == "login"
                    and "登 录" in selector
                    and (
                        await self._login_stage_ok()
                        or await self._account_marker_visible()
                    )
                ):
                    self._skip_remaining_login_steps = True
                    logger.info("  登录按钮未出现，页面已有登录标记，跳过后续登录表单步骤")
                    return
                logger.warning("  点击失败: {}", selector)
                if isinstance(exc, WorkflowExecutionError):
                    raise
                raise WorkflowExecutionError(self._click_miss_message(selector)) from exc
            logger.info("  点击: {}", selector)
            opener = self.page
            await self._drop_duplicate_tabs_missing(selector)
            if watch_login:
                self._login_submit_seen = True
                await self._await_login_landing(step)
            await self._trace_browser_action("click.after")

            if expect == "new_tab":
                # 等待新标签页出现
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    candidates = [p for p in self.pages if p not in tabs_before
                                  and await p.opener() is opener]
                    if len(candidates) == 1:
                        popup = candidates[0]
                        self._current_page_idx = self.pages.index(popup)
                        if self._executing_step_index is not None:
                            self._tab_checkpoints[popup] = (self._executing_step_index, opener)
                        logger.info("  切换到新标签页 #{}", self._current_page_idx)
                        await self._bring_page_to_front(popup)
                        # 等待新页面加载。加载完再放到前台，让这一份文档在可见时完成自己的签名初始化。
                        try:
                            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                            await asyncio.sleep(2)
                        except Exception:
                            pass
                        await self._bring_page_to_front(self.page)
                        # 新文档要先跑完自己的初始化请求（签名/会话握手），再点业务菜单
                        await self._wait_network_quiet(idle_ms=step.get("settle_idle_ms", 700),
                                                       timeout=step.get("settle_timeout", 10000))
                        return
                await self._pause_and_wait("未检测到站点打开的业务标签页，请取消任务并检查门户入口或弹窗拦截设置")
                raise WorkflowPaused("未确认业务标签页，不继续执行")
            elif step.get("settle", False):
                await self._wait_network_quiet(idle_ms=step.get("settle_idle_ms", 500),
                                               timeout=step.get("settle_timeout", 8000))
        finally:
            if watch_login:
                await self._disarm_login_response_listener()

    async def _do_wait(self, step: dict) -> None:
        """等待选择器出现或等待指定毫秒。"""
        selector = step.get("selector", "")
        timeout = step.get("timeout", 10000)
        ms = step.get("ms", 0)

        if ms:
            logger.info("  等待 {}ms", ms)
            await asyncio.sleep(ms / 1000)
            return

        if selector:
            logger.info("  等待元素: {}", selector)
            no_data_selector = str(step.get("no_data_selector") or "").strip()
            no_data_text = str(step.get("no_data_text") or "").strip()
            if step.get("allow_empty") and no_data_selector:
                deadline = time.monotonic() + int(timeout) / 1000
                while time.monotonic() < deadline:
                    if await self._dom_visible(no_data_selector, timeout=100):
                        raise WorkflowSkipRepeatRound(no_data_text or no_data_selector)
                    if await self._dom_visible(selector, timeout=100):
                        return
                    await asyncio.sleep(0.1)
                if await self._dom_visible(no_data_selector, timeout=200):
                    raise WorkflowSkipRepeatRound(no_data_text or no_data_selector)
            try:
                if not await self._dom_visible(selector, timeout=timeout):
                    raise WorkflowExecutionError(f"等待元素超时：{selector}")
            except Exception as exc:
                raise WorkflowExecutionError(f"等待元素失败：{selector}") from exc

    @staticmethod
    def _coerce_target_date(value: str | date | None) -> date | None:
        if value is None or value == "":
            return None
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    def _settlement_month_bounds(self) -> tuple[date, date]:
        """补数据指定了月份时，查的就是这个月。否则仍按目标日的上月。"""
        if self.query_month is not None:
            first = self.query_month.replace(day=1)
            if first.month == 12:
                last = date(first.year + 1, 1, 1) - timedelta(days=1)
            else:
                last = date(first.year, first.month + 1, 1) - timedelta(days=1)
            return first, last
        base = self.target_date or date.today()
        last = base.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last

    def _parse_date_value(self, value: str) -> date:
        base = self.target_date or date.today()
        if value in ("previous_month_start", "previous_month_end"):
            first, last = self._settlement_month_bounds()
            return first if value.endswith("start") else last
        if value in ("{target}", "target", "today"):
            return base
        if value == "yesterday":
            return base - timedelta(days=1)
        if re.match(r"^-\d+d$", value):
            return base - timedelta(days=int(value[1:-1]))
        return date.fromisoformat(value)

    @staticmethod
    def _month_text_candidates(month: int) -> list[str]:
        names = {
            1: "一月", 2: "二月", 3: "三月", 4: "四月",
            5: "五月", 6: "六月", 7: "七月", 8: "八月",
            9: "九月", 10: "十月", 11: "十一月", 12: "十二月",
        }
        return [
            names[month],
            f"{month}月",
            f"{month} 月",
            f"{month:02d}月",
            f"{month:02d} 月",
        ]

    async def _click_if_visible(self, selectors: list[str], timeout: int = 500) -> bool:
        for selector in selectors:
            if not await self._dom_visible(selector, timeout=timeout):
                continue
            await self._trace_browser_action("pick_date.before_calendar_click")
            if await self._low_profile_click(selector, timeout=timeout):
                await self._trace_browser_action("pick_date.after_calendar_click")
                return True
        return False

    async def _read_element_picker_header(self) -> tuple[int, int] | None:
        """读取 Element UI / Element Plus 日期面板头部年月。"""
        labels = await self._dom_texts(".el-date-picker__header-label", visible_only=True)
        if not labels:
            return None
        cleaned = [re.sub(r"\s+", "", item or "") for item in labels if (item or "").strip()]
        if not cleaned:
            return None
        combined = "".join(cleaned)

        year_match = re.search(r"(\d{4})年", combined)
        if not year_match:
            year_match = re.search(r"(\d{4})", combined)
        if not year_match:
            return None

        month: int | None = None
        numeric_month = re.search(r"(\d{1,2})月", combined)
        if numeric_month:
            month = int(numeric_month.group(1))
        else:
            month_names = {
                "十一月": 11, "十二月": 12, "一月": 1, "二月": 2,
                "三月": 3, "四月": 4, "五月": 5, "六月": 6,
                "七月": 7, "八月": 8, "九月": 9, "十月": 10,
            }
            for name, number in month_names.items():
                if name in combined:
                    month = number
                    break
        if not month:
            return None
        return int(year_match.group(1)), month

    async def _open_element_year_panel(self) -> bool:
        if await self._dom_visible(".el-year-table .cell", timeout=100):
            return True
        opened = await self._click_if_visible(
            [
                '.el-date-picker__header-label:has-text("年")',
                ".el-date-picker__header-label",
            ],
            timeout=500,
        )
        if opened:
            await asyncio.sleep(0.2)
        return opened

    async def _open_element_month_panel(self) -> bool:
        for selector in (".el-month-table .cell", ".el-month-table td"):
            if await self._dom_visible(selector, timeout=100):
                return True
        # 头部有两个标签时，第二个是月份。没文字特征可用，只能按位置取。
        label = ".el-date-picker__header-label"
        if (
            await self._dom_count(label) > 1
            and await self._dom_visible(label, timeout=500, index=1)
            and await self._low_profile_click(label, timeout=500, index=1)
        ):
            await asyncio.sleep(0.2)
            return True
        opened = await self._click_if_visible(
            ['.el-date-picker__header-label:has-text("月")'],
            timeout=500,
        )
        if opened:
            await asyncio.sleep(0.2)
        return opened

    async def _click_element_year(self, year: int) -> bool:
        year_selector = f'.el-year-table .cell:text-is("{year}")'
        for _ in range(20):
            if await self._click_if_visible([year_selector], timeout=300):
                await asyncio.sleep(0.2)
                return True
            texts = await self._dom_texts(".el-year-table .cell")
            years = sorted(int(t.strip()) for t in texts if re.match(r"^\s*\d{4}\s*$", t or ""))
            if not years:
                return False
            if year < years[0]:
                moved = await self._click_if_visible(
                    [
                        ".el-date-picker__prev-btn.el-icon-d-arrow-left",
                        ".el-picker-panel__icon-btn.el-icon-d-arrow-left",
                        ".el-icon-d-arrow-left",
                    ],
                    timeout=300,
                )
            elif year > years[-1]:
                moved = await self._click_if_visible(
                    [
                        ".el-date-picker__next-btn.el-icon-d-arrow-right",
                        ".el-picker-panel__icon-btn.el-icon-d-arrow-right",
                        ".el-icon-d-arrow-right",
                    ],
                    timeout=300,
                )
            else:
                return False
            if not moved:
                return False
            await asyncio.sleep(0.2)
        return False

    async def _click_element_month(self, month: int) -> bool:
        selectors = [f'.el-month-table .cell:text-is("{text}")' for text in self._month_text_candidates(month)]
        if await self._click_if_visible(selectors, timeout=500):
            await asyncio.sleep(0.2)
            return True
        return False

    async def _click_date_title(self, date_str: str) -> bool:
        cell_selector = f'td[title="{date_str}"]'
        if not await self._dom_visible(cell_selector, timeout=1000):
            return False
        await self._trace_browser_action("pick_date.before_calendar_click")
        if not await self._low_profile_click(cell_selector, timeout=1000):
            return False
        await self._trace_browser_action("pick_date.after_calendar_click")
        logger.info("  点击日历日期(title): {}", date_str)
        await asyncio.sleep(0.5)
        return True

    async def _click_element_day(self, target: date) -> bool:
        day_num = str(target.day)
        selectors = [
            f'td.available:not(.prev-month):not(.next-month) span:text-is("{day_num}")',
            f'td.available:not(.prev-month):not(.next-month) .el-date-table-cell__text:text-is("{day_num}")',
            f'td.available:not(.prev-month):not(.next-month) .cell:text-is("{day_num}")',
            f'td.available span:text-is("{day_num}")',
            f'td.available .el-date-table-cell__text:text-is("{day_num}")',
        ]
        if await self._click_if_visible(selectors, timeout=1000):
            logger.info("  点击日历日期(span): {}", day_num)
            await asyncio.sleep(0.5)
            return True
        return False

    async def _select_element_date_by_year_month(self, target: date) -> bool:
        """优先按 Element UI/Plus 的 年份→月份→日期 面板选择日期。"""
        header = await self._read_element_picker_header()
        if not header:
            return False
        cur_year, cur_month = header

        if cur_year != target.year:
            if not await self._open_element_year_panel():
                return False
            if not await self._click_element_year(target.year):
                return False

        if cur_year != target.year or cur_month != target.month:
            month_visible = await self._dom_visible(".el-month-table .cell", timeout=200)
            if not month_visible and not await self._open_element_month_panel():
                return False
            if not await self._click_element_month(target.month):
                return False

        if await self._click_element_day(target):
            logger.info("  Element 日期面板选择: {}-{:02d}-{:02d}", target.year, target.month, target.day)
            return True
        return False

    async def _navigate_element_month_delta(self, target: date) -> bool:
        header = await self._read_element_picker_header()
        if not header:
            return False
        cur_year, cur_month = header
        month_diff = (target.year - cur_year) * 12 + (target.month - cur_month)
        if month_diff:
            arrow_sel = ".el-icon-arrow-left" if month_diff < 0 else ".el-icon-arrow-right"
            for _ in range(abs(month_diff)):
                if not await self._click_if_visible([arrow_sel], timeout=500):
                    return False
                await asyncio.sleep(0.12)
            logger.info("  日历月份导航: {}-{} → {}-{}", cur_year, cur_month, target.year, target.month)
        return await self._click_element_day(target)

    async def _do_pick_date(self, step: dict) -> None:
        """选择日期，并在选完后等页面把这次切换触发的查询跑完。

        现场证据：日期变更会触发业务重查（getJddjTree / getFirstAndLastTen）。
        如果紧接着点导出，就会在数据还没回来、甚至请求已被网关拒绝时继续操作。
        """
        await self._trace_browser_action("pick_date.before")
        try:
            await self._apply_date_pick(step)
            if step.get("verify_value"):
                selector = step.get("selector", ".el-date-picker input")
                target = self._parse_date_value(step.get("value", "yesterday"))
                expected = target.strftime("%Y-%m") if step.get("month_only") else target.isoformat()
                actual = await self._dom_probe("value", selector)
                if actual != expected:
                    raise WorkflowExecutionError(
                        f"日期选择未生效：期望 {expected}，输入框实际为 {actual!r}（{selector}）"
                    )
        finally:
            await self._trace_browser_action("pick_date.after_selection")
            await self._wait_network_quiet(
                idle_ms=step.get("settle_idle_ms", 800),
                timeout=step.get("settle_timeout", 15000),
            )
            await self._trace_browser_action("pick_date.after_settle")

    async def _apply_date_pick(self, step: dict) -> None:
        """选择日期。

        支持 value: today / yesterday / 2026-06-25 / -1d / -2d
        
        适配 Element UI (el-date-picker) 和 Ant Design (ant-picker) 两种日期选择器。
        策略：点开输入框 → 快捷按钮 → Ant title → Element 年/月/日 → 月份箭头 → fill+Enter
        """
        selector = step.get("selector", ".el-date-picker input")
        value = step.get("value", "yesterday")

        target = self._parse_date_value(value)
        if step.get("month_only"):
            await self._apply_month_pick(selector, target)
            return
        date_str = target.strftime("%Y-%m-%d")
        logger.info("  选择日期: {} → {}", value, date_str)

        # 点击日期输入框，打开日历面板
        await self._trace_browser_action("pick_date.before_open")
        if not await self._low_profile_click(selector, timeout=5000):
            raise WorkflowExecutionError(self._click_miss_message(selector))
        await self._trace_browser_action("pick_date.after_open")
        await asyncio.sleep(0.5)

        # 方式1：快捷按钮（今天 / 昨天）
        shortcut_map = {"today": "今天", "yesterday": "昨天"}
        shortcut_text = shortcut_map.get(value)
        if shortcut_text:
            shortcut = (
                f'.el-picker-panel__shortcut:has-text("{shortcut_text}"), '
                f'button:has-text("{shortcut_text}")'
            )
            if await self._dom_visible(shortcut, timeout=1000) and await self._low_profile_click(
                shortcut, timeout=1000
            ):
                logger.info("  快捷按钮: {}", shortcut_text)
                await asyncio.sleep(0.5)
                return
            logger.debug("  快捷按钮不可用，尝试日历格子")

        # 方式2：td[title="YYYY-MM-DD"]（Ant Design / 部分 Element UI）
        if await self._click_date_title(date_str):
            return

        # 方式3：Element UI / Element Plus：年份 → 月份 → 日期
        try:
            if await self._select_element_date_by_year_month(target):
                return
        except WorkflowExecutionError:
            raise
        except Exception:
            logger.debug("  Element 年月面板选择失败，尝试月份箭头")

        # 方式4：月份箭头导航兜底
        try:
            if await self._navigate_element_month_delta(target):
                return
        except WorkflowExecutionError:
            raise
        except Exception:
            logger.debug("  月份箭头导航失败，继续尝试日期格子")

        # 方式5：当前面板日期格子兜底
        try:
            if await self._click_element_day(target):
                return
        except WorkflowExecutionError:
            raise
        except Exception:
            logger.debug("  Element UI span 匹配失败，尝试 fill+Enter")

        # 方式6：逐键输入 + Enter（Element UI 的备选方案，readonly 输入框可能不生效）
        try:
            if await self._low_profile_fill(selector, date_str):
                keyboard = getattr(self.page, "keyboard", None)
                if keyboard is not None:
                    await keyboard.press("Enter")
                else:
                    raise WorkflowExecutionError("键盘输入不可用，日期选择已停止")
                await asyncio.sleep(1)
                return
            raise WorkflowExecutionError(f"日期输入框不可见：{selector}")
        except Exception as exc:
            raise WorkflowExecutionError(f"日期选择失败：{date_str}") from exc

    async def _apply_month_pick(self, selector: str, target: date) -> None:
        """Element UI 的 type=month 面板：点击月份即完成选择。"""
        if not await self._low_profile_click(selector, timeout=5000):
            raise WorkflowExecutionError(self._click_miss_message(selector))
        await asyncio.sleep(0.5)
        labels = await self._dom_texts(".el-date-picker__header-label", visible_only=True)
        year_text = " ".join(labels)
        match = re.search(r"\d{4}", year_text)
        if not match:
            raise WorkflowExecutionError(f"月份面板未显示年份：{selector}")
        if int(match.group()) != target.year:
            if not await self._open_element_year_panel() or not await self._click_element_year(target.year):
                raise WorkflowExecutionError(f"月份选择失败：无法定位 {target.year} 年")
        if not await self._dom_visible(".el-month-table .cell", timeout=1000):
            raise WorkflowExecutionError("月份面板未显示可选月份")
        if not await self._click_element_month(target.month):
            raise WorkflowExecutionError(f"月份选择失败：无法定位 {target.month} 月")

    async def _do_reset_site_storage(self, step: dict) -> None:
        """清掉当前站点的本地状态，回到“干净首次访问”的现场。

        现场手工恢复步骤就是这个：清 localStorage / sessionStorage / cookie，
        刷新后白屏或半登录状态消失，可以重新登录。登录流程开头执行它，
        可以把历史上残留的 pfxh-userInfo / 旧 token 一次清干净。
        """
        page = self.page
        origin = step.get("origin") or ""
        if not origin:
            from urllib.parse import urlsplit

            parts = urlsplit(page.url)
            origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme.startswith("http") else ""
        if not origin:
            logger.warning("  reset_site_storage 无法确定站点 origin: {}", page.url)
            return

        default_types = ["local_storage", "session_storage", "cookies"]
        storage_types = step.get("types") or default_types
        if isinstance(storage_types, str):
            storage_types = [item.strip() for item in storage_types.split(",") if item.strip()]
        keep = set(step.get("keep_keys") or [])

        cleared_keys: list[str] = []
        if "local_storage" in storage_types or "session_storage" in storage_types:
            for scope in ("localStorage", "sessionStorage"):
                if (scope == "localStorage" and "local_storage" not in storage_types) or (
                    scope == "sessionStorage" and "session_storage" not in storage_types
                ):
                    continue
                try:
                    keys = await page.evaluate(
                        f"() => {{ const out = []; for (let i = 0; i < {scope}.length; i += 1) "
                        f"out.push({scope}.key(i)); return out; }}"
                    )
                except Exception:
                    keys = []
                for key in keys or []:
                    if key in keep:
                        continue
                    try:
                        await page.evaluate(f"key => {scope}.removeItem(key)", key)
                        cleared_keys.append(f"{scope}.{key}")
                    except Exception:
                        pass

        used_cdp = False
        cookie_error = ""
        if "cookies" in storage_types:
            # httpOnly 的会话 cookie 用 document.cookie 删不掉，走 CDP 的按站点清理。
            # CDP 只认固定的 storageTypes 名字；带 keep_keys 时不交给 CDP 清本地存储，
            # 否则要保留的键会被一并抹掉。
            cdp_types = [
                item for item in storage_types
                if item in {
                    "all", "appcache", "cookies", "file_systems", "indexeddb",
                    "local_storage", "shader_cache", "websql", "service_workers",
                    "cache_storage",
                }
            ]
            if keep:
                cdp_types = [item for item in cdp_types if item not in ("local_storage", "all")]
            if cdp_types:
                try:
                    cdp = await self.context.new_cdp_session(page)
                    await cdp.send("Storage.clearDataForOrigin", {
                        "origin": origin,
                        "storageTypes": ",".join(cdp_types),
                    })
                    await cdp.detach()
                    used_cdp = True
                except Exception as exc:
                    cookie_error = str(exc)
                    logger.warning("  CDP 清理 httpOnly cookie 失败: {}", exc)

        if "cookies" in storage_types and not used_cdp:
            message = (
                f"CDP 清理 httpOnly cookie 失败：{cookie_error or 'CDP 不可用'}。"
                "已暂停。不会改用清空全部 localStorage/sessionStorage 作为兜底。"
            )
            while True:
                await self._pause_and_wait(message)

        logger.info(
            "  已清理站点存储 {}（{}；{}）",
            origin, ",".join(storage_types), "CDP" if used_cdp else "JS",
        )
        if step.get("reload", True):
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await self._wait_network_quiet(idle_ms=step.get("settle_idle_ms", 600),
                                           timeout=step.get("settle_timeout", 10000))
        self._emit("storage_reset", origin=origin, types=storage_types,
                   keys=cleared_keys[:20], reload=bool(step.get("reload", True)))

    def _request_owner_page(self, request):
        """请求是从哪个页面发出的。

        Args:
            request: Playwright 请求，或测试用的同等对象。

        Returns:
            Any: 所属页面。没有 frame、页面已分离时为 None。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._request_owner_page(object()) is None
            True
        """
        try:
            frame = getattr(request, "frame", None)
            if frame is None:
                return None
            return getattr(frame, "page", None)
        except Exception:
            return None

    def _inflight_query_requests(self) -> list:
        """当前绑定页上仍在途的 xhr/fetch。

        别的标签即使同属 pm.gd.csg.cn（门户、现货、结算）也不算这笔导出的查询。
        静态资源不算。所属页面已关闭的请求从在途集合里去掉。

        Returns:
            list: 会拦住当前页导出的请求。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._inflight_query_requests()
            []
        """
        page = self.page
        page_url = self._page_url(page) if page else ""
        found = []
        for request in list(self._inflight):
            try:
                if getattr(request, "resource_type", "") not in ("xhr", "fetch"):
                    continue
                owner = self._request_owner_page(request)
                if owner is not None and (owner is not page or not self._page_is_open(owner)):
                    if not self._page_is_open(owner):
                        self._inflight.discard(request)
                    continue
                url = getattr(request, "url", "") or ""
                if page_url and not self._same_origin(url, page_url):
                    continue
                path = url.split("?", 1)[0].lower()
                if path.endswith(self._STATIC_SUFFIXES):
                    continue
                found.append(request)
            except Exception:
                continue
        return found

    def _export_blocking_errors(self) -> list[dict]:
        """只留下当前绑定页上还没确认的请求错误。

        Returns:
            list[dict]: 会拦住这笔导出的错误。其他标签的 HTTP 0 或 4xx 不算。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._export_blocking_errors()
            []
        """
        page = self.page
        blocking = []
        for item in self._unacknowledged_page_errors():
            owner = item.get("_page")
            if owner is not None and page is not None and owner is not page:
                continue
            blocking.append(item)
        return blocking

    @staticmethod
    def _describe_inflight(requests: list) -> str:
        """把仍在途的查询数量和地址写成暂停原因。

        Args:
            requests (list): 当前页在途的 xhr/fetch。

        Returns:
            str: 含数量和去查询串后的地址。没有请求时为空字符串。

        Examples:
            >>> class _Req:
            ...     url = "https://pm.gd.csg.cn/pfjs/export?day=1"
            >>> "export" in WorkflowRunner._describe_inflight([_Req()])
            True
        """
        if not requests:
            return ""
        urls = []
        for request in requests[:4]:
            raw = str(getattr(request, "url", "") or "").split("?", 1)[0]
            if raw:
                urls.append(raw[:140])
        text = f"仍有 {len(requests)} 个当前页的同源查询请求在途"
        if urls:
            text += "：" + "；".join(urls)
        return text

    async def _pause_until_export_ready(
        self, *, timeout: int = 60000, settle_ms: int = 1200
    ) -> None:
        """导出前等待当前页查询完成，再给页面一个短暂稳定时间。

        其他标签的心跳、长轮询和失败请求不拦截这笔导出。
        查询成功后自动等待 ``settle_ms``；只有请求失败或超过 ``timeout``
        才暂停人工处理。这样慢网络会多等一会儿，不会直接把任务卡成暂停。

        Returns:
            None: 当前页可以导出时返回。

        Raises:
            WorkflowPaused: 用户取消，或没有继续回调时。

        Examples:
            >>> WorkflowRunner._describe_inflight([])
            ''
        """
        deadline = time.monotonic() + max(int(timeout), 0) / 1000
        settle_s = max(int(settle_ms), 0) / 1000
        while True:
            await self._drain_response_captures()
            gateway = self._export_blocking_errors()
            inflight = self._inflight_query_requests()
            if not gateway and not inflight:
                if settle_s:
                    await asyncio.sleep(settle_s)
                    await self._drain_response_captures()
                    if self._export_blocking_errors() or self._inflight_query_requests():
                        continue
                return
            parts = []
            if gateway:
                parts.append(
                    "仍有未处理的请求异常："
                    + "；".join(self._describe_page_errors(gateway))[:300]
                )
            inflight_text = self._describe_inflight(inflight)
            if inflight_text:
                parts.append(inflight_text)
            if not gateway and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                continue
            if not gateway:
                parts.insert(0, f"等待查询完成超过 {int(timeout)}ms")
            await self._pause_and_wait(
                "导出前暂停。" + " ".join(parts) + "。不会在请求未确认时导出。"
            )

    async def _do_download_notice_reports(self, step: dict) -> None:
        """按目标日期下载“资讯公告 → 市场运行报告”的附件。

        列表走门户自己的 /sso-portal/portal/notice/list 接口，详情页点附件下载。
        目标日期来自任务窗口；任务按天执行时会逐日调用本动作。
        """
        from spider_gd.core.report_dates import select_report_notices

        target = self.target_date or date.today()
        notice_type = int(step.get("notice_type_id", 57))
        broad = str(step.get("broad_heading", "资讯公告"))
        page_size = max(1, min(int(step.get("page_size", 50)), 100))
        max_pages = max(1, min(int(step.get("max_pages", 10)), 100))
        api_path = str(step.get(
            "list_api",
            "/portal/xhr/sso-portal/portal/notice/list",
        ))
        timeout = int(step.get("timeout", 30000))
        base = Path(self._active_output_dir) if self._active_output_dir else self.download_dir
        base.mkdir(parents=True, exist_ok=True)

        notices = await self._fetch_notice_reports(
            api_path, broad, notice_type, page_size, max_pages, target, timeout,
        )
        if not notices:
            raise WorkflowExecutionError(
                "市场运行报告列表为空：请检查登录态、列表接口路径和参数"
            )
        matched = select_report_notices(notices, target)
        self._emit(
            "report_scan",
            date=target.isoformat(),
            total=len(notices),
            matched=len(matched),
        )
        if not matched:
            logger.warning("  运行日报 {} 没有找到匹配公告（扫描 {} 条）", target, len(notices))
            self._note_file("empty", reason=f"没有找到 {target.isoformat()} 的报告", when=target)
            self._emit("report_not_found", date=target.isoformat(), indicator=self._indicator_id())
            return

        for notice in matched:
            await self._download_notice_attachments(notice, notice_type, base, timeout)

    async def _fetch_notice_reports(
        self,
        api_path: str,
        broad: str,
        notice_type: int,
        page_size: int,
        max_pages: int,
        target: date,
        timeout: int,
    ) -> list[dict]:
        origin = await self.page.evaluate("() => location.origin")
        url = api_path if api_path.startswith("http") else f"{origin}{api_path}"
        js = """
        async ({url, payload}) => {
            try {
                const host = location.hostname;
                const token = sessionStorage.getItem(host + '_temp')
                    || sessionStorage.getItem(host)
                    || localStorage.getItem(host)
                    || '';
                const headers = {
                    'Content-Type': 'application/json; charset=utf-8',
                    'Accept': 'application/json',
                    'Front-Path': location.pathname,
                };
                if (token) headers['SSO-Auth-Token'] = token;
                const response = await fetch(url, {
                    method: 'POST',
                    headers,
                    credentials: 'include',
                    body: JSON.stringify(payload),
                });
                return {status: response.status, text: await response.text()};
            } catch (error) {
                return {status: 0, text: String(error)};
            }
        }
        """
        notices: list[dict] = []
        for page_num in range(1, max_pages + 1):
            payload = {
                "broadHeading": broad,
                "noticeTypeId": notice_type,
                "pageNum": page_num,
                "pageSize": page_size,
                "flag": 1,
            }
            try:
                result = await asyncio.wait_for(
                    self.page.evaluate(js, {"url": url, "payload": payload}),
                    timeout=timeout / 1000,
                )
            except Exception as exc:
                raise WorkflowExecutionError(f"读取市场运行报告列表失败: {exc}") from exc
            status = int((result or {}).get("status") or 0)
            text = str((result or {}).get("text") or "")
            if status != 200:
                raise WorkflowExecutionError(
                    f"读取市场运行报告列表失败: HTTP {status}，{text[:300]}"
                )
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise WorkflowExecutionError(
                    f"市场运行报告列表返回的不是 JSON: {text[:300]}"
                ) from exc
            if data.get("code") != 200:
                raise WorkflowExecutionError(
                    f"市场运行报告列表失败: {data.get('msg') or data}"
                )
            page_data = data.get("data") or {}
            items = []
            for item in page_data.get("list") or []:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("noticeTypeId")
                if item_type is not None and int(item_type) != notice_type:
                    continue
                items.append(item)
            notices.extend(items)
            pages = int(page_data.get("pages") or 1)
            logger.info("  运行日报列表第 {}/{} 页，{} 条", page_num, pages, len(items))
            if page_num >= pages:
                break
            # 列表按发布时间倒序；翻过目标日期较远后就不用再翻。
            unpinned = [item for item in items if not item.get("isTop")]
            times = [
                str(item.get("publishTime") or "")[:10]
                for item in unpinned
                if item.get("publishTime")
            ]
            if page_num >= 2 and times:
                try:
                    oldest = min(date.fromisoformat(value) for value in times if value)
                except ValueError:
                    oldest = None
                if oldest and oldest < target - timedelta(days=31):
                    break
        return notices

    async def _download_notice_attachments(
        self,
        notice: dict,
        notice_type: int,
        base: Path,
        timeout: int,
    ) -> None:
        from urllib.parse import quote

        notice_id = notice.get("noticeId")
        title = str(notice.get("noticeTitle") or "")
        if notice_id is None:
            raise WorkflowExecutionError(f"运行日报缺少 noticeId: {title}")
        origin = await self.page.evaluate("() => location.origin")
        detail_url = (
            f"{origin}/portal/#/home/informationNotice/detail"
            f"?id={quote(str(notice_id))}&noticeTypeId={notice_type}"
        )
        logger.info("  打开运行日报详情：{}", title)
        await self.page.goto(detail_url, wait_until="domcontentloaded")
        try:
            await self.page.wait_for_selector(
                ".attachments, button:has-text('下 载')",
                timeout=timeout,
            )
        except Exception as exc:
            raise WorkflowExecutionError(
                f"运行日报详情页没有出现下载按钮：{title}"
            ) from exc
        await asyncio.sleep(0.8)

        saved = await self._save_attachment_buttons(
            base, timeout, ".attachments button", title=title, notice_id=notice_id,
        )
        if saved == 0:
            raise WorkflowExecutionError(f"运行日报没有下载到附件：{title}")

    async def _save_attachment_buttons(
        self,
        base: Path,
        timeout: int,
        selector: str = ".attachments button",
        *,
        title: str = "",
        notice_id: int | None = None,
    ) -> int:
        """逐个点击详情页附件按钮并保存；返回成功保存的数量。"""
        saved = 0
        index = 0
        while index < 20:
            buttons = self.page.locator(selector)
            count = await buttons.count()
            if count == 0 and selector != "button:has-text('下 载')":
                buttons = self.page.locator("button:has-text('下 载')")
                count = await buttons.count()
            if index >= count:
                break
            button = buttons.nth(index)
            try:
                async with self.page.expect_download(timeout=timeout) as download_info:
                    await button.click()
                download = await download_info.value
            except Exception as exc:
                logger.warning("  运行日报附件 {} 下载失败: {}", index + 1, exc)
                index += 1
                continue
            fallback = f"运行日报_{notice_id or index + 1}_{index + 1}.pdf"
            suggested = download.suggested_filename or fallback
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", Path(suggested).name).strip()
            safe_name = safe_name or fallback
            save_path = base / safe_name
            tmp_path = base / f".{safe_name}.{time.time_ns()}.tmp"
            await download.save_as(str(tmp_path))
            result = save_versioned_file(tmp_path, save_path)
            saved += 1
            self._emit(
                "report_saved",
                path=str(save_path),
                title=title,
                notice_id=notice_id,
            )
            self._note_file("saved", save_path)
            if result.reused_existing:
                logger.success("  运行日报附件未变化 → {}", save_path)
            else:
                logger.success("  运行日报附件已下载 → {}", save_path)
            index += 1
        return saved

    def _export_error_on_page(self, item: dict) -> bool:
        """这条请求错误是不是当前绑定页上的。

        Args:
            item: ``_page_errors`` 中的一条。

        Returns:
            bool: 没有页面归属，或归属就是当前页时为 True。其他标签的错误为 False。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._export_error_on_page({"status": 0})
            True
        """
        owner = item.get("_page")
        page = self.page
        return not (owner is not None and page is not None and owner is not page)

    async def _read_export_notice(self) -> str:
        """读取导出后弹出的错误框或提示文字。

        Returns:
            str: 合并后的可见文字。页面读不到时为空字符串。

        Examples:
            >>> "暂无数据" in "查询 暂无数据"
            True
        """
        if self.page is None:
            return ""
        texts: list[str] = []
        for selector in _EXPORT_NOTICE_SELECTORS:
            try:
                found = await self._dom_texts(selector, visible_only=True)
            except Exception:
                continue
            for item in found:
                cleaned = re.sub(r"\s+", " ", str(item or "")).strip()
                if cleaned and cleaned not in texts:
                    texts.append(cleaned)
        return "；".join(texts)[:300]

    async def _export_diagnosis(
        self, baseline: int, timeout_error: str = "",
    ) -> dict[str, str] | None:
        """看点击之后的新错误和弹窗，决定这次导出算什么。

        Args:
            baseline: 点击前 ``_page_errors`` 的长度。
            timeout_error: 下载等待已经结束时的原始异常。还在等时留空。

        Returns:
            dict | None: :func:`diagnose_export_miss` 的结果。

        Examples:
            >>> diagnose_export_miss([], "", timeout_error="") is None
            True
        """
        await self._drain_response_captures()
        errors = [
            item for item in self._page_errors[baseline:]
            if not item.get("_resolved") and self._export_error_on_page(item)
        ]
        dialog = await self._read_export_notice()
        return diagnose_export_miss(errors, dialog, timeout_error=timeout_error)

    async def _export_download(self, selector: str, timeout: int, baseline: int) -> tuple[str, Any]:
        """点击导出并等待下载；出现弹窗或请求失败就提前返回。

        Args:
            selector: 导出按钮选择器。
            timeout: 等待下载的毫秒数。
            baseline: 点击前的错误条数，只把之后的新错误算进这次导出。

        Returns:
            tuple[str, Any]: ``("download", download)`` 或 ``("miss", diagnosis)``。

        Raises:
            WorkflowExecutionError: 导出按钮不存在，或点击过程中页面探测失败。

        Examples:
            >>> diagnose_export_miss([], "暂无数据")["action"]
            'skip'
        """
        timeout_ms = max(int(timeout), 1)
        context = self.page.expect_download(timeout=timeout_ms)
        try:
            download_info = await context.__aenter__()
        except Exception as exc:
            diagnosis = await self._export_diagnosis(baseline, timeout_error=str(exc))
            if diagnosis is None:
                diagnosis = {
                    "action": "fail",
                    "reason": f"导出失败: 等待下载时出错。原始错误：{exc}",
                }
            return ("miss", diagnosis)

        closed = False

        async def _close(exc_type=None, exc=None, tb=None) -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            with contextlib.suppress(Exception):
                await context.__aexit__(exc_type, exc, tb)

        try:
            try:
                clicked = await self._low_profile_click(selector, timeout=min(timeout_ms, 10000))
            except WorkflowExecutionError:
                await _close(WorkflowExecutionError, WorkflowExecutionError("点击未完成"), None)
                raise
            except Exception as exc:
                await _close(type(exc), exc, exc.__traceback__)
                raise WorkflowExecutionError(self._click_miss_message(selector)) from exc
            if not clicked:
                miss = WorkflowExecutionError(self._click_miss_message(selector))
                await _close(WorkflowExecutionError, miss, None)
                raise miss

            async def _wait_value():
                return await download_info.value

            waiter = asyncio.create_task(_wait_value())
            diagnosis: dict[str, str] | None = None
            try:
                deadline = time.monotonic() + timeout_ms / 1000
                while True:
                    diagnosis = await self._export_diagnosis(baseline)
                    if diagnosis is not None:
                        # 浏览器把成功的下载也记成 HTTP 0。给下载事件留一点时间，
                        # 避免把已经开始的文件当成失败。
                        if diagnosis.get("action") == "fail" and not waiter.done():
                            await asyncio.wait({waiter}, timeout=1.0)
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    done, _pending = await asyncio.wait({waiter}, timeout=min(0.3, remaining))
                    if waiter in done:
                        break
                if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                    download = waiter.result()
                    self._acknowledge_page_errors([
                        item for item in self._page_errors[baseline:]
                        if self._export_error_on_page(item)
                        and str(item.get("failure") or "").find("ERR_ABORTED") >= 0
                    ])
                    await _close()
                    return ("download", download)
                if diagnosis is None and waiter.done() and not waiter.cancelled():
                    caught = waiter.exception()
                    if caught is not None:
                        diagnosis = await self._export_diagnosis(
                            baseline, timeout_error=str(caught),
                        )
                if diagnosis is None:
                    diagnosis = await self._export_diagnosis(
                        baseline,
                        timeout_error=(
                            f'Timeout {timeout_ms}ms exceeded while waiting for event "download"'
                        ),
                    )
                if diagnosis is None:
                    diagnosis = {
                        "action": "fail",
                        "reason": "导出失败: 等待下载超时，没有捕获到文件。",
                    }
                return ("miss", diagnosis)
            finally:
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await waiter
        finally:
            await _close(asyncio.CancelledError, asyncio.CancelledError(), None)

    async def _settle_export_miss(
        self, step: dict, diagnosis: dict[str, str], baseline: int,
    ) -> None:
        """按诊断结果结束这次没拿到文件的导出。

        Args:
            step: 导出步骤，用来读 ``save_as`` 和 API 兜底。
            diagnosis: :func:`diagnose_export_miss` 的结果。
            baseline: 点击前的错误条数。被本次诊断消费的错误会标成已确认，
                避免下一天的导出闸门再被它们拦住。

        Returns:
            None: API 兜底成功时返回。

        Raises:
            WorkflowSkipRepeatRound: 页面说明该日没有可导出的数据。
            WorkflowPaused: 文档签名被网关拒绝。
            WorkflowDateExportFailed: 这一天导出失败，日期循环可以继续。

        Examples:
            >>> diagnose_export_miss([], "无数据")["action"]
            'skip'
        """
        action = diagnosis.get("action") or "fail"
        reason = str(diagnosis.get("reason") or "导出失败")
        fresh = [
            item for item in self._page_errors[baseline:]
            if self._export_error_on_page(item)
        ]
        logger.error("  {}", reason)
        if action == "skip":
            self._acknowledge_page_errors(fresh)
            self._note_file("empty", reason=reason)
            raise WorkflowSkipRepeatRound(reason)
        if action == "gateway":
            if self.page is not None:
                self._poisoned_pages.add(self.page)
            await self._pause_and_wait(reason)
            self._acknowledge_page_errors(fresh)
            raise WorkflowPaused(reason)
        self._acknowledge_page_errors(fresh)
        save_name = str(step.get("save_as") or "")
        if await self._try_api_fallback(save_name, RuntimeError(reason)):
            return
        raise WorkflowDateExportFailed(reason)

    async def _do_export(self, step: dict) -> None:
        """点击导出并使用站点提供的原始文件名保存到配置目录。

        等待下载时同时看当前页的请求结果和提示框。网关 400、HTTP 0 或
        「暂无数据」一类提示出现后立刻结束，不再干等下载事件。
        保存阶段的异常仍视为整步失败。

        Args:
            step: ``export`` 步骤。可用 ``timeout``、``selector``、``save_as``。

        Returns:
            None: 文件已保存，或 API 兜底成功。

        Raises:
            WorkflowExecutionError: 按钮缺失，或文件保存失败。
            WorkflowDateExportFailed: 这一天没有下载到文件。
            WorkflowSkipRepeatRound: 页面说明该日没有数据。
            WorkflowPaused: 文档签名失效，或导出前查询一直不结束。

        Examples:
            >>> "skip" == diagnose_export_miss([], "暂无数据")["action"]
            True
        """
        selector = step.get("selector", "button:has-text('导出')")
        save_name = str(step.get("save_as") or "")
        timeout = step.get("timeout", 30000)
        await self._pause_until_export_ready(
            timeout=int(step.get("query_wait_timeout", timeout)),
            settle_ms=int(step.get("query_settle_ms", 1200)),
        )

        logger.info("  点击导出: {}", selector)
        baseline = len(self._page_errors)
        kind, payload = await self._export_download(selector, int(timeout), baseline)
        if kind == "miss":
            await self._settle_export_miss(step, payload, baseline)
            return

        download = payload
        try:
            original_name = download.suggested_filename
            # 浏览器导出按 save_as 重命名（扩展名以站点为准），没写则保留站点原名。
            save_path = self._export_path(save_name, original_name)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = save_path.parent / f".{save_path.name}.{time.time_ns()}.tmp"
            await download.save_as(str(tmp_path))
            result = save_versioned_file(tmp_path, save_path)
            self._emit(
                "export_saved", path=str(save_path), original_name=original_name,
                size=save_path.stat().st_size,
            )
            self._note_file("saved", save_path)
            if result.reused_existing:
                logger.success("  导出成功（文件未变化）→ {}", save_path)
            elif result.archived_path:
                logger.success("  导出成功（已保留旧版本 {}）→ {}", result.archived_path, save_path)
            else:
                logger.success("  导出成功 → {}", save_path)
        except Exception as exc:
            logger.error("  导出失败: {}", exc)
            raise WorkflowExecutionError(f"导出失败: {exc}") from exc

    async def _try_api_fallback(self, save_as: str, original_error: BaseException) -> bool:
        fallback = self.config.get("api_fallback") or {}
        if not fallback or not fallback.get("enabled", True):
            return False
        api_url = fallback.get("url") or fallback.get("api_path") or ""
        if not api_url:
            return False

        self._emit("api_fallback", status="start", url=api_url, reason=str(original_error))
        try:
            result = await self._fetch_api_fallback(fallback)
            save_path = self._resolve_save_path(save_as, "api_fallback.xlsx").with_suffix(".xlsx")
            save_path.parent.mkdir(parents=True, exist_ok=True)
            self._save_api_fallback_result(result, save_path)
            logger.success("  API 降级导出成功 → {}", save_path)
            self._emit("api_fallback", status="success", url=api_url, path=str(save_path))
            self._note_file("saved", save_path)
            return True
        except Exception as exc:
            logger.error("  API 降级导出失败: {}", exc)
            self._emit("api_fallback", status="error", url=api_url, error=str(exc))
            return False

    async def _fetch_api_fallback(self, fallback: dict) -> dict:
        api_url = fallback.get("url") or fallback.get("api_path") or ""
        method = str(fallback.get("method", "GET")).upper()
        params = fallback.get("params") or {}
        form = fallback.get("form") or fallback.get("data") or {}
        json_body = fallback.get("json")
        timeout = int(fallback.get("timeout", 30000))
        js_code = """
        async ({url, method, params, form, jsonBody, timeout}) => {
          const fullUrl = new URL(url, window.location.origin);
          for (const [key, value] of Object.entries(params || {})) {
            fullUrl.searchParams.set(key, String(value));
          }
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), timeout);
          const headers = { "Accept": "application/json, text/plain, */*" };
          const options = { method, credentials: "include", headers, signal: controller.signal };
          if (jsonBody !== null && jsonBody !== undefined) {
            headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(jsonBody);
          } else if (form && Object.keys(form).length) {
            headers["Content-Type"] = "application/x-www-form-urlencoded";
            options.body = new URLSearchParams(form).toString();
          }
          try {
            const resp = await fetch(fullUrl.toString(), options);
            const contentType = resp.headers.get("content-type") || "";
            const filename = (resp.headers.get("content-disposition") || "").match(/filename="?([^";]+)"?/i)?.[1] || "";
            const buffer = await resp.arrayBuffer();
            let binary = "";
            const bytes = new Uint8Array(buffer);
            const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {
              binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
            }
            clearTimeout(timer);
            return {
              ok: resp.ok,
              status: resp.status,
              statusText: resp.statusText,
              contentType,
              filename,
              base64: btoa(binary),
            };
          } catch (error) {
            clearTimeout(timer);
            return { ok: false, status: 0, statusText: String(error), contentType: "", filename: "", base64: "" };
          }
        }
        """
        result = await self.page.evaluate(
            js_code,
            {
                "url": api_url,
                "method": method,
                "params": params,
                "form": form,
                "jsonBody": json_body,
                "timeout": timeout,
            },
        )
        if not result.get("ok"):
            raise WorkflowExecutionError(f"API fallback HTTP {result.get('status')}: {result.get('statusText')}")
        return result

    def _save_api_fallback_result(self, result: dict, save_path: Path) -> None:
        raw = base64.b64decode(result.get("base64") or "")
        content_type = (result.get("contentType") or "").lower()
        if (
            "spreadsheet" in content_type
            or "excel" in content_type
            or "octet-stream" in content_type
            or save_path.suffix.lower() in (".xlsx", ".xls")
        ) and not ("json" in content_type or "text" in content_type):
            save_versioned_bytes(save_path, raw)
            return

        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except Exception:
            data = {"text": text}
        self._save_data_as_xlsx(data, save_path)

    @staticmethod
    def _save_versioned_text(save_path: Path, text: str, encoding: str = "utf-8") -> None:
        save_versioned_bytes(save_path, text.encode(encoding))

    @staticmethod
    def _save_workbook_versioned(save_path: Path, wb) -> None:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = save_path.parent / f".{save_path.stem}.{time.time_ns()}{save_path.suffix or '.xlsx'}"
        wb.save(str(tmp_path))
        save_versioned_file(tmp_path, save_path)

    @staticmethod
    def _save_data_as_xlsx(data: Any, save_path: Path) -> None:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Data"

        if isinstance(data, dict) and "data" in data and isinstance(data["data"], (list, dict)):
            data = data["data"]

        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                headers: list[str] = []
                for item in data:
                    if isinstance(item, dict):
                        for key in item.keys():
                            if key not in headers:
                                headers.append(key)
                for ci, header in enumerate(headers, start=1):
                    ws.cell(row=1, column=ci, value=header)
                for ri, item in enumerate(data, start=2):
                    for ci, header in enumerate(headers, start=1):
                        ws.cell(row=ri, column=ci, value=item.get(header) if isinstance(item, dict) else str(item))
            else:
                for ri, item in enumerate(data, start=1):
                    ws.cell(row=ri, column=1, value=item)
        elif isinstance(data, dict):
            ws.cell(row=1, column=1, value="key")
            ws.cell(row=1, column=2, value="value")
            for ri, (key, value) in enumerate(data.items(), start=2):
                ws.cell(row=ri, column=1, value=key)
                ws.cell(row=ri, column=2, value=json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
        else:
            ws.cell(row=1, column=1, value=str(data))

        WorkflowRunner._save_workbook_versioned(save_path, wb)

    async def _do_switch_tab(self, step: dict) -> None:
        """切换到指定标签页。index: 0=第一个, -1=最后一个。"""
        index = step.get("index", -1)
        if index < 0:
            index = len(self.pages) + index
        if 0 <= index < len(self.pages):
            self._current_page_idx = index
            await self._bring_page_to_front(self.pages[index])
            logger.info("  切换到标签页 #{}", index)
        else:
            logger.warning("  无效的标签页索引: {}", index)

    async def _do_close_tab(self, step: dict) -> None:
        """关闭指定标签页。index: 要关闭的标签页索引。"""
        index = step.get("index", -1)
        if index < 0:
            index = len(self.pages) + index
        if not (0 <= index < len(self.pages)):
            logger.warning("  无效的标签页索引: {}", index)
            return
        if len(self.pages) <= 1:
            logger.info("  只有一个标签页，跳过关闭")
            return
        page_to_close = self.pages[index]
        if page_to_close in self._protected_pages:
            logger.info("保留用户已有页面")
            return
        # 关的是别的标签时，绑定页保持不变。关的是当前标签时，先选中旁边那一页再关。
        if page_to_close is self.page:
            survivor = self.pages[index - 1] if index > 0 else self.pages[index + 1]
        else:
            survivor = self.page
        await self._bring_page_to_front(survivor)
        self.pages.pop(index)
        try:
            await page_to_close.close()
        except Exception as exc:
            logger.warning("  关闭标签页失败: {}", exc)
        if survivor in self.pages:
            self._current_page_idx = self.pages.index(survivor)
        elif self._current_page_idx >= len(self.pages):
            self._current_page_idx = len(self.pages) - 1
        await self._bring_page_to_front(self.page)
        logger.info("  已关闭标签页 #{}，剩余 {} 个", index, len(self.pages))

    async def _do_close_other_tabs(self, step: dict) -> None:
        """关闭除当前标签外的所有其他标签页。"""
        keep_idx = step.get("keep_index", self._current_page_idx)
        if keep_idx < 0:
            keep_idx = len(self.pages) + keep_idx
        if not (0 <= keep_idx < len(self.pages)):
            logger.warning("  无效的保留标签页索引: {}", keep_idx)
            return

        closed = 0
        for i in range(len(self.pages) - 1, -1, -1):
            if i == keep_idx or self.pages[i] in self._protected_pages:
                continue
            try:
                await self.pages[i].close()
                closed += 1
            except Exception:
                pass

        # 重建 pages 列表
        kept_page = self.pages[keep_idx]
        self.pages = [kept_page] + [p for p in self.pages if p != kept_page and p in self._protected_pages]
        self._current_page_idx = 0
        await self._bring_page_to_front(kept_page)
        logger.info("  已关闭 {} 个其他标签页，保留 #{}", closed, keep_idx)

    async def _do_extract(self, step: dict) -> None:
        """提取页面文本内容，保存到 JSON。"""
        selectors = step.get("selectors", {})
        save_as = step.get("save_as", "extracted.json")

        result: dict[str, Any] = {}
        for name, sel in selectors.items():
            try:
                el = self.page.locator(sel).first
                text = await el.text_content()
                result[name] = text.strip() if text else ""
                logger.info("  提取 {}: {}", name, result[name])
            except Exception as exc:
                logger.warning("  提取失败 {}: {}", name, exc)
                result[name] = None

        save_path = self._resolve_save_path(save_as, "extracted.json")
        save_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_versioned_text(save_path, json.dumps(result, ensure_ascii=False, indent=2))
        self._note_file("saved", save_path)
        logger.info("  数据保存到: {}", save_path)

    async def _do_pause(self, step: dict) -> None:
        """暂停，提示用户手动操作。通过回调等待用户确认。"""
        msg = step.get("message", "请手动操作后继续...")
        logger.info("  ⏸️  {}", msg)
        self._on_event({"event": "waiting_input", "message": msg})
        if self._wait_continue:
            ok = await self._wait_continue()
            if not ok:
                self._stopped = True

    async def _do_screenshot(self, step: dict) -> None:
        """截屏保存。"""
        name = step.get("name", f"screenshot_{int(time.time())}.png")
        path = self.download_dir / name
        await self.page.screenshot(path=str(path), full_page=True)
        logger.info("  截图保存: {}", path)

    async def _do_fill(self, step: dict) -> None:
        """在输入框中填入文本。"""
        selector = step.get("selector", "")
        value = step.get("value", "")
        if not selector:
            logger.warning("  fill 缺少 selector")
            return
        if isinstance(value, str) and "{target_ym}" in value:
            target = self.target_date or date.today()
            value = value.replace("{target_ym}", f"{target.year}年{target.month}月")
        elif value in {"today", "yesterday", "{target}"} or (
            isinstance(value, str) and re.fullmatch(r"-\d+d", value)
        ):
            target = self._parse_date_value(str(value))
            value = f"{target.month}.{target.day:02d}"
        if re.search(r"\$\{[^}]+\}", str(value)):
            await self._abort_login_retry(f"敏感参数未配置或未替换: {value}")
        if not await self._low_profile_fill(selector, str(value)):
            raise WorkflowExecutionError(f"输入框未出现：{selector}")
        logger.info("  填入: {} → [已隐藏]", selector)

    async def _do_check(self, step: dict) -> None:
        """勾选复选框。如果已选中则跳过。"""
        selector = step.get("selector", "")
        if not selector:
            logger.warning("  check 缺少 selector")
            return
        if step.get("optional") and not await self._dom_visible(selector):
            logger.info("  可选复选框未出现，跳过: {}", selector)
            return
        if await self._dom_probe("checked", selector):
            logger.info("  已选中，跳过: {}", selector)
            return
        if not await self._low_profile_click(selector, timeout=3000):
            raise WorkflowExecutionError(f"复选框未出现：{selector}")
        checked_selector = selector.replace(":not(:checked)", "")
        if not await self._dom_probe("checked", checked_selector):
            raise WorkflowExecutionError(f"复选框未选中：{selector}")
        logger.info("  已勾选: {}", selector)

    async def _do_captcha(self, step: dict) -> None:
        """识别 4 位验证码并填入。不合格就刷新两次再认，登录仍由后面的 click 提交。

        去掉空白后必须是 4 位数字或英文字母。位数不对、汉字或其它字符都算错。
        错一次就用低痕迹点击验证码图片刷新两次，等新图后再识别，这算一轮。
        轮数读 ``self_check_rounds``，没写就是 3。识别前仍可用 ``refresh_before``。
        点击间隔不会低于 800 毫秒，避免瑞数把刷新当成脚本打回 HTTP 400。
        """
        import sys as _sys
        _src = Path(__file__).resolve().parent.parent / "src"
        if str(_src) not in _sys.path:
            _sys.path.insert(0, str(_src))

        img_selector = step.get("captcha_img", "img[src^='data:image']")
        input_selector = step.get("captcha_input", "#validateOnly_verifyCode")
        if any(key in step for key in ("submit_selector", "login_retry", "submit_wait_ms")):
            raise WorkflowExecutionError("captcha 不再提交登录；请把 submit_selector 改为后续独立 click 步骤，并移除 login_retry / submit_wait_ms")
        pre_refresh = captcha_refresh_count(step)
        rounds = captcha_self_check_rounds(step)
        if "retry" in step and "self_check_rounds" in step:
            logger.info(
                "  captcha 步骤同时写了 retry 和 self_check_rounds，轮数使用 self_check_rounds={}",
                rounds,
            )
        elif "retry" in step:
            logger.info(
                "  captcha 步骤的 retry={} 当作自检轮数；每一轮不合格会刷新两次。"
                "以后请改用 self_check_rounds",
                step.get("retry"),
            )

        from spider_gd.core.crawler.captcha_solver import CaptchaSolver
        solver = CaptchaSolver()

        # 预处理：先刷新验证码 N 次再识别（减少噪点/干扰线）。
        # 每次刷新都是新的服务端 key。点得太快会和图对不上，也会触发 400。
        if pre_refresh > 0:
            logger.info("  验证码预刷新 {} 次（低痕迹点击，间隔至少 0.8s）...", pre_refresh)
            for refresh_index in range(pre_refresh):
                await self._refresh_captcha_image(img_selector, step, f"before_{refresh_index + 1}")
            await asyncio.sleep(0.3)

        async def _recognize_once(label: str) -> str | None:
            await self._stop_captcha_on_request_error()
            try:
                code = await self._solve_captcha(img_selector, solver)
            except CaptchaError as exc:
                logger.warning("  验证码自检未通过（{}）: {}", label, exc)
                return None
            except WorkflowExecutionError:
                raise
            except Exception as exc:
                logger.warning("  验证码识别失败（{}）: {}", label, exc)
                return None
            await self._stop_captcha_on_request_error()
            logger.info("  验证码识别: {} ({})", code, label)
            return code

        async def _fill_code(code: str) -> None:
            await self._trace_browser_action("captcha.before_fill")
            # 返回普通数据并使用鼠标键盘，避免创建元素句柄触发预览初始化。
            if not await self._low_profile_fill(input_selector, code):
                raise WorkflowExecutionError(f"验证码输入框未出现: {input_selector}")
            await self._trace_browser_action("captcha.after_fill")
            await self._stop_captcha_on_request_error()
            logger.info("  验证码已填入；captcha 步骤结束，未提交登录")

        code = await _recognize_once("首次")
        if code:
            await _fill_code(code)
            return

        for round_index in range(1, rounds + 1):
            logger.info(
                "  验证码不合格，第 {}/{} 轮：低痕迹点击刷新两次后再识别",
                round_index, rounds,
            )
            await self._refresh_captcha_image(img_selector, step, f"check_{round_index}_1")
            await self._refresh_captcha_image(img_selector, step, f"check_{round_index}_2")
            code = await _recognize_once(f"第{round_index}轮")
            if code:
                await _fill_code(code)
                return

        # 全部失败 → 暂停等人工输入
        logger.error("  验证码自检 {} 轮都不合格，暂停等待人工输入", rounds)
        screenshot = await self._capture_failure_screenshot("captcha_exhausted")
        message = f"验证码自动识别失败（自检{rounds}轮），已保存现场截图，请手动输入验证码后点击继续"
        self._emit(
            "captcha_failed",
            error=f"验证码自动识别失败（自检{rounds}轮）",
            attempts=rounds,
            screenshot=screenshot,
        )
        self._emit("waiting_input", message=message)
        if self._wait_continue:
            ok = await self._wait_continue()
            if ok:
                return
        raise WorkflowExecutionError(f"验证码自动识别失败（自检{rounds}轮）")

    async def _refresh_captcha_image(self, img_selector: str, step: dict, label: str) -> None:
        """用低痕迹点击换一张验证码，并等到图片地址变化。

        出现请求异常时沿用原有暂停，不再继续点。点不到图片就记一笔并返回，
        让识别自己失败，而不是改用 locator.click。

        Args:
            img_selector (str): 验证码图片选择器。
            step (dict): captcha 步骤，用来读刷新间隔。
            label (str): 写进动作轨迹的阶段名。

        Returns:
            None: 刷新结束后返回。请求异常时改为抛出 ``WorkflowPaused``。

        Examples:
            >>> captcha_refresh_delay_s({"refresh_interval_ms": 800}, random.Random(0)) >= 0.8
            True
        """
        await self._stop_captcha_on_request_error()
        before = await self._dom_attr(img_selector, "src")
        delay_s = captcha_refresh_delay_s(step)
        await self._trace_browser_action(f"captcha.refresh_{label}")
        await asyncio.sleep(delay_s)
        clicked = await self._low_profile_click(img_selector, timeout=3000)
        if not clicked:
            logger.warning("  验证码图不可点击，跳过本次刷新")
        await self._stop_captcha_on_request_error()
        await self._wait_captcha_src(img_selector, before, delay_s)
        await self._wait_captcha_stable(img_selector)
        await self._trace_browser_action(f"captcha.refreshed_{label}")

    async def _stop_captcha_on_request_error(self) -> None:
        await self._drain_response_captures()
        errors = self._unacknowledged_page_errors()
        if errors:
            reason = "验证码步骤出现请求异常，已停止自动刷新和重试：" + "；".join(
                self._describe_page_errors(errors)
            )[:500]
            await self._pause_and_wait(reason + "。请取消本轮，修复页面后重新运行此步骤。")
            raise WorkflowPaused(reason)

    async def _wait_captcha_src(
        self, img_selector: str, before: str | None, delay_s: float,
    ) -> None:
        """等验证码图片地址变化。每次查看至少隔开 0.8 秒，最多看 4 次。

        地址一直不变就停，不再按很短的间隔反复读页面。

        Args:
            img_selector: 验证码图片选择器。
            before: 点击前的图片地址。
            delay_s: 两次查看之间的秒数，不会低于 0.8。

        Returns:
            None: 看到新地址，或已经看过 4 次后返回。

        Examples:
            >>> captcha_refresh_delay_s({"refresh_interval_ms": 100}, random.Random(0)) >= 0.8
            True
        """
        gap = max(delay_s, 0.8)
        for attempt in range(4):
            await asyncio.sleep(gap)
            current = await self._dom_attr(img_selector, "src")
            if current and current != before:
                return
            if attempt == 3:
                logger.info("  验证码图片地址没有变化，停止查看")

    async def _wait_captcha_stable(self, img_selector: str, idle_ms: int = 400, timeout: int = 4000) -> None:
        """等验证码图片地址停住再截图。

        最多读几次，两次之间至少隔 0.4 秒。地址不变就返回，
        不再用 0.1 秒的间隔反复读页面。

        Args:
            img_selector: 验证码图片选择器。
            idle_ms: 两次查看的间隔，小于 400 时按 400。
            timeout: 用来限制最多看几次，不是紧密轮询的时长。

        Returns:
            None: 连续两次地址相同，或次数用完后返回。

        Examples:
            >>> max(400, 100) == 400
            True
        """
        idle_s = max(idle_ms, 400) / 1000
        # timeout 只保留给旧调用，不再按它折成很多次读取。
        reads = 2 if timeout >= 0 else 2
        previous: str | None = None
        for _ in range(reads):
            await asyncio.sleep(idle_s)
            current = await self._dom_attr(img_selector, "src") or ""
            if previous is not None and current == previous:
                return
            previous = current

    async def _captcha_image_bytes(self, img_selector: str) -> bytes:
        """优先解码已显示的 data 图片；其它情况仅使用 CDP 坐标截图。"""
        src = await self._dom_attr(img_selector, "src")
        if isinstance(src, str) and src.startswith("data:image") and "," in src:
            header, payload = src.split(",", 1)
            if ";base64" in header:
                with contextlib.suppress(Exception):
                    return base64.b64decode(payload)
        clipped = await self._captcha_cdp_screenshot(img_selector)
        if clipped:
            return clipped
        raise WorkflowExecutionError("验证码取图失败：图片无法直接解码，坐标截图也不可用；已停止")

    async def _captcha_cdp_screenshot(self, img_selector: str) -> bytes | None:
        """根据普通坐标数据截取图片区域，不创建元素句柄。"""
        box = await self._dom_box(img_selector, timeout=1000)
        if not isinstance(box, dict):
            return None
        session = None
        try:
            session = await self.page.context.new_cdp_session(self.page)
            result = await session.send("Page.captureScreenshot", {
                "format": "png",
                # clip 是文档坐标，不是视口坐标；页面有滚动时要用 pageX/pageY。
                "clip": {
                    "x": box.get("pageX", box["x"]), "y": box.get("pageY", box["y"]),
                    "width": box["width"], "height": box["height"], "scale": 1,
                },
            })
            return base64.b64decode(result.get("data", ""))
        except Exception as exc:
            logger.debug("  CDP 裁剪截图失败: {}", exc)
            return None
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.detach()

    async def _solve_captcha(self, img_selector: str, solver) -> str:
        await self._wait_captcha_stable(img_selector)
        await self._trace_browser_action("captcha.before_screenshot")
        img_bytes = await self._captcha_image_bytes(img_selector)
        await self._trace_browser_action("captcha.after_screenshot")
        return solver.recognize(img_bytes)

    async def _read_login_error(self, step: dict) -> str:
        """读取登录失败提示。

        现场不只有 .ant-modal-confirm：证书/PIN 失败是弹窗，验证码错误常常是
        toast/message。以前只认弹窗，导致“点了没反应”时没有任何原因可查。
        """
        selectors = step.get("error_selectors") or [
            ".ant-modal-confirm",
            ".el-message-box",
            ".ant-message-error",
            ".el-message--error",
            ".ant-notification-notice-error",
        ]
        for selector in selectors:
            try:
                block = self.page.locator(selector).first
                if not await block.is_visible(timeout=200):
                    continue
                text = re.sub(r"\s+", " ", (await block.inner_text(timeout=1000)) or "").strip()
                if text:
                    return text[:300]
            except Exception:
                continue
        return ""

    async def _login_stage_ok(self, step: dict | None = None) -> bool:
        """登录阶段只核对当前页是否进入工作台。"""
        return await self._is_logged_in_dashboard()

    def _describe_auth_traffic(self) -> str:
        """把登录阶段请求按“业务响应 / 网关拦截”分类。

        关键区分：业务错误是 application/json + `{"code":...}`；
        网关/WAF 拦截是 text/html + 极短响应体（现场为 6 字节）。
        """
        if not self._auth_traffic:
            return "登录阶段没有捕获到 sso-auth 请求"
        parts = []
        for item in self._auth_traffic[-4:]:
            ct = (item.get("content_type") or "").lower()
            try:
                status = int(item.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            size = item.get("body_size")
            short_body = isinstance(size, int) and 0 <= size <= self._GATEWAY_BODY_MAX
            if "json" in ct:
                kind = "业务响应"
            elif status >= 400 and (("html" in ct) or short_body):
                kind = "疑似网关拦截"
            elif not ct:
                kind = "无 content-type"
            else:
                kind = ct.split(";")[0]
            content_type = ct or "无 content-type"
            parts.append(
                f"{item['method']} {item['path']} → HTTP {item['status']} {content_type}（{kind}）"
            )
        return "；".join(parts)

    async def _dump_login_diagnostics(self, reason: str) -> str:
        """登录失败现场落盘：半登录证据 + 登录请求 + 控制台/页面异常。"""
        try:
            page = self.page
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = self.download_dir / "_failures" / f"login_diagnostics_{stamp}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            keys: list[str] = []
            token_len = 0
            user_info_len = 0
            if page is not None:
                with contextlib.suppress(Exception):
                    keys = await page.evaluate(
                        "() => { const out = []; for (let i = 0; i < localStorage.length; i += 1) "
                        "out.push(localStorage.key(i)); return out; }"
                    )
                with contextlib.suppress(Exception):
                    token_len = await page.evaluate(
                        "() => String(localStorage.getItem('pfxh-token') || '').length"
                    )
                with contextlib.suppress(Exception):
                    user_info_len = await page.evaluate(
                        "() => String(localStorage.getItem('pfxh-userInfo') || '').length"
                    )
            lines = [
                f"reason: {reason}",
                f"url: {page.url if page is not None else ''}",
                f"localStorage keys: {', '.join(keys)}",
                f"pfxh-token length: {token_len}（0 表示半登录）",
                f"pfxh-userInfo length: {user_info_len}",
                "",
                "--- 登录阶段请求（sso-auth） ---",
                *[
                    f"{item['at']} {item['method']} HTTP {item['status']} "
                    f"{item.get('content_type') or '-'} {item['url']}"
                    for item in self._auth_traffic[-20:]
                ],
                "",
                "--- 失败请求（network / 证书服务 WS） ---",
                *self._failed_requests[-20:],
                "",
                "--- console errors ---",
                *[f"{item['at']} {item['text']}" for item in self._console_errors[-20:]],
                "",
                "--- page exceptions ---",
                *self._page_exceptions[-20:],
            ]
            path.write_text("\n".join(lines), encoding="utf-8")
            await self._save_page_html(f"login_{stamp}")
            logger.info("  登录诊断已保存: {}", path)
            return str(path)
        except Exception as exc:
            logger.debug("  登录诊断保存失败: {}", exc)
            return ""

    async def _pause_login_error(self, reason: str) -> None:
        screenshot = await self._capture_failure_screenshot("login_error")
        diagnostics = await self._dump_login_diagnostics(reason)
        traffic = self._describe_auth_traffic()
        detail = f"{reason}｜{traffic}"
        if diagnostics:
            detail += f"｜诊断: {diagnostics}"
        while True:
            self._paused = True
            self._emit("error_pause", error=detail, message=reason + "；请人工修复后重新检查",
                       screenshot=screenshot, diagnostics=diagnostics, auth_traffic=traffic)
            self._emit("waiting_input", message="已暂停整个任务，等待人工处理")
            continued = False
            if self._wait_continue:
                continued = await self._wait_continue()
            if not continued:
                self._mark_failed(detail)
                if self._wait_continue:
                    self._stopped = True
                    self._emit("aborted", message="用户取消")
                raise WorkflowPaused(detail)
            if self._shared_session is None:
                self._mark_failed(detail)
                raise WorkflowPaused(detail)
            auth = await self._shared_session.check_auth(self.page)
            if auth["state"] == "authenticated":
                self._paused = False
                self._skip_remaining_login_steps = True
                return
            reason = auth.get("reason", "登录仍未恢复")
            traffic = self._describe_auth_traffic()
            detail = f"{reason}｜{traffic}"
            if diagnostics:
                detail += f"｜诊断: {diagnostics}"

    async def _do_dismiss_modal(self, step: dict) -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        selector = step.get("selector", ".ant-modal-confirm-btns button")
        timeout = step.get("timeout", 5000)
        error_selectors = step.get("error_selectors") or [
            selector,
            ".ant-modal-confirm",
            ".el-message-box",
            ".ant-message-error",
            ".el-message--error",
            ".ant-notification-notice-error",
        ]
        visible_selector = ""
        deadline = time.monotonic() + max(int(timeout), 0) / 1000
        for candidate in error_selectors:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if await self._dom_visible(candidate, timeout=remaining):
                visible_selector = candidate
                break
        if not visible_selector:
            return
        async def optional_text(selector):
            try:
                texts = await self._dom_texts(selector)
                return texts[0] if texts else ""
            except (PlaywrightTimeoutError, TimeoutError):
                return ""
        title = await optional_text(".ant-modal-confirm-title")
        content = await optional_text(".ant-modal-confirm-content")
        if not content:
            content = await optional_text(visible_selector)
        reason = f"{title}: {content}" if title else content or "业务页面错误弹窗"
        if step.get("report_error", True):
            await self._pause_login_error(reason)
        else:
            if not await self._low_profile_click(selector, timeout=3000):
                raise WorkflowExecutionError("错误弹窗按钮不可操作")

    async def _do_hover(self, step: dict) -> None:
        """悬停在元素上（触发下拉菜单/tooltip）。

        只用 DOM 探针取坐标 + ``page.mouse.move`` 人形轨迹移过去停住，不按键；
        不调用 page.hover / locator.hover，不创建 ElementHandle。
        """
        selector = str(step.get("selector", "") or "")
        timeout = int(step.get("timeout", 5000))
        if not selector:
            raise WorkflowExecutionError("hover 缺少 selector")
        await self._trace_browser_action("hover.before")
        await self._bring_page_to_front(self.page)
        box = await self._dom_box(selector, timeout=timeout)
        if box is None:
            raise WorkflowExecutionError(f"悬停目标未出现：{selector}")
        try:
            await self._human_move_to(box)
        except Exception as exc:
            raise WorkflowExecutionError(f"坐标悬停失败：{selector}") from exc
        await asyncio.sleep(0.5)
        await self._trace_browser_action("hover.after")
        logger.info("  悬停: {}", selector)

    _SELECT_OPTION_SELECTORS = (
        # 精确匹配优先，避免“选项1”误中“选项10”；探针只取可见节点。
        ".el-select-dropdown__item:text-is({value})",
        ".ant-select-item-option:has(.ant-select-item-option-content:text-is({value}))",
        ".el-select-dropdown__item:has-text({value})",
        ".ant-select-item-option:has-text({value})",
    )

    async def _do_select(self, step: dict) -> None:
        """选择下拉框选项（Element UI select / Ant Design select）。

        触发器与选项都走 ``_low_profile_click``（坐标 + 人形按压），不创建
        ElementHandle、不调用 locator.click；选项找不到直接中止。
        可选 ``option_selector`` 指定选项选择器（支持 ``{value}`` 占位）。
        """
        selector = str(step.get("selector", "") or "")
        value = str(step.get("value", "") or "")
        timeout = int(step.get("timeout", 5000))
        if not selector:
            raise WorkflowExecutionError("select 缺少 selector")
        logger.info("  选择下拉: {} = {}", selector, value)
        await self._trace_browser_action("select.before_open")
        if not await self._low_profile_click(selector, timeout=timeout):
            raise WorkflowExecutionError(f"下拉触发器未出现：{selector}")
        await asyncio.sleep(0.4)
        if not value:
            return
        quoted = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        custom = step.get("option_selector")
        candidates = [str(custom).replace("{value}", value)] if custom else [
            template.format(value=quoted) for template in self._SELECT_OPTION_SELECTORS
        ]
        deadline = time.monotonic() + timeout / 1000
        while True:
            for option in candidates:
                if await self._dom_visible(option):
                    if not await self._low_profile_click(option, timeout=500):
                        raise WorkflowExecutionError(f"下拉选项点击失败：{value}")
                    await self._trace_browser_action("select.after_option")
                    logger.info("  选中选项: {}", value)
                    return
            if time.monotonic() >= deadline:
                raise WorkflowExecutionError(f"未找到下拉选项：{value}（{selector}）")
            await asyncio.sleep(0.15)

    async def _do_pick_date_range(self, step: dict) -> None:
        """选择日期范围（Element UI daterange 的两个 el-range-input，或两个独立输入框）。

        start_selector / end_selector 指定开始/结束输入框；值格式同 pick_date：
        today / yesterday / -Nd / YYYY-MM-DD。``format`` 可改日期格式（默认 %Y-%m-%d），
        ``confirm`` 为 false 时不按 Enter。

        只走安全路径：坐标点击聚焦 → 真实键盘逐字输入 → keyboard.press("Enter")。
        不使用 page.click / page.fill / page.press，任何一步失败直接中止。
        """
        start_sel = step.get("start_selector", ".el-range-editor input.el-range-input:nth-of-type(1)")
        end_sel = step.get("end_selector", ".el-range-editor input.el-range-input:nth-of-type(2)")
        fmt = str(step.get("format", "%Y-%m-%d"))
        confirm = str(step.get("confirm", True)).lower() not in ("false", "0", "no")
        start_str = self._parse_date_value(str(step.get("start_value", "-7d"))).strftime(fmt)
        end_str = self._parse_date_value(str(step.get("end_value", "yesterday"))).strftime(fmt)
        logger.info("  选择日期范围: {} ~ {}", start_str, end_str)
        await self._trace_browser_action("pick_date_range.before")
        for sel, text in ((start_sel, start_str), (end_sel, end_str)):
            if not await self._low_profile_fill(sel, text):
                raise WorkflowExecutionError(f"日期范围输入框未出现：{sel}")
            if confirm:
                await asyncio.sleep(random.uniform(0.12, 0.25))
                await self.page.keyboard.press("Enter")
            await asyncio.sleep(0.3)
            actual = await self._dom_probe("value", sel)
            if actual != text:
                raise WorkflowExecutionError(f"日期范围未生效：{sel} 期望 {text}，实际 {actual!r}")
        await self._trace_browser_action("pick_date_range.after")

    async def _do_extract_table(self, step: dict) -> None:
        """提取 HTML 表格内容，保存为 CSV/JSON/Excel。

        处理合并单元格（rowspan/colspan），输出完整的二维表格。

        字段：
            selector: 表格容器选择器（默认 table）
            save_as: 保存文件名，支持 {date} 占位符
            format: csv / json / xlsx（默认 csv）
        """
        selector = step.get("selector", "table")
        save_as = step.get("save_as", "table_{date}.csv")
        fmt = step.get("format", "csv")

        logger.info("  提取表格: {}", selector)

        # 用 JS 构建二维网格，处理 rowspan/colspan
        js_code = """
        (sel) => {
            const table = document.querySelector(sel);
            if (!table) return { _error: 'table not found: ' + sel };

            const trs = table.querySelectorAll('tr');
            if (!trs.length) return { _error: 'no rows' };

            let maxCols = 0;
            const values = [];    // values[row][col] = text

            for (let r = 0; r < trs.length; r++) {
                const cells = trs[r].querySelectorAll('td, th');
                let col = 0;
                if (!values[r]) values[r] = [];

                for (const cell of cells) {
                    // 跳过被上方 rowspan 占用的列
                    while (col < (values[r].length || 0) && values[r][col] !== undefined) col++;

                    const rs = parseInt(cell.getAttribute('rowspan') || '1');
                    const cs = parseInt(cell.getAttribute('colspan') || '1');
                    const text = (cell.textContent || '').trim().replace(/\\s+/g, ' ');

                    // 写入 values 网格
                    for (let dr = 0; dr < rs; dr++) {
                        if (!values[r + dr]) values[r + dr] = [];
                        for (let dc = 0; dc < cs; dc++) {
                            // 只有第一个格子放文本，其余放空串（span 占位）
                            values[r + dr][col + dc] = (dr === 0 && dc === 0) ? text : '';
                        }
                    }
                    col += cs;
                }
                if (col > maxCols) maxCols = col;
            }

            // 展平为 2D 数组
            const result = [];
            for (let r = 0; r < values.length; r++) {
                const rowResult = [];
                for (let c = 0; c < maxCols; c++) {
                    rowResult.push((values[r] && values[r][c] !== undefined) ? values[r][c] : '');
                }
                result.push(rowResult);
            }

            return { rows: result, maxCols };
        }
        """

        try:
            raw = await self.page.evaluate(js_code, selector)
        except Exception as exc:
            logger.error("  表格提取失败: {}", exc)
            self._note_file("failed", reason=f"表格提取失败: {exc}")
            return

        if isinstance(raw, dict) and "_error" in raw:
            logger.error("  表格提取失败: {}", raw["_error"])
            self._note_file("failed", reason=f"表格提取失败: {raw['_error']}")
            return

        rows = raw.get("rows", [])
        logger.info("  提取到 {} 行 x {} 列", len(rows), raw.get("maxCols", 0))

        save_path = self._resolve_save_path(save_as, "table.csv")
        save_path.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "json":
            save_path = save_path.with_suffix(".json")
            self._save_versioned_text(save_path, json.dumps(rows, ensure_ascii=False, indent=2))
        elif fmt == "xlsx":
            save_path = save_path.with_suffix(".xlsx")
            self._save_grid_as_xlsx(rows, save_path)
        else:
            import csv as _csv
            save_path = save_path.with_suffix(".csv")
            output = io.StringIO()
            writer = _csv.writer(output)
            writer.writerows(rows)
            self._save_versioned_text(save_path, output.getvalue(), encoding="utf-8-sig")

        self._note_file("saved", save_path)
        logger.success("  表格保存 → {}", save_path)

    def _calendar_blocking_errors(self, baseline: int) -> list[dict]:
        """这一轮翻周之后，当前页上会让日历读取失效的请求错误。

        Args:
            baseline: 本周开始前 ``_page_errors`` 的长度。

        Returns:
            list[dict]: 网关空 HTML 400，或日历查询 / 开关轮询自己的失败。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._calendar_blocking_errors(0)
            []
        """
        found = []
        for item in self._page_errors[baseline:]:
            if item.get("_resolved") or not self._export_error_on_page(item):
                continue
            url = str(item.get("url") or "")
            if (
                self._is_gateway_form(item)
                or "findTradeCalendar" in url
                or "systemSwitch/getSwitch" in url
            ):
                found.append(item)
        return found

    async def _calendar_query_pending(self) -> bool:
        """当前页是否还有交易日历查询在途。

        Returns:
            bool: ``findTradeCalendar`` 仍在飞时为 True。开关轮询不算。

        Examples:
            >>> WorkflowRunner._is_gateway_form({"status": 200})
            False
        """
        for request in self._inflight_query_requests():
            url = str(getattr(request, "url", "") or "")
            if "findTradeCalendar" in url or "tradeCalendarOut" in url:
                return True
        return False

    async def _pause_for_calendar_gateway(self, errors: list[dict]) -> None:
        """日历查询被网关拒绝时暂停，禁止记成空周。

        Args:
            errors: 已经确认的网关或日历请求错误。

        Returns:
            None: 不会正常返回。

        Raises:
            WorkflowPaused: 用户取消，或没有继续回调。

        Examples:
            >>> "findTradeCalendar" in "tradeCalendarOut/findTradeCalendar"
            True
        """
        if self.page is not None:
            self._poisoned_pages.add(self.page)
        described = "；".join(self._describe_page_errors(errors))[:500]
        reason = (
            "交易日历请求被网关拒绝（HTTP 400 空 HTML，当前文档的请求签名已失效）。"
            f"{described}。"
            "这一周不能记成没有交易事件。不要刷新当前文档，请回到门户重新进入业务页。"
        )
        logger.error("  {}", reason)
        await self._pause_and_wait(reason)
        raise WorkflowPaused(reason)

    async def _ensure_calendar_settled(self, baseline: int, timeout_ms: int = 8000) -> None:
        """等本周的日历查询结束；期间出现网关 400 就暂停。

        周视图的日期格会先换，事件要等 ``findTradeCalendar`` 回来才画上。
        查询还在飞或刚被网关拒绝时，空网格不是「没有交易事件」。

        Args:
            baseline: 本周开始前的错误条数。
            timeout_ms: 最长等待，毫秒。查询一直不结束时到点返回，调用方再看错误。

        Returns:
            None: 查询已停，且这段时间没有网关拒绝。

        Raises:
            WorkflowPaused: 当前文档的日历或开关请求被网关拒绝。

        Examples:
            >>> WorkflowRunner._is_gateway_form({
            ...     "status": 400, "resource_type": "xhr", "content_type": "text/html",
            ...     "body_size": 6, "body_blank": True,
            ... })
            True
        """
        deadline = time.monotonic() + max(int(timeout_ms), 0) / 1000
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            await self._drain_response_captures()
            blocking = self._calendar_blocking_errors(baseline)
            if blocking:
                await self._pause_for_calendar_gateway(blocking)
            if await self._calendar_query_pending():
                quiet_since = None
            else:
                if quiet_since is None:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= 1.2:
                    return
            await asyncio.sleep(0.15)
        await self._drain_response_captures()
        blocking = self._calendar_blocking_errors(baseline)
        if blocking:
            await self._pause_for_calendar_gateway(blocking)

    def _tracked_calendar_monday(self) -> date:
        """当前认为周视图停在哪一周。

        优先用上一周成功切过去的周一。还没切过时，按今天所在周，
        和页面刚打开时默认显示的当前周一致。不再读日历 DOM。

        Returns:
            date: 该周周一。

        Examples:
            >>> WorkflowRunner._tracked_calendar_monday.__name__
            '_tracked_calendar_monday'
        """
        shown = getattr(self, "_calendar_shown_monday", None)
        if isinstance(shown, date):
            return shown
        today = date.today()
        return today - timedelta(days=today.weekday())

    def _calendar_payload_on_page(self, item: dict) -> bool:
        """这条日历响应是不是当前绑定页的。

        Args:
            item: ``_calendar_payloads`` 里的一条。

        Returns:
            bool: 没有页面归属，或归属就是当前页时为 True。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._calendar_payload_on_page({"status": 200})
            True
        """
        owner = item.get("_page")
        page = self.page
        return not (owner is not None and page is not None and owner is not page)

    async def _await_calendar_payload(self, baseline: int, *, clicked: bool) -> dict | None:
        """等到这一周的 findTradeCalendar 响应。

        没点翻周时用已经捕获的最近一条。点过之后只认基线之后的新响应。
        网关 400 在这里暂停，不返回给调用方去记空周。

        Args:
            baseline: 本周点击前已有的响应条数。
            clicked: 这一周是否点过日期格。

        Returns:
            dict | None: 响应记录。超时且没有可用响应时为 None。

        Raises:
            WorkflowPaused: 当前文档的日历或开关请求被网关拒绝。

        Examples:
            >>> "findTradeCalendar" in "findTradeCalendar"
            True
        """
        if not clicked:
            prior = [item for item in self._calendar_payloads if self._calendar_payload_on_page(item)]
            if prior:
                return prior[-1]
        deadline = time.monotonic() + float(getattr(self, "_calendar_payload_wait_s", 8))
        watch = getattr(self, "_calendar_watch_baseline", 0)
        while time.monotonic() < deadline:
            await self._drain_response_captures()
            blocking = self._calendar_blocking_errors(watch)
            if blocking:
                await self._pause_for_calendar_gateway(blocking)
            fresh = [
                item for item in self._calendar_payloads[baseline:]
                if self._calendar_payload_on_page(item) and not item.get("ignored")
            ]
            if fresh:
                return fresh[-1]
            await asyncio.sleep(0.15)
        return None

    async def _do_select_calendar_week(self, step: dict, *, target: date | None = None) -> None:
        """用页面自带的周选择器把周视图移到任务目标周。

        只做和日清算相同的坐标点击：打开日期框、按已知月份差点翻月、点目标日。
        不轮询面板脚本，也不读日历 DOM。事件由随后的 findTradeCalendar 响应提供。
        """
        target = target or self.target_date or date.today()
        monday = target - timedelta(days=target.weekday())
        selector = str(step.get("selector") or ".calendar-box")
        shown = self._tracked_calendar_monday()
        self._calendar_week_clicked = shown != monday
        if shown == monday:
            self._calendar_shown_monday = monday
            return
        self._calendar_ignored_requests = {id(item) for item in list(self._inflight)}
        await self._bring_page_to_front(self.page)
        # 只做和日清算相同的坐标点击。月份差按已知周计算，不再轮询面板脚本。
        if not await self._low_profile_click(f"{selector} .date-box input", timeout=5000):
            if not await self._low_profile_click(".date-box input", timeout=5000):
                raise WorkflowExecutionError("找不到周选择输入框（.date-box input）")
        await asyncio.sleep(0.4)
        delta = (monday.year - shown.year) * 12 + (monday.month - shown.month)
        arrow = (
            ".el-picker-panel .el-picker-panel__icon-btn.el-icon-arrow-left"
            if delta < 0
            else ".el-picker-panel .el-picker-panel__icon-btn.el-icon-arrow-right"
        )
        for _ in range(abs(delta)):
            if not await self._low_profile_click(arrow, timeout=3000):
                raise WorkflowExecutionError("周选择器翻月按钮不可用，无法翻周")
            await asyncio.sleep(0.35)
        day_selector = (
            ".el-picker-panel .el-date-table td.available"
            f":not(.prev-month):not(.next-month):text-is('{monday.day}')"
        )
        if not await self._low_profile_click(day_selector, timeout=3000):
            raise WorkflowExecutionError(f"周选择器里找不到目标日期 {monday.isoformat()}")
        self._calendar_shown_monday = monday
        self._calendar_week_clicked = True

    async def _do_extract_calendar_month_weeks(self, step: dict) -> None:
        """按月选、按周执行：一次运行内翻遍目标月的所有周并合并进月文件。

        周视图明细比月视图全（标的时间/申报时间/正式灾备），但一次只有一周；
        这里用周选择器逐周切换（点某天=选该天所在周），每周读一次 merge 进
        交易日历明细YYYY-MM.csv。空周（无交易安排）跳过不报错。
        月份优先取任务的 query_month，其次 target_date 所在月。
        """
        month_anchor = self.query_month or self.target_date or date.today()
        month_start = month_anchor.replace(day=1)
        month_text = month_start.strftime("%Y-%m")
        mondays = weeks_of_month(month_start)
        logger.info("  交易日历 {} 共 {} 周，逐周读取", month_text, len(mondays))
        # 点“周”后页面上可能同时留着月视图日历盒。周选择器在周视图那一盒里，
        # 所以翻周用本步骤的 selector（.calendar-box:has(周视图)），不要点到月视图的输入框。
        week_selector = str(step.get("week_selector") or step.get("selector") or ".calendar-box")
        extract_step = {
            **step,
            "action": "extract_calendar",
            "month_mode": "week",
            "from_network": True,
        }
        for monday in mondays:
            # 这一周里新出现的网关 400 不能记成「没有交易事件」。
            self._calendar_watch_baseline = len(self._page_errors)
            self._calendar_payload_baseline = len(self._calendar_payloads)
            await self._bring_page_to_front(self.page)
            await self._do_select_calendar_week({"selector": week_selector}, target=monday)
            await self._do_extract_calendar(extract_step, strict=False)
            await asyncio.sleep(0.8)

    async def _do_extract_calendar(self, step: dict, *, strict: bool = True) -> None:
        """读取安插在页面上的 FullCalendar 交易日历，保存为固定列 CSV。

        字段：
            selector: 日历容器（默认 .calendar-box）
            save_as: 文件名，支持 {month_iso} / {month}
            include_empty_days: 当月没有交易安排的日期是否也输出一行（默认 true）
            include_other: 是否读取日历网格里上/下月的日期（周视图跨月时用，默认 false）
            month_mode: page（按页面显示月份，默认）/ target（按任务目标月份）
            merge: 是否与已有 CSV 合并（周明细每天补一次时用，默认 false）

        为什么不用 extract_table：交易日历是 FullCalendar 的 div 网格，不是 <table>；
        直接读 ``[data-date]`` + ``.fc-event`` 比依赖表格结构稳定。
        """
        selector = str(step.get("selector") or ".calendar-box")
        save_as = str(step.get("save_as") or "交易日历{month_iso}.csv")
        include_empty_days = step.get("include_empty_days", True)
        include_other = bool(step.get("include_other", False))
        merge = bool(step.get("merge", False))
        month_mode = str(step.get("month_mode") or "page").strip().lower()
        if month_mode not in {"page", "target", "week"}:
            month_mode = "page"

        logger.info("  读取交易日历: {}", selector)
        network_view = ""
        rows: list = []
        if step.get("from_network"):
            payload = await self._await_calendar_payload(
                getattr(self, "_calendar_payload_baseline", 0),
                clicked=bool(getattr(self, "_calendar_week_clicked", True)),
            )
            if payload is None and not getattr(self, "_calendar_week_clicked", True):
                logger.warning("  没有捕获到 findTradeCalendar，退回读网格")
            elif payload is None:
                raise WorkflowExecutionError(
                    "翻周后没有收到 findTradeCalendar 响应，不能记成没有交易事件"
                )
            elif int(payload.get("status") or 0) >= 400:
                await self._pause_for_calendar_gateway([payload])
            else:
                parsed = calendar_rows_from_api(str(payload.get("text") or ""))
                if parsed is None:
                    raise WorkflowExecutionError(
                        "findTradeCalendar 返回的不是 JSON，不能记成没有交易事件"
                    )
                rows = self._limit_calendar_rows(parsed)
                network_view = "周"
        if not network_view:
            try:
                raw = await self.page.evaluate(
                    _CALENDAR_EXTRACT_JS,
                    {"selector": selector, "includeOther": include_other},
                )
            except Exception as exc:
                raise WorkflowExecutionError(f"交易日历读取失败: {exc}") from exc
            if not isinstance(raw, dict):
                raise WorkflowExecutionError("交易日历读取失败: 页面返回结构异常")
            rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
            page_month_hint = str(raw.get("month") or "")
            view_hint = str(raw.get("view") or "")
        else:
            page_month_hint = ""
            view_hint = network_view
        if not rows:
            await self._ensure_calendar_settled(
                getattr(self, "_calendar_watch_baseline", 0), timeout_ms=2000,
            )
            if not strict:
                if step.get("from_network"):
                    logger.info("  交易日历目标周没有交易事件，跳过")
                    self._note_calendar_empty(step)
                else:
                    logger.info("  交易日历当前周无事件，跳过")
                return
            # 不写空文件：没有日期通常意味着页面没加载完/账号没有日历权限。
            raise WorkflowExecutionError("交易日历读取失败: 页面上没有找到任何日期")

        page_month = normalize_month(page_month_hint)
        target_month = (self.target_date or date.today()).strftime("%Y-%m")
        month = target_month if month_mode == "target" else (page_month or target_month)

        # A week can span two months. Write both months into their own files.
        months = sorted({str(item.get("date") or "")[:7] for item in rows
                         if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item.get("date") or ""))}) if month_mode == "week" else [month]
        saved = 0
        for output_month in months:
            save_name = save_as.replace("{month_iso}", output_month).replace("{month}", output_month.replace("-", ""))
            save_path = self._resolve_save_path(save_name, f"交易日历{output_month}.csv")
            if save_path.suffix.lower() != ".csv":
                save_path = save_path.with_suffix(".csv")
            table = build_calendar_table(
                rows, month=output_month, view=view_hint or "月",
                include_empty_days=bool(include_empty_days),
                captured_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            if len(table) <= 1:
                continue
            if merge and save_path.exists():
                import csv as _csv
                try:
                    with save_path.open("r", encoding="utf-8-sig", newline="") as fh:
                        existing_rows = list(_csv.DictReader(fh))
                    table = merge_calendar_rows(existing_rows, table)
                except Exception as exc:
                    logger.warning("  旧交易日历读取失败，按新文件写入: {}", exc)
            self._save_versioned_text(save_path, calendar_csv_text(table), encoding="utf-8-sig")
            saved += 1
            logger.success("  交易日历已保存 → {}（{} 行）", save_path, len(table) - 1)
            self._on_event({"event": "calendar_saved", "path": str(save_path),
                            "month": output_month, "rows": len(table) - 1})
            self._note_file("saved", save_path)
            self._calendar_saved_paths.add(str(save_path))
        if not saved:
            await self._ensure_calendar_settled(
                getattr(self, "_calendar_watch_baseline", 0), timeout_ms=2000,
            )
            if not strict:
                logger.info("  交易日历目标周没有交易事件，跳过")
                self._note_calendar_empty(step)
                return
            raise WorkflowExecutionError("交易日历读取失败: 目标周没有交易事件")

    def _limit_calendar_rows(self, rows: list[dict]) -> list[dict]:
        """只留下当前这一周、并且落在任务目标月里的事件。

        接口一次会带回整月甚至相邻月。周明细按周执行，不能把目标周以外的日期
        写进别的月份文件。目标月里、这一周跨出去的日期留给那个月自己的任务。

        Args:
            rows: 接口解析出的事件。

        Returns:
            list[dict]: 过滤后的事件。没有目标周时原样返回。

        Examples:
            >>> runner = WorkflowRunner({"name": "n", "steps": []}, download_dir="/tmp")
            >>> runner._calendar_shown_monday = date(2026, 8, 31)
            >>> runner.query_month = date(2026, 9, 1)
            >>> kept = runner._limit_calendar_rows([
            ...     {"date": "2026-08-31"}, {"date": "2026-09-01"}, {"date": "2026-09-15"},
            ... ])
            >>> [item["date"] for item in kept]
            ['2026-09-01']
        """
        monday = getattr(self, "_calendar_shown_monday", None)
        if isinstance(monday, date):
            start = monday.isoformat()
            end = (monday + timedelta(days=6)).isoformat()
            rows = [
                item for item in rows
                if start <= str(item.get("date") or "") <= end
            ]
        month = getattr(self, "query_month", None)
        if isinstance(month, date):
            prefix = month.strftime("%Y-%m")
            rows = [
                item for item in rows
                if str(item.get("date") or "").startswith(prefix)
            ]
        return rows

    def _note_calendar_empty(self, step: dict) -> None:
        """这一周确实没有事件时记一笔空结果。

        同一个月文件已经在本轮写过时不再改成空，避免后面的空周把前面的成功盖掉。

        Args:
            step: 日历提取步骤，用来还原文件名。

        Returns:
            None: 需要时发出 ``file_result``。

        Examples:
            >>> "没有交易事件" != ""
            True
        """
        save_as = str(step.get("save_as") or "交易日历{month_iso}.csv")
        anchor = self.query_month or self.target_date or date.today()
        month = anchor.strftime("%Y-%m")
        save_name = save_as.replace("{month_iso}", month).replace("{month}", month.replace("-", ""))
        save_path = self._resolve_save_path(save_name, f"交易日历{month}.csv")
        if save_path.suffix.lower() != ".csv":
            save_path = save_path.with_suffix(".csv")
        if str(save_path) in self._calendar_saved_paths:
            return
        self._note_file("empty", reason="没有交易事件")

    @staticmethod
    def _save_grid_as_xlsx(rows: list[list[str]], save_path):
        """将二维数组保存为 Excel。"""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Table"
        for ri, row in enumerate(rows, start=1):
            for ci, val in enumerate(row, start=1):
                if val:
                    ws.cell(row=ri, column=ci, value=val)
        WorkflowRunner._save_workbook_versioned(Path(save_path), wb)

    async def _do_extract_echarts(self, step: dict) -> None:
        """提取页面中 ECharts 图表的 option 数据。

        通过 page.evaluate() 调用 echarts.getInstanceByDom 获取图表配置，
        包含 series、xAxis、yAxis 等完整数据。

        字段：
            selector: ECharts 挂载的 DOM 容器选择器
            save_as: 保存文件名，支持 {date} 占位符
            extract_data_only: 仅提取 series 数据（默认 true）
            format: "json"（默认）或 "xlsx"（导出 Excel）
        """
        selector = step.get("selector", ".chart-container")
        save_as = step.get("save_as", "echarts_{date}.json")
        data_only = step.get("extract_data_only", True)
        fmt = step.get("format", "json")

        logger.info("  提取 ECharts: {}", selector)

        js_code = """
        ({selector, dataOnly}) => {
            const results = [];
            const containers = document.querySelectorAll(selector);
            for (const dom of containers) {
                try {
                    const instance = echarts.getInstanceByDom(dom);
                    if (instance) {
                        const option = instance.getOption();
                        if (dataOnly) {
                            const slim = {};
                            if (option.series) slim.series = option.series.map(s => ({
                                name: s.name,
                                type: s.type,
                                data: s.data
                            }));
                            if (option.xAxis) slim.xAxis = option.xAxis;
                            if (option.yAxis) slim.yAxis = option.yAxis;
                            if (option.title) slim.title = option.title;
                            if (option.legend) slim.legend = option.legend;
                            results.push(slim);
                        } else {
                            results.push(option);
                        }
                    } else {
                        results.push({ _error: 'echarts instance not found on dom' });
                    }
                } catch (e) {
                    results.push({ _error: e.message });
                }
            }
            return results;
        }
        """

        try:
            raw = await self.page.evaluate(js_code, {"selector": selector, "dataOnly": data_only})
        except Exception as exc:
            logger.error("  ECharts 提取失败: {}", exc)
            raw = [{"_error": str(exc)}]

        logger.info("  提取到 {} 个图表", len(raw) if isinstance(raw, list) else 0)

        save_path = self._resolve_save_path(save_as, "echarts.json")

        if fmt == "xlsx":
            save_path = save_path.with_suffix(".xlsx")
            self._save_echarts_as_xlsx(raw, save_path)
        else:
            save_path = save_path.with_suffix(".json")
            self._save_versioned_text(save_path, json.dumps(raw, ensure_ascii=False, indent=2))
        errors = [
            str(item.get("_error") or "")
            for item in raw
            if isinstance(item, dict) and "_error" in item
        ] if isinstance(raw, list) else []
        if isinstance(raw, list) and raw and len(errors) == len(raw):
            self._note_file("failed", save_path, reason="；".join(item for item in errors if item)[:300] or "图表提取失败")
        else:
            self._note_file("saved", save_path)
        logger.success("  ECharts 数据保存 → {}", save_path)

    async def _do_intercept_api(self, step: dict) -> None:
        """在页面注入拦截器，捕获后续 XHR/fetch 请求的响应。

        配合 collect_api 使用：先 intercep_api 注入 → 触发页面请求 →
        collect_api 取回数据。

        字段：
            url_pattern: 匹配 URL 的关键字（如 "marketEntitiesNum"）
        """
        url_pattern = step.get("url_pattern", "")

        if not url_pattern:
            logger.error("  intercept_api 缺少 url_pattern")
            return

        js_inject = """
        (pattern) => {
            if (window.__rpa_interceptor_active__) return 'already_active';
            window.__rpa_interceptor_active__ = true;
            window.__rpa_intercepted__ = null;     // 存最后一条，不是数组

            // monkey-patch XMLHttpRequest
            const origOpen = XMLHttpRequest.prototype.open;
            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                this.__rpa_url = url;
                return origOpen.call(this, method, url, ...rest);
            };
            XMLHttpRequest.prototype.send = function(...args) {
                if (this.__rpa_url && this.__rpa_url.indexOf(pattern) !== -1) {
                    this.addEventListener('load', function() {
                        try {
                            window.__rpa_intercepted__ = JSON.parse(this.responseText);
                        } catch(e) {
                            window.__rpa_intercepted__ = { _error: 'parse: ' + e.message };
                        }
                    });
                }
                return origSend.call(this, ...args);
            };

            // monkey-patch fetch
            const origFetch = window.fetch;
            window.fetch = function(url, ...args) {
                const urlStr = typeof url === 'string' ? url : (url.url || '');
                if (urlStr.indexOf(pattern) !== -1) {
                    return origFetch.call(this, url, ...args).then(resp => {
                        return resp.clone().json().then(data => {
                            window.__rpa_intercepted__ = data;  // 直接存最后一条
                            return resp;
                        }).catch(() => resp);
                    });
                }
                return origFetch.call(this, url, ...args);
            };
            return 'injected';
        }
        """
        try:
            result = await self.page.evaluate(js_inject, url_pattern)
            logger.info("  拦截器注入: {}, pattern={}", result, url_pattern)
        except Exception as exc:
            logger.error("  拦截器注入失败: {}", exc)

    async def _do_collect_api(self, step: dict) -> None:
        """取回 intercept_api 拦截到的 API 数据，保存为 Excel。

        字段：
            save_as:      保存文件名
            response_path:响应中数据路径（如 "data.currentPirceVO.details"）
            wait_before:  取回前额外等待 ms（默认 0）
        """
        save_as = step.get("save_as", "collected_{date}.xlsx")
        response_path = step.get("response_path", "")
        wait_ms = step.get("wait_before", 0)

        if wait_ms:
            logger.info("  等待 {}ms...", wait_ms)
            await asyncio.sleep(wait_ms / 1000)

        try:
            data = await self.page.evaluate("() => window.__rpa_intercepted__")
        except Exception as exc:
            logger.error("  取回拦截数据失败: {}", exc)
            data = {"_error": str(exc)}

        if not data:
            logger.warning("  未拦截到任何请求")
            data = {"_error": "no request intercepted"}

        # 按 response_path 提取（纯 dict 路径，如 "currentPirceVO.details"）
        extracted = data
        if response_path and isinstance(extracted, dict) and "_error" not in extracted:
            for key in response_path.split("."):
                if isinstance(extracted, dict):
                    extracted = extracted.get(key, extracted)
                else:
                    break

        # 保存 Excel
        save_path = self._resolve_save_path(save_as, "collected.xlsx")
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path = save_path.with_suffix(".xlsx")

        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Data"

        if isinstance(extracted, dict) and "_error" in extracted:
            ws.cell(row=1, column=1, value=f"Error: {extracted['_error']}")
        elif isinstance(extracted, list) and len(extracted) > 0:
            first = extracted[0]
            if isinstance(first, dict):
                headers = list(first.keys())
                for ci, h in enumerate(headers, start=1):
                    ws.cell(row=1, column=ci, value=h)
                for ri, item in enumerate(extracted, start=2):
                    for ci, h in enumerate(headers, start=1):
                        ws.cell(row=ri, column=ci, value=item.get(h))
            else:
                for ri, val in enumerate(extracted, start=1):
                    ws.cell(row=ri, column=1, value=str(val))
        else:
            ws.cell(row=1, column=1, value=str(extracted))

        self._save_workbook_versioned(save_path, wb)
        if isinstance(extracted, dict) and "_error" in extracted:
            self._note_file("failed", save_path, reason=str(extracted["_error"]))
            logger.error("  拦截数据失败: {}", extracted["_error"])
        else:
            self._note_file("saved", save_path)
            logger.success("  拦截数据保存 → {}", save_path)

    async def _do_api_call(self, step: dict) -> None:
        """通过浏览器 fetch 调用 API（利用当前页面的 cookie/HdKGccbL）。

        字段：
            api_path:  API 路径（如 /portal/xhr/sso-portal/portal/home/marketEntitiesNum）
            save_as:   保存文件名，支持 {date} 占位符
            method:    HTTP 方法，默认 GET
            response_path: 响应 JSON 中数据所在路径，如 "data.currentPirceVO.details"
        """
        api_path = step.get("api_path", "")
        save_as = step.get("save_as", "api_{date}.xlsx")
        method = step.get("method", "GET")
        response_path = step.get("response_path", "")

        if not api_path:
            logger.error("  api_call 缺少 api_path")
            self._note_file("failed", reason="缺少 api_path")
            return

        # 从当前页面 URL / cookies 提取 HdKGccbL
        hdkgccbl = ""
        try:
            page_url = self.page.url
            import re as _re
            m = _re.search(r'HdKGccbL=([^&#]+)', page_url)
            if m:
                hdkgccbl = m.group(1)
                logger.info("  从URL提取 HdKGccbL: {}...", hdkgccbl[:20])
        except Exception:
            pass

        # 如果 URL 里没有，尝试从 cookies 取
        if not hdkgccbl:
            try:
                cookies = await self.context.cookies()
                for c in cookies:
                    if c["name"] == "HdKGccbL":
                        hdkgccbl = c["value"]
                        logger.info("  从Cookie提取 HdKGccbL: {}...", hdkgccbl[:20])
                        break
            except Exception:
                pass

        if not hdkgccbl:
            logger.warning("  未能提取 HdKGccbL，API 调用可能失败")

        full_url = api_path
        if hdkgccbl:
            full_url += ("&" if "?" in api_path else "?") + "HdKGccbL=" + hdkgccbl

        logger.info("  API 调用: {} {}", method, full_url[:80])

        js_code = """
        async ({url, method}) => {
            const resp = await fetch(url, {
                method: method,
                credentials: 'include',
                headers: { 'Accept': 'application/json, text/plain, */*' }
            });
            if (!resp.ok) {
                return { _error: `HTTP ${resp.status}: ${resp.statusText}` };
            }
            return await resp.json();
        }
        """

        try:
            result = await self.page.evaluate(js_code, {"url": full_url, "method": method})
        except Exception as exc:
            logger.error("  API 调用失败: {}", exc)
            result = {"_error": str(exc)}

        # 按 response_path 提取数据
        data = result
        if response_path and isinstance(result, dict) and "_error" not in result:
            for key in response_path.split("."):
                if isinstance(data, dict):
                    data = data.get(key, data)
                else:
                    break

        # 保存为 Excel
        save_path = self._resolve_save_path(save_as, "api.xlsx")
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path = save_path.with_suffix(".xlsx")

        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Data"

        if isinstance(data, dict) and "_error" in data:
            ws.cell(row=1, column=1, value=f"Error: {data['_error']}")
        elif isinstance(data, list) and len(data) > 0:
            # 写入表头（用第一个元素的 keys）
            first = data[0]
            if isinstance(first, dict):
                headers = list(first.keys())
                for ci, h in enumerate(headers, start=1):
                    ws.cell(row=1, column=ci, value=h)
                for ri, item in enumerate(data, start=2):
                    for ci, h in enumerate(headers, start=1):
                        ws.cell(row=ri, column=ci, value=item.get(h))
            else:
                for ri, val in enumerate(data, start=1):
                    ws.cell(row=ri, column=1, value=str(val))
        else:
            ws.cell(row=1, column=1, value=str(data))

        self._save_workbook_versioned(save_path, wb)
        if isinstance(data, dict) and "_error" in data:
            self._note_file("failed", save_path, reason=str(data["_error"]))
            logger.error("  API 数据失败: {}", data["_error"])
        else:
            self._note_file("saved", save_path)
            logger.success("  API 数据保存 → {}", save_path)

    @staticmethod
    def _save_echarts_as_xlsx(raw: list, save_path):
        """将 ECharts option 数据转为 Excel 保存。"""
        from openpyxl import Workbook

        save_path = save_path.with_suffix(".xlsx")
        wb = Workbook()
        # 删除默认 sheet
        wb.remove(wb.active)

        for chart_idx, chart in enumerate(raw):
            if not isinstance(chart, dict) or "_error" in chart:
                continue

            series_list = chart.get("series", [])
            title_text = ""
            if "title" in chart and isinstance(chart["title"], dict):
                title_text = chart["title"].get("text", "")
            elif "title" in chart and isinstance(chart["title"], list):
                title_text = chart["title"][0].get("text", "") if chart["title"] else ""

            # 每个 series 一个 sheet（或全部合并到一个 sheet）
            sheet_name = f"Chart{chart_idx + 1}" if not title_text else title_text[:31]
            ws = wb.create_sheet(title=sheet_name)

            # 收集所有数据列
            all_columns: list[list] = []  # [(header, values), ...]

            for s in series_list:
                name = s.get("name", f"Series{len(all_columns)}")
                raw_data = s.get("data", [])

                if not raw_data:
                    all_columns.append((name, []))
                    continue

                # data 可能是 [val1, val2, ...] 或 [[x1, y1], [x2, y2], ...]
                first = raw_data[0]
                if isinstance(first, (list, tuple)):
                    # 二维数组：第一列是 X 标签，其余是 Y 值
                    xs = [item[0] for item in raw_data]
                    ys = [item[1] if len(item) > 1 else None for item in raw_data]

                    # 只在第一个 series 写入 X 列
                    if chart_idx == 0 and len(all_columns) == 0 and xs:
                        all_columns.append(("时间", xs))
                    all_columns.append((name, ys))
                else:
                    all_columns.append((name, raw_data))

            # 写入表头
            for col_idx, (header, _) in enumerate(all_columns, start=1):
                ws.cell(row=1, column=col_idx, value=header)

            # 写入数据
            max_rows = max((len(vals) for _, vals in all_columns), default=0)
            for row_idx in range(max_rows):
                for col_idx, (_, vals) in enumerate(all_columns, start=1):
                    if row_idx < len(vals):
                        ws.cell(row=row_idx + 2, column=col_idx, value=vals[row_idx])

        # 如果没有创建任何 sheet（全部是 error），放一个 fallback
        if not wb.sheetnames:
            ws = wb.create_sheet(title="Error")
            for i, chart in enumerate(raw):
                if isinstance(chart, dict) and "_error" in chart:
                    ws.cell(row=i + 1, column=1, value=f"Chart {i}: {chart['_error']}")

        WorkflowRunner._save_workbook_versioned(Path(save_path), wb)

    @staticmethod
    def _step_summary(step: dict) -> str:
        action = step.get("action", "?")
        sel = step.get("selector", "")
        url = step.get("url", "")
        if action == "navigate":
            return f"导航 → {url}"
        if action == "click":
            return f"点击 {sel}"
        if action == "export":
            return f"导出 {sel}"
        if action == "wait":
            return f"等待 {sel or str(step.get('ms', '')) + 'ms'}"
        if action == "ensure_login":
            return "检查已有登录；未登录时等待手工登录"
        if action == "login":
            return "登录"
        if action == "repeat":
            start = step.get("start", -1)
            end = step.get("end", -1)
            try:
                label = f"{format_d_offset(int(start))} 至 {format_d_offset(int(end))}"
            except (TypeError, ValueError):
                label = f"{start} 至 {end}"
            return f"按日期循环 {label}"
        if action == "pick_date":
            return f"选择日期 {step.get('value', '?')}"
        if action == "pick_date_range":
            return f"选择日期范围 {step.get('start_value', '?')}~{step.get('end_value', '?')}"
        if action == "extract_table":
            return f"提取表格 {sel}"
        if action == "extract_echarts":
            return f"提取ECharts {sel}"
        if action == "extract_calendar":
            return f"提取交易日历 → {step.get('save_as', '交易日历.csv')}"
        if action == "extract_calendar_month_weeks":
            return f"按月逐周提取交易日历 → {step.get('save_as', '交易日历明细.csv')}"
        if action == "select_calendar_week":
            return "定位目标周"
        if action == "api_call":
            return f"API调用 {step.get('api_path', '?')}"
        if action == "intercept_api":
            return f"拦截器注入 {step.get('url_pattern', '?')}"
        if action == "collect_api":
            return f"取回拦截数据 → {step.get('save_as', '?')}"
        if action == "hover":
            return f"悬停 {sel}"
        if action == "select":
            return f"下拉选择 {sel}"
        if action == "close_tab":
            return f"关闭标签页 #{step.get('index', -1)}"
        if action == "close_other_tabs":
            return "关闭其他标签页"
        if action in ("reset_site_storage", "reset_storage"):
            return f"重置站点存储 {step.get('origin', '当前站点')}"
        return f"{action}"


# ============================================================
# CLI
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="RPA 工作流执行器")
    parser.add_argument("workflow", help="YAML 工作流文件路径")
    parser.add_argument("--download-dir", default="./downloads", help="下载文件保存目录")
    args = parser.parse_args()

    workflow_path = Path(args.workflow)
    if not workflow_path.exists():
        logger.error("工作流文件不存在: {}", workflow_path)
        sys.exit(1)

    config = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    runner = WorkflowRunner(config, download_dir=args.download_dir)

    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
