"""FastAPI 控制面：架构图顶层"统一控制接口"。

暴露给龙虾/Codex 类 Agent 或运维的 HTTP 接口：
- ``POST /api/crawl/start``  触发一次采集
- ``GET  /api/health``       健康检查
- ``GET  /api/tasks``        最近任务列表
- ``GET  /api/tasks/{id}``   单任务详情（item 状态）

业务逻辑全部委托 :class:`ControlService`，本模块只做 HTTP 适配。
fastapi 延迟导入（属 online extra），无依赖环境仍可 import 上层包。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from loguru import logger
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from spider_gd.gateway.control_service import ControlService

from spider_gd.core.station_picker import StationPickError, pick_station
from spider_gd.core.station_profile import (
    mask_station_profiles,
    merge_station_profiles,
    parse_station_profiles,
    profiles_for_window,
    public_station,
)
from spider_gd.core.workflow_outputs import output_specs_or_fallback, workflow_outputs
from spider_gd.interfaces.alert import AlertLevel
from spider_gd.interfaces.storage import ExecutionLogRecord


class CrawlStartRequest(BaseModel):
    target_date: str | None = None
    task_name: str | None = None
    indicators: list[str] | None = None
    data_dir: str | None = None


class WorkflowSaveRequest(BaseModel):
    name: str
    yaml_text: str = ""
    group_id: str | None = None


class DownloadLogRequest(BaseModel):
    download_dir: str = ""


class TaskPathPreviewRequest(BaseModel):
    template: str = ""
    root: str = ""
    exec_date: str = ""
    ssid: str = ""


class TaskStructurePreviewRequest(BaseModel):
    structure: str = ""
    indicators: list[str] = []
    root: str = ""
    exec_date: str = ""
    ssid: str = ""


class BrowserConnectionRequest(BaseModel):
    mode: str = "cdp"
    endpoint: str = "http://127.0.0.1:9222"
    profile_dir: str = ""
    auth_policy: str = "reuse_only"
    target_origin: str = "https://pm.gd.csg.cn"
    success_selector: str = ".ant-menu-horizontal, .el-menu"
    identity_selector: str = ""
    expected_identity: str = ""


class BrowserPageRequest(BaseModel):
    page_id: str


class BrowserRunControlRequest(BaseModel):
    run_id: str
    pause_version: int = 0


_browser_background_tasks: set = set()


class WorkflowRunRequest(BaseModel):
    mode: str = "single"
    cert_password: str = ""
    account: str = ""
    account_password: str = ""
    download_dir: str = ""
    headless: str = "false"
    minimized: str = "false"
    skip_steps: str = ""
    reference_date: str = ""
    slots: list[str] = Field(default_factory=list)


class DateProfileRequest(BaseModel):
    """把日/周/月和默认窗口写进指标 YAML，并整理循环步骤。"""
    yaml_text: str = ""
    date_kind: str = "day"
    start: int = -1
    end: int = -1
    # 导出 key → [start, end] 写到导出步骤上；None 表示跟随指标。不传就不动。
    output_windows: dict[str, list[int] | None] | None = None


class ExecutionLogBatchDeleteRequest(BaseModel):
    exec_ids: list[str] = Field(default_factory=list, max_length=1000)


class DateProfileReadRequest(BaseModel):
    yaml_text: str = ""


def _describe_outputs(cfg: dict, indicator: str) -> list[dict[str, Any]]:
    """导出清单，带是否在日期循环里、指标里给它写的默认范围。"""
    from spider_gd.core.dates import day_loop_output_keys, read_output_windows

    in_loop = set(day_loop_output_keys(cfg, indicator))
    windows = read_output_windows(cfg, indicator)
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "save_as": spec.save_as,
            "action": spec.action,
            "step_index": spec.step_index,
            "position": spec.position,
            "duplicate": spec.duplicate,
            "in_loop": spec.key in in_loop,
            "window": list(windows[spec.key]) if spec.key in windows else None,
        }
        for spec in workflow_outputs(cfg, indicator)
    ]


def _bool_text(value: object) -> bool:
    """把设置里的开关文本读成布尔值。

    Args:
        value: 设置值，常见为 ``true`` / ``false``。

    Returns:
        bool: 打开时为 True。空或无法识别时为 False。

    Examples:
        >>> _bool_text("true")
        True
        >>> _bool_text("false")
        False
    """
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _parse_profile_yaml(text: str) -> dict:
    try:
        cfg = yaml.safe_load(text) if text.strip() else {}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"YAML 语法错误: {exc}") from exc
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="YAML 根节点必须是映射")
    return cfg


class CatchupRunRequest(BaseModel):
    """一次补数的请求体。不写入定时任务。

    存储字段不传时，沿用能覆盖这些指标的定时任务的下载路径和存储结构。
    ``storage_overridden`` 为真时用本次请求里的路径；空结构表示直接放在下载根目录。

    Examples:
        >>> req = CatchupRunRequest(indicators=["日前"], storage_overridden=True, path_structure="")
        >>> req.path_structure
        ''
        >>> req.download_dir
        ''
    """
    login: str = ""
    indicators: list[str] = []
    target_dates: list[str] = []
    target_months: list[str] = []
    indicator_dates: dict[str, list[str]] = {}
    indicator_months: dict[str, list[str]] = {}
    output_dates: dict[str, list[str]] = {}
    cert_password: str = ""
    account: str = ""
    account_password: str = ""
    download_dir: str = ""
    path_template: str | None = None
    path_folders: list[dict[str, Any]] | None = None
    path_structure: str | None = None
    # 为真时即使路径为空，也不再回退到定时任务的存储配置
    storage_overridden: bool = False
    headless: str = "false"
    minimized: str = "true"
    upload_profile_id: str = ""
    auto_upload: bool = False


def _make_wf_loader(service):
    """创建从 DB 加载工作流配置的 loader（供 TaskRunner 使用）。"""
    def _load(wf_id: str):
        cfg = service._ctx.state_store.get_workflow_config(wf_id)
        if cfg and cfg.get("yaml_text"):
            import yaml
            return yaml.safe_load(cfg["yaml_text"]) or {}
        return None
    return _load


def _workflow_outputs_for(service, indicators: list[str]) -> dict:
    """读取指标工作流，返回 {指标: [OutputSpec, ...]}。"""
    result = {}
    for indicator in indicators:
        cfg = service._ctx.state_store.get_workflow_config(indicator)
        parsed = {}
        if cfg and cfg.get("yaml_text"):
            try:
                parsed = yaml.safe_load(cfg["yaml_text"]) or {}
            except Exception:
                parsed = {}
        result[indicator] = output_specs_or_fallback(parsed, indicator)
    return result


def _group_output_dirs(flat_dirs: dict[str, str]) -> dict[str, dict[str, str]]:
    grouped: dict[str, dict[str, str]] = {}
    for canonical, directory in flat_dirs.items():
        indicator, _, output_key = canonical.partition("::")
        grouped.setdefault(indicator, {})[output_key] = directory
    return grouped


def _make_dated_download_dir(base_dir: str) -> str:
    """构建日期分层下载目录: {base}/{YYYY-MM}/{YYYY-MM-DD}/"""
    from datetime import datetime
    today = datetime.now()
    d = Path(base_dir) / today.strftime("%Y-%m") / today.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _current_wifi_name(fallback: str = "") -> str:
    """当前连接的 WiFi 名；读不到时用配置的目标 SSID / 占位。"""
    try:
        from spider_gd.infrastructure.network import WifiConnector

        ssid = WifiConnector().current_ssid()
        if ssid:
            return ssid
    except Exception:
        pass
    return str(fallback or "").strip()


def _resolve_browser_visibility(headless: str = "false", minimized: str = "false") -> tuple[bool, bool]:
    """把用户侧“隐藏运行”统一映射为离屏可见 Chrome。

    目标站点/UKey 登录页在真 headless Chromium 中容易出现元素不可见或按钮等待超时；
    所以控制接口不再把 headless=true 传给浏览器，而是打开一个离屏窗口并禁用后台限速。
    """
    hidden_requested = headless.lower() == "true" or minimized.lower() == "true"
    return False, hidden_requested


def _looks_like_site_unreachable(message: str) -> bool:
    """识别 Playwright/Chrome 网络层不可达错误，避免重复告警。"""
    markers = (
        "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_RESET", "ERR_CONNECTION_FAILED",
        "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
        "ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT",
        "ERR_PROXY_CONNECTION_FAILED", "ERR_ADDRESS_UNREACHABLE",
    )
    return any(marker in message for marker in markers)


def _site_unreachable_message(message: str) -> str:
    if "ERR_CONNECTION_CLOSED" in message or "ERR_CONNECTION_RESET" in message:
        hint = "连接被关闭/重置，常见原因是随身 WiFi 或专网链路中断、网关切换、站点主动断开。"
    elif "ERR_INTERNET_DISCONNECTED" in message or "ERR_ADDRESS_UNREACHABLE" in message:
        hint = "本机网络不可达，请优先检查随身 WiFi 在线状态、网卡连接和专网路由。"
    elif "ERR_NAME_NOT_RESOLVED" in message:
        hint = "域名解析失败，请检查 DNS、专网解析和本机 hosts/路由规则。"
    elif "ERR_PROXY_CONNECTION_FAILED" in message:
        hint = "代理连接失败，请检查代理/VPN/专网代理配置。"
    elif "ERR_TIMED_OUT" in message or "ERR_CONNECTION_TIMED_OUT" in message:
        hint = "连接超时，请检查专网路由、随身 WiFi 信号、目标站点是否维护。"
    else:
        hint = "目标站点不可达，请检查随身 WiFi、专网路由、DNS 和 U-Key/证书服务。"
    return f"⚠️ 目标站点不可达：{hint}\n原始错误：{message}"


def _find_recent_storage_state(base_dir: str) -> str | None:
    """在下载根目录及子目录中查找最近的 storage_state.json。
    
    优先级: 1) 根目录平铺 2) 按修改时间最新的子目录文件
    """
    base = Path(base_dir)
    if not base.exists():
        return None
    
    # 1) 根目录平铺
    flat = base / "storage_state.json"
    if flat.exists():
        return str(flat)
    
    # 2) 搜索所有子目录，按修改时间取最新
    candidates = sorted(
        base.rglob("storage_state.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(candidates[0]) if candidates else None


def _diagnostic_secret_values(store: Any) -> list[str]:
    """从场站、上传配置和 .env 里取出诊断包需要打星的明文。

    Args:
        store: 状态库。

    Returns:
        list[str]: 明文密钥。读失败的来源直接跳过。
    """
    secrets: list[str] = []
    try:
        from spider_gd.core.station_profile import parse_station_profiles

        raw = store.get_setting("stationProfiles") or ""
        for profile in parse_station_profiles(raw):
            secrets.extend([profile.wifi_password, profile.cert_password])
    except Exception:
        pass
    try:
        from spider_gd.gateway.upload_profiles import load_profiles

        for profile in load_profiles(store):
            secrets.append(getattr(profile, "wifi_password", "") or "")
            for header in getattr(profile, "headers", []) or []:
                if getattr(header, "secret", False):
                    secrets.append(getattr(header, "value", "") or "")
            for param in getattr(profile, "params", []) or []:
                if getattr(param, "secret", False):
                    secrets.append(getattr(param, "value", "") or "")
    except Exception:
        pass
    return [item for item in secrets if item and item != "******"]


def _diagnostic_host_facts(service: Any) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """采集网卡、当前 WiFi 和 UKey。任何一项失败都不影响打包。

    Args:
        service: 控制服务，用来读设置里的网卡名。

    Returns:
        tuple[dict, str, dict]: 网卡信息、当前 SSID、UKey 检测结果。
    """
    network: dict[str, Any] = {}
    ssid = ""
    ukey: dict[str, Any] = {}
    try:
        from spider_gd.infrastructure.network.wifi_connector import WifiConnector

        ssid = WifiConnector().current_ssid(
            str(service._ctx.state_store.get_setting("wifiInterfaceName") or "")
        )
    except Exception as exc:
        ssid = ""
        network["wifi_error"] = str(exc)
    try:
        import socket

        network["hostname"] = socket.gethostname()
        network["interfaces"] = socket.getaddrinfo(socket.gethostname(), None)
        network["interfaces"] = sorted({item[4][0] for item in network["interfaces"]})
    except Exception as exc:
        network["interfaces_error"] = str(exc)
    try:
        from spider_gd.infrastructure.network.usb_devices import probe_ukeys

        report = probe_ukeys()
        if isinstance(report, dict):
            ukey = {
                "summary": report.get("summary", ""),
                "distinguishable": report.get("distinguishable", False),
                "device_count": len(report.get("devices") or []),
            }
    except Exception as exc:
        ukey = {"error": str(exc)}
    return network, ssid, ukey


def create_app(
    service: ControlService, *, enable_web_ui: bool = False, enable_mcp: bool = False
) -> Any:
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi import HTTPException
    from fastapi import Body
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse

    # MCP server 需在 FastAPI 构造前创建：streamable_http_app() 返回的 Starlette
    # 子应用自带 lifespan，但 mount 后父应用不会触发子应用 lifespan，
    # 因此必须由父应用 lifespan 接管 session manager 的 run() 生命周期。
    mcp: Any = None
    mcp_app: Any = None
    if enable_mcp:
        from spider_gd.gateway.mcp.server import create_mcp_server

        mcp = create_mcp_server(service)
        mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        if mcp is not None:
            async with mcp.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="spider-gd 控制接口", version="0.1.4", lifespan=lifespan)

    def _sync_browser_download_dir() -> None:
        from spider_gd.core.crawler.shared_browser import get_shared_browser

        saved = service._ctx.state_store.get_all_settings()
        directory = str(saved.get("downloadDir", "")).strip() or str(service._ctx.settings.storage.data_dir)
        get_shared_browser().set_download_dir(directory)

    _sync_browser_download_dir()

    # 本地桌面应用场景：Electron 前端 dev 模式 origin 为 http://localhost:5173，
    # 生产打包后通过 file:// 加载，均需放行 CORS 预检请求。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["null"],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 全局异常处理：记录 500 错误的完整 traceback
    @app.exception_handler(Exception)
    async def _log_unhandled(request: Any, exc: Exception) -> Any:
        from fastapi.responses import JSONResponse

        logger.opt(exception=True).error(
            "HTTP 500 | {} {} | {}",
            request.method, request.url.path, str(exc)
        )
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "type": type(exc).__name__}
        )

    @app.post("/api/crawl/start")
    async def crawl_start(req: CrawlStartRequest) -> dict[str, Any]:
        return await service.start_crawl(target_date=req.target_date, task_name=req.task_name, indicators=req.indicators, data_dir=req.data_dir)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return await service.health()

    @app.get("/api/schedule/jobs")
    async def list_schedule_jobs() -> dict[str, Any]:
        """列出所有已注册的调度任务（调试用）。"""
        try:
            jobs = await service._ctx.scheduler.list_jobs()
            return {
                "count": len(jobs),
                "jobs": [{"job_id": j.job_id, "cron": j.cron, "enabled": j.enabled} for j in jobs],
            }
        except Exception as e:
            return {"error": str(e), "jobs": []}

    @app.post("/api/schedule/reload")
    async def reload_schedule() -> dict[str, Any]:
        """重新加载调度任务（从 SQLite task_configs 重新读取并注册）。"""
        from spider_gd.app import register_jobs as _register_jobs

        ctx = service._ctx
        try:
            # 清掉旧任务
            existing = await ctx.scheduler.list_jobs()
            for j in existing:
                await ctx.scheduler.remove_job(j.job_id)
            # 重新注册
            ids = await _register_jobs(ctx)
            return {"status": "ok", "jobs": ids}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @app.get("/api/logs/recent")
    async def recent_logs(lines: int = 100, task: str = "") -> dict[str, Any]:
        """返回最近的日志行。task 参数可筛选特定任务的日志。"""
        from pathlib import Path as _Path

        log_dir = _Path(service._ctx.settings.log.file_dir).resolve()
        log_file = log_dir / "app.log"
        if not log_file.exists():
            return {"lines": [], "message": "日志文件不存在"}

        try:
            # 读取最后 N 行（大文件用反向读取）
            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            # 按 task 筛选
            if task:
                all_lines = [l for l in all_lines if f"| {task: <16} |" in l]

            recent = all_lines[-lines:]
            return {"lines": [l.rstrip("\n") for l in recent], "total": len(all_lines)}
        except Exception as e:
            return {"lines": [], "error": str(e)}

    @app.get("/api/logs/path")
    async def log_path() -> dict[str, Any]:
        """返回日志目录绝对路径（供 Electron 打开文件管理器）。"""
        from pathlib import Path as _Path

        log_dir = _Path(service._ctx.settings.log.file_dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)  # 确保存在
        return {"path": str(log_dir)}

    @app.get("/api/logs/stats")
    async def log_stats() -> dict[str, Any]:
        """返回日志目录统计（总大小、文件数等）。"""
        from spider_gd.config.logging import get_log_stats

        return get_log_stats(service._ctx.settings.log.file_dir)

    @app.post("/api/alert/test")
    async def alert_test() -> dict[str, Any]:
        return await service.test_alert()

    @app.get("/api/tasks")
    async def list_tasks(limit: int = 20) -> dict[str, Any]:
        return {"tasks": service.list_tasks(limit)}

    @app.get("/api/tasks/list")
    async def list_tasks_api() -> list[dict[str, Any]]:
        """返回任务配置列表（从 tasks/ YAML 文件）。"""
        tasks_root = _tasks_dir()
        if not tasks_root.exists():
            return []
        result = []
        for f in sorted(tasks_root.glob("*.yml")):
            try:
                cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception:
                cfg = {}
            result.append({
                "name": f.stem,
                "label": cfg.get("name", f.stem),
                "description": cfg.get("description", ""),
                "login": cfg.get("login", ""),
                "indicators": cfg.get("indicators", []),
                "stop_on_error": cfg.get("stop_on_error", True),
            })
        return result

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        return service.get_task(task_id)

    # ---- 工作流（指标配置）CRUD ----
    _WORKFLOWS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "workflows"
    # PyInstaller 环境下 __file__ 在 _MEIPASS 里，用 CWD 兜底
    if not _WORKFLOWS_DIR.exists():
        import os as _os2
        _WORKFLOWS_DIR = Path(_os2.getcwd()) / "workflows"
    _continue_events: dict[str, dict] = {}  # name -> {"event": asyncio.Event, "cancel": [bool]}
    _stop_flags: dict[str, list[bool]] = {}  # name -> [bool] 用于 step-stream 的中止信号

    def _wf_path(name: str) -> Path:
        from spider_gd.config.user_home import resolve_workflow_file, user_workflows_dir

        found = resolve_workflow_file(name)
        if found is not None:
            return found
        safe = re.sub(r"[^\w\-.]", "_", name)
        folder = user_workflows_dir(create=False)
        if not folder.exists():
            folder = _WORKFLOWS_DIR
        return folder / f"{safe}.yml"

    def _workflow_meta(parsed: dict[str, Any]) -> dict[str, Any]:
        import sys
        tools_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from workflow_types import infer_meta
        return infer_meta(parsed)

    def _workflow_display_name(workflow_id: str) -> str:
        store = service._ctx.state_store
        workflow = store.get_workflow_config(workflow_id)
        if not workflow:
            return workflow_id
        groups = {group["id"]: group for group in store.list_workflow_groups()}
        parts = [workflow["name"]]
        parent = workflow.get("group_id")
        seen: set[str] = set()
        while parent and parent in groups and parent not in seen:
            seen.add(parent)
            group = groups[parent]
            parts.insert(0, group["name"])
            parent = group.get("parent_id")
        return " / ".join(parts)

    @app.get("/api/workflows")
    async def list_workflows() -> list[dict[str, Any]]:
        """从 SQLite 获取工作流列表。"""
        configs = service._ctx.state_store.list_workflow_configs()
        result = []
        for c in configs:
            steps_count = 0
            profile = {"kind": "day", "start": -1, "end": -1, "has_repeat": False}
            output_mode = ""
            output_name = ""
            outputs: list[dict[str, Any]] = []
            raw = None
            try:
                raw = yaml.safe_load(c.get("yaml_text", "") or "")
                steps_count = len(raw.get("steps", [])) if isinstance(raw, dict) else 0
                if isinstance(raw, dict):
                    from spider_gd.core.dates import read_date_profile
                    profile = read_date_profile(raw)
                    steps = [s for s in (raw.get("steps") or []) if isinstance(s, dict)]
                    actions = [s.get("action") for s in steps]
                    has_export = "export" in actions
                    extract_actions = {
                        "extract", "extract_table", "extract_echarts", "extract_calendar",
                    }
                    has_extract = any(action in extract_actions for action in actions)
                    if has_export and has_extract:
                        output_mode = "mixed"
                    elif has_export:
                        output_mode = "export"
                    elif has_extract:
                        output_mode = "extract"
                    save_actions = {"export", *extract_actions}
                    for step in reversed(steps):
                        if step.get("action") in save_actions and step.get("save_as"):
                            output_name = str(step.get("save_as"))
                            break
                    outputs = _describe_outputs(raw, c["id"])
            except Exception:
                pass
            result.append({
                "name": c["id"],
                "label": c["name"],
                "group_id": c.get("group_id"),
                "type": c.get("type", "indicator"),
                "needs_login": c.get("needs_login", True),
                "start_from": c.get("start_from", "dashboard"),
                "description": "",
                "steps_count": steps_count,
                "date_kind": profile["kind"],
                "window_start": profile["start"],
                "window_end": profile["end"],
                "has_repeat": profile["has_repeat"],
                "output_mode": output_mode,
                "output_name": output_name,
                "outputs": outputs,
            })
        return result

    def _validate_workflow_parent(parent_id: str | None, moving_group: str | None = None) -> None:
        if not parent_id:
            return
        groups = {g["id"]: g for g in service._ctx.state_store.list_workflow_groups()}
        if parent_id not in groups:
            raise HTTPException(status_code=400, detail="上级分组不存在")
        seen = {moving_group} if moving_group else set()
        node = parent_id
        while node:
            if node in seen:
                raise HTTPException(status_code=400, detail="不能移动到自身或子分组")
            seen.add(node)
            node = groups[node].get("parent_id")

    def _check_sibling_name(name: str, parent_id: str | None, kind: str, exclude: str | None = None) -> None:
        store = service._ctx.state_store
        peers = store.list_workflow_groups() if kind == "group" else store.list_workflow_configs()
        for peer in peers:
            if peer["id"] != exclude and peer.get("parent_id" if kind == "group" else "group_id") == parent_id and peer["name"].casefold() == name.casefold() and (kind == "group" or peer.get("type") == kind):
                raise HTTPException(status_code=409, detail="同一上级下已有同名项目")

    @app.get("/api/workflow-groups")
    async def list_workflow_groups() -> list[dict[str, Any]]:
        return service._ctx.state_store.list_workflow_groups()

    @app.post("/api/workflow-groups")
    async def create_workflow_group(body: dict[str, Any]) -> dict[str, Any]:
        from uuid import uuid4
        name = str(body.get("name", "")).strip()
        parent_id = body.get("parent_id") or None
        if not name:
            raise HTTPException(status_code=400, detail="分组名称不能为空")
        _validate_workflow_parent(parent_id)
        _check_sibling_name(name, parent_id, "group")
        group_id = "group_" + uuid4().hex
        service._ctx.state_store.save_workflow_group(group_id, name, parent_id)
        return {"id": group_id, "name": name, "parent_id": parent_id}

    @app.put("/api/workflow-groups/{group_id}")
    async def update_workflow_group(group_id: str, body: dict[str, Any]) -> dict[str, Any]:
        old = service._ctx.state_store.get_workflow_group(group_id)
        if not old:
            raise HTTPException(status_code=404, detail="分组不存在")
        name = str(body.get("name", old["name"])).strip()
        parent_id = body.get("parent_id") or None
        if not name:
            raise HTTPException(status_code=400, detail="分组名称不能为空")
        _validate_workflow_parent(parent_id, group_id)
        _check_sibling_name(name, parent_id, "group", group_id)
        service._ctx.state_store.save_workflow_group(group_id, name, parent_id)
        return {"status": "ok"}

    @app.delete("/api/workflow-groups/{group_id}")
    async def delete_workflow_group(group_id: str) -> dict[str, Any]:
        store = service._ctx.state_store
        if not store.get_workflow_group(group_id):
            raise HTTPException(status_code=404, detail="分组不存在")
        if any(g.get("parent_id") == group_id for g in store.list_workflow_groups()) or any(c.get("group_id") == group_id for c in store.list_workflow_configs()):
            raise HTTPException(status_code=409, detail="分组不为空，请先移动或删除子项")
        store.delete_workflow_group(group_id)
        return {"status": "ok"}

    @app.patch("/api/workflows/{name}/meta")
    async def update_workflow_meta(name: str, body: dict[str, Any]) -> dict[str, Any]:
        store = service._ctx.state_store
        cfg = store.get_workflow_config(name)
        if not cfg:
            raise HTTPException(status_code=404, detail="工作流不存在")
        label = str(body.get("label", cfg["name"])).strip()
        group_id = body.get("group_id") or None
        if not label:
            raise HTTPException(status_code=400, detail="名称不能为空")
        _validate_workflow_parent(group_id)
        _check_sibling_name(label, group_id, cfg["type"], name)
        cfg.update(name=label, group_id=group_id)
        store.save_workflow_config(cfg)
        return {"status": "ok"}

    @app.post("/api/workflows/date-profile")
    async def apply_workflow_date_profile(body: DateProfileRequest) -> dict[str, Any]:
        """按日/月和 D 窗口整理指标配置。不直接落盘，由编辑器保存。"""
        from spider_gd.core.dates import (
            apply_date_profile,
            read_date_profile,
            render_workflow_yaml,
            set_output_windows,
        )

        cfg = _parse_profile_yaml(body.yaml_text)
        kind = body.date_kind if body.date_kind in {"day", "week", "month"} else "day"
        if body.output_windows is not None:
            # 先按当前 YAML 的 key 写，再整理循环；按位置命名的 key 整理后会变
            cfg = set_output_windows(cfg, {
                key: (tuple(value[:2]) if value and len(value) >= 2 else None)
                for key, value in body.output_windows.items()
            })
        try:
            updated = apply_date_profile(cfg, kind, body.start, body.end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        profile = read_date_profile(updated)
        return {
            "yaml_text": render_workflow_yaml(body.yaml_text, updated),
            "steps": updated.get("steps") or [],
            "kind": profile["kind"],
            "start": profile["start"],
            "end": profile["end"],
            "has_repeat": profile["has_repeat"],
            "outputs": _describe_outputs(updated, str(updated.get("name") or "")),
        }

    @app.post("/api/workflows/date-profile/read")
    async def read_workflow_date_profile(body: DateProfileReadRequest) -> dict[str, Any]:
        """读出当前编辑中的 YAML 的日期配置和各导出的默认范围，不改写。"""
        from spider_gd.core.dates import read_date_profile

        cfg = _parse_profile_yaml(body.yaml_text)
        profile = read_date_profile(cfg)
        return {
            "kind": profile["kind"],
            "start": profile["start"],
            "end": profile["end"],
            "has_repeat": profile["has_repeat"],
            "outputs": _describe_outputs(cfg, str(cfg.get("name") or "")),
        }

    @app.get("/api/workflows/export-all")
    async def export_all_workflows() -> Any:
        """导出全部工作流为 ZIP（每个工作流一个 .yml 文件）。"""
        import zipfile as _zipfile
        import tempfile as _tmp
        import json as _json
        from fastapi.responses import FileResponse
        from starlette.background import BackgroundTask

        configs = service._ctx.state_store.list_workflow_configs()
        if not configs and not service._ctx.state_store.list_workflow_groups():
            raise HTTPException(status_code=404, detail="没有可导出的工作流")

        tmp_zip = Path(_tmp.gettempdir()) / f"workflows_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        with _zipfile.ZipFile(str(tmp_zip), "w", _zipfile.ZIP_DEFLATED) as zf:
            manifest = {"version": 1, "groups": service._ctx.state_store.list_workflow_groups(), "workflows": []}
            for index, cfg in enumerate(configs):
                filename = f"workflows/{index:04d}.yml"
                zf.writestr(filename, cfg.get("yaml_text", ""))
                manifest["workflows"].append({"id": cfg["id"], "name": cfg["name"], "group_id": cfg.get("group_id"), "file": filename})
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2))

        return FileResponse(
            str(tmp_zip),
            media_type="application/zip",
            filename="workflows_export.zip",
            background=BackgroundTask(tmp_zip.unlink, missing_ok=True),
        )

    @app.post("/api/workflows/import")
    async def import_workflows(body: dict[str, Any]) -> dict[str, Any]:
        """批量导入工作流。body: { "files": [{"name": "...", "yaml_text": "..."}] }"""
        files = body.get("files", [])
        if not isinstance(files, list) or len(files) == 0:
            raise HTTPException(status_code=400, detail="请提供至少一个文件")

        imported: list[str] = []
        errors: list[str] = []
        for f in files:
            name = f.get("name", "")
            yaml_text = f.get("yaml_text", "")
            if not name or not yaml_text:
                errors.append("跳过: 缺少 name 或 yaml_text")
                continue
            try:
                cfg = yaml.safe_load(yaml_text) or {}
                meta = _workflow_meta(cfg)
                label = cfg.get("name", name)
                _check_sibling_name(label, None, meta.get("type", "indicator"), name)
                service._ctx.state_store.save_workflow_config({
                    "id": name,
                    "name": label,
                    "type": meta.get("type", "indicator"),
                    "needs_login": meta.get("needs_login", True),
                    "start_from": meta.get("start_from", "dashboard"),
                    "yaml_text": yaml_text,
                    "group_id": None,
                })
                imported.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        return {"status": "ok", "imported": imported, "errors": errors}

    @app.post("/api/workflows/import-archive")
    async def import_workflow_archive(body: dict[str, Any]) -> dict[str, Any]:
        import base64
        import io
        import json
        import zipfile
        try:
            raw = base64.b64decode(body.get("data", ""), validate=True)
            if len(raw) > 20_000_000:
                raise ValueError("ZIP 超过 20 MB")
            archive = zipfile.ZipFile(io.BytesIO(raw))
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("version") != 1:
                raise ValueError("不支持的清单版本")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"无效的工作流 ZIP：{exc}") from exc
        store = service._ctx.state_store
        groups = manifest.get("groups", [])
        pending = {g["id"]: g for g in groups}
        while pending:
            ready = [g for g in pending.values() if not g.get("parent_id") or g["parent_id"] not in pending]
            if not ready:
                raise HTTPException(status_code=400, detail="分组层级有循环")
            for group in ready:
                parent_id = group.get("parent_id")
                _validate_workflow_parent(parent_id, group["id"])
                _check_sibling_name(group["name"], parent_id, "group", group["id"])
                store.save_workflow_group(group["id"], group["name"], parent_id)
                del pending[group["id"]]
        imported = []
        errors = []
        for item in manifest.get("workflows", []):
            try:
                path = item["file"]
                if not path.startswith("workflows/") or ".." in path:
                    raise ValueError("无效的文件路径")
                yaml_text = archive.read(path).decode("utf-8")
                parsed = yaml.safe_load(yaml_text) or {}
                meta = _workflow_meta(parsed)
                parent_id = item.get("group_id")
                _validate_workflow_parent(parent_id)
                _check_sibling_name(item["name"], parent_id, meta["type"], item["id"])
                store.save_workflow_config({"id": item["id"], "name": item["name"],
                    "type": meta["type"], "needs_login": meta["needs_login"],
                    "start_from": meta["start_from"], "yaml_text": yaml_text,
                    "group_id": parent_id})
                imported.append(item["id"])
            except Exception as exc:
                errors.append(f"{item.get('id', '?')}: {exc}")
        return {"status": "ok", "imported": imported, "errors": errors}

    @app.get("/api/workflows/{name}")
    async def get_workflow(name: str) -> dict[str, Any]:
        cfg = service._ctx.state_store.get_workflow_config(name)
        if not cfg:
            raise HTTPException(status_code=404, detail="工作流不存在")
        raw = cfg.get("yaml_text", "")
        parsed = yaml.safe_load(raw) or {}
        return {
            "name": name,
            "label": cfg["name"],
            "group_id": cfg.get("group_id"),
            "type": cfg.get("type", "indicator"),
            "needs_login": cfg.get("needs_login", True),
            "start_from": cfg.get("start_from", "dashboard"),
            "description": parsed.get("description", ""),
            "steps": parsed.get("steps", []),
            "yaml_text": raw,
        }

    @app.put("/api/workflows/{name}")
    async def save_workflow(name: str, req: WorkflowSaveRequest) -> dict[str, Any]:
        text = req.yaml_text
        if not text or not text.strip():
            raise HTTPException(status_code=400, detail="YAML 内容不能为空")
        try:
            parsed = yaml.safe_load(text)
            if not isinstance(parsed, dict):
                raise ValueError("YAML 必须是字典")
        except Exception as e:
            logger.warning("YAML 语法错误 ({}): {}", name, e)
            raise HTTPException(status_code=400, detail=f"YAML 语法错误: {e}")

        import sys as _sys2
        _tools2 = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        if str(_tools2) not in _sys2.path:
            _sys2.path.insert(0, str(_tools2))
        from workflow_types import infer_meta
        meta = infer_meta(parsed)
        # 更新时保留已有类型，避免 YAML 中误删 type 字段导致登录变指标
        existing = service._ctx.state_store.get_workflow_config(name)
        if existing:
            if not parsed.get("type"):
                meta["type"] = existing.get("type", meta["type"])
            if parsed.get("needs_login") is None:
                meta["needs_login"] = existing.get("needs_login", meta["needs_login"])
            if not parsed.get("start_from"):
                meta["start_from"] = existing.get("start_from", meta["start_from"])
        group_id = existing.get("group_id") if existing else req.group_id
        _validate_workflow_parent(group_id)
        label = existing["name"] if existing else parsed.get("name", req.name or name)
        _check_sibling_name(label, group_id, meta["type"], name)
        service._ctx.state_store.save_workflow_config({
            "id": name,
            "name": label,
            "type": meta["type"],
            "needs_login": meta["needs_login"],
            "start_from": meta["start_from"],
            "yaml_text": text,
            "group_id": group_id,
        })
        logger.info("工作流已保存: {} ({} bytes)", name, len(text))
        return {"status": "ok", "name": name}

    @app.delete("/api/workflows/{name}")
    async def delete_workflow(name: str) -> dict[str, Any]:
        used_by = [task["name"] for task in service._ctx.state_store.list_task_configs()
                   if task.get("login") == name or name in task.get("indicators", [])]
        if used_by:
            raise HTTPException(status_code=409, detail="已被任务引用：" + "、".join(used_by))
        ok = service._ctx.state_store.delete_workflow_config(name)
        if not ok:
            raise HTTPException(status_code=404, detail="工作流不存在")
        return {"status": "ok"}

    @app.post("/api/workflows/{name}/run")
    async def run_workflow(name: str) -> dict[str, Any]:
        """通过 WorkflowRunner 执行指定工作流。"""
        cfg = service._ctx.state_store.get_workflow_config(name)
        if not cfg:
            raise HTTPException(status_code=404, detail="工作流不存在")
        try:
            import sys
            _tools = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
            if str(_tools) not in sys.path:
                sys.path.insert(0, str(_tools))
            from workflow_runner import WorkflowRunner
            parsed = yaml.safe_load(cfg.get("yaml_text", "") or "")
            runner = WorkflowRunner(parsed, download_dir=str(service._ctx.settings.storage.data_dir))
            await runner.run()
            if runner.failed:
                return {
                    "status": "error",
                    "name": name,
                    "error": runner.failure_reason or "工作流执行失败",
                }
            return {"status": "ok", "name": name}
        except Exception as exc:
            logger.error("工作流执行失败 {}: {}", name, exc)
            return {"status": "error", "name": name, "error": str(exc)}

    @app.post("/api/workflows/{name}/record")
    async def record_workflow(name: str, site_url: str = "") -> dict[str, Any]:
        """启动录制器：打开 Chrome 浏览器，用户在浏览器中操作，停止后自动更新工作流 YAML。"""
        import subprocess as _sp
        import tempfile as _tmp
        import uuid as _uuid

        # 获取工作流配置
        cfg = service._ctx.state_store.get_workflow_config(name)
        if cfg:
            wf_name = cfg.get("name", name)
            parsed = yaml.safe_load(cfg.get("yaml_text", "") or "") or {}
            needs_login = parsed.get("needs_login", True)
        else:
            # 回退 YAML 文件
            path = _wf_path(name)
            if not path.exists():
                raise HTTPException(status_code=404, detail="工作流不存在")
            raw = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(raw) or {}
            wf_name = parsed.get("name", name)
            needs_login = parsed.get("needs_login", True)

        # 起始 URL：优先用前端传的 site_url，否则用系统配置的 platform.domain
        if site_url:
            start_url = site_url
        else:
            domain = service._ctx.settings.platform.domain
            start_url = f"https://{domain}/portal/#/home"

        # 临时输出文件
        tmp_path = Path(_tmp.gettempdir()) / f"spider_record_{_uuid.uuid4().hex[:8]}.yml"

        # 确定 tools 目录和 Python 解释器
        _tools_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        recorder_script = _tools_dir / "record_workflow.py"
        if not recorder_script.exists():
            raise HTTPException(status_code=500, detail=f"录制脚本不存在: {recorder_script}")

        python_exe = sys.executable if hasattr(sys, 'executable') else "python"

        logger.info(
            "🎬 启动录制: workflow={} needs_login={} url={} tmp={}",
            name, needs_login, start_url, tmp_path,
        )

        hint = (
            "浏览器已打开，请手动登录后再操作指标页面。"
            if needs_login
            else "浏览器已打开，请直接操作指标页面。"
        )

        try:
            # 同步等待录制完成（这是一个长时间阻塞调用）
            from spider_gd.infrastructure.proc import run_capture as _run_capture

            proc = await asyncio.to_thread(
                _run_capture,
                [
                    python_exe, str(recorder_script),
                    "--url", start_url,
                    "-o", str(tmp_path),
                    "-n", wf_name,
                ],
                timeout=600,  # 10 分钟超时
                cwd=str(_tools_dir.parent),  # 项目根目录作为 CWD
            )

            stderr = proc.stderr or ""
            if proc.returncode != 0:
                logger.error("录制失败 (code={}): stderr={}", proc.returncode, stderr[-500:])
                return {
                    "status": "error",
                    "message": f"录制进程异常退出 (code={proc.returncode})",
                    "hint": hint,
                    "stderr": stderr[-500:],
                }

            logger.info("录制进程退出 OK, 读取输出: {}", tmp_path)

        except TimeoutError:
            logger.error("录制超时 (10min)")
            return {"status": "error", "message": "录制超时（10 分钟）", "hint": hint}
        except Exception as exc:
            logger.error("录制异常: {}", exc)
            return {"status": "error", "message": f"录制失败: {exc}", "hint": hint}

        # 读取录制输出的 YAML
        if not tmp_path.exists():
            return {"status": "error", "message": "录制未生成输出文件", "hint": hint}

        try:
            recorded_yaml_text = tmp_path.read_text(encoding="utf-8")
            recorded_cfg = yaml.safe_load(recorded_yaml_text) or {}
            steps = recorded_cfg.get("steps", [])
        except Exception as exc:
            logger.error("读取录制输出失败: {}", exc)
            return {"status": "error", "message": f"读取录制输出失败: {exc}"}
        finally:
            # 清理临时文件
            try:
                tmp_path.unlink()
            except Exception:
                pass

        if not steps:
            return {"status": "error", "message": "录制结果为空，没有捕获到任何步骤"}

        # 合并回原工作流：保留 name/type/needs_login/start_from，只替换 steps
        parsed["steps"] = steps
        new_yaml_text = yaml.dump(parsed, allow_unicode=True, default_flow_style=False, sort_keys=False)

        # 保存到 DB
        _tools_dir2 = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        if str(_tools_dir2) not in sys.path:
            sys.path.insert(0, str(_tools_dir2))
        from workflow_types import infer_meta
        meta = infer_meta(parsed)
        service._ctx.state_store.save_workflow_config({
            "id": name,
            "name": parsed.get("name", wf_name),
            "type": meta["type"],
            "needs_login": meta["needs_login"],
            "start_from": meta["start_from"],
            "yaml_text": new_yaml_text,
        })

        logger.success("✅ 录制完成并保存: {} ({} 步骤)", name, len(steps))
        return {
            "status": "ok",
            "name": name,
            "steps_count": len(steps),
            "yaml_text": new_yaml_text,
            "hint": hint,
        }

    @app.get("/api/workflows/{name}/run/stream")
    async def run_workflow_stream(name: str, cert_password: str = "", account: str = "", account_password: str = "", download_dir: str = "", headless: str = "false", minimized: str = "false"):
        """SSE 流式执行工作流，推送每一步进度。"""
        import asyncio as _asyncio
        import sys as _sys
        import uuid as _uuid
        import time as _time

        _tools = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        if str(_tools) not in _sys.path:
            _sys.path.insert(0, str(_tools))
        from workflow_runner import WorkflowRunner

        # 从 DB 加载工作流
        db_cfg = service._ctx.state_store.get_workflow_config(name)
        if not db_cfg or not db_cfg.get("yaml_text"):
            # 回退 YAML 文件
            path = _wf_path(name)
            if not path.exists():
                raise HTTPException(status_code=404, detail="工作流不存在")
            raw = path.read_text(encoding="utf-8")
        else:
            raw = db_cfg["yaml_text"]
        for placeholder, val in [("${CERT_PASSWORD}", cert_password), ("${ACCOUNT}", account), ("${ACCOUNT_PASSWORD}", account_password)]:
            if val:
                raw = raw.replace(placeholder, val)
        cfg = yaml.safe_load(raw) or {}

        # 创建执行日志记录
        now_ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        exec_id = f"wf_{_uuid.uuid4().hex[:12]}"
        service._ctx.state_store.create_execution_log(
            ExecutionLogRecord(
                exec_id=exec_id, name=_workflow_display_name(name),
                type="workflow", target_id=name,
                status="running", reason="",
                created_at=now_ts, updated_at=now_ts,
            )
        )
        _log_final_status: list[str] = ["running"]
        _site_unreachable_alerted: list[bool] = [False]

        def _fmt_event(data: dict) -> str:
            """将 SSE 事件格式化为可读日志行。"""
            evt = data.get("event", "")
            if evt == "browser_start":
                return "▶ 浏览器启动"
            elif evt == "step_start":
                s, t = data.get("step", "?"), data.get("total", "?")
                act = data.get("action", "")
                summary = data.get("summary", "")
                return f"⏳ 步骤 {s}/{t}: {act} → {summary}"
            elif evt == "step_done":
                s = data.get("step", "?")
                return f"✅ 步骤 {s} 完成"
            elif evt == "step_error":
                s = data.get("step", "?")
                err = data.get("error", "")
                return f"❌ 步骤 {s} 失败: {err}"
            elif evt == "workflow_done":
                return f"🏁 工作流完成: {data.get('name', name)}"
            elif evt == "export_saved":
                return f"📁 导出已保存: {data.get('path', '')}（浏览器原始文件名: {data.get('original_name', '')}）"
            elif evt == "calendar_saved":
                return (
                    f"📅 交易日历已保存: {data.get('path', '')}"
                    f"（{data.get('month', '')}，{data.get('rows', '?')} 行）"
                )
            elif evt == "workflow_error":
                err = data.get("error", "")
                return f"💥 工作流异常: {err}"
            elif evt == "aborted":
                msg = data.get("message", "")
                return f"⛔ 执行中止: {msg}"
            elif evt == "site_unreachable":
                msg = data.get("message", "")
                return f"🌐 站点不可达: {msg}"
            elif evt == "captcha_failed":
                msg = data.get("error", "")
                shot = data.get("screenshot", "")
                return f"🧩 验证码失败: {msg}" + (f" 截图: {shot}" if shot else "")
            elif evt == "api_fallback":
                status = data.get("status", "")
                url = data.get("url", "")
                path = data.get("path", "")
                err = data.get("error", "")
                if status == "success":
                    return f"🔁 API降级成功: {path}"
                if status == "error":
                    return f"🔁 API降级失败: {url} {err}"
                return f"🔁 API降级尝试: {url}"
            elif evt == "waiting_input":
                msg = data.get("message", "")
                return f"⏸ 等待用户操作: {msg}"
            elif evt == "error_pause":
                msg = data.get("message", "") or data.get("error", "")
                return f"⛔ 错误暂停: {msg}"
            else:
                return f"📌 {evt}: {json.dumps({k: v for k, v in data.items() if k != 'event'}, ensure_ascii=False)}"

        # needs_login 表示必须核验现有会话；WorkflowRunner.run 会在执行前检查。
        # 登录由独立登录工作流或任务的 login 阶段完成，指标播放不重放登录表单。

        dl_dir = (
            download_dir
            or str(service._ctx.state_store.get_setting("downloadDir") or "")
            or str(service._ctx.settings.storage.data_dir)
        )
        Path(dl_dir).mkdir(parents=True, exist_ok=True)
        
        # 接管会话复用已有 Context，不再用 storage_state 重建浏览器上下文。

        is_headless, is_minimized = _resolve_browser_visibility(headless, minimized)
        if is_minimized:
            logger.info("隐藏运行已切换为离屏可见 Chrome，避免真 headless 下目标元素不可见")
        cfg["_control_name"] = name
        runner = WorkflowRunner(
            cfg,
            download_dir=dl_dir,
            headless=is_headless,
            minimized=is_minimized,
        )

        event_queue: _asyncio.Queue = _asyncio.Queue()
        # 注册人工介入事件
        cont_evt: _asyncio.Event = _asyncio.Event()
        cont_result: list[bool] = [True]
        _continue_events[name] = {"event": cont_evt, "cancel": cont_result}

        def _on_event(data: dict):
            try:
                event_queue.put_nowait(data)
            except Exception:
                pass
            # 同时写入执行日志（忽略写入错误以防阻塞主流程）
            try:
                line = _fmt_event(data)
                service._ctx.state_store.append_execution_log_detail(exec_id, line)
                evt = data.get("event", "")
                if evt in ("error_pause", "waiting_input"):
                    _log_final_status[:] = ["paused", data.get("error") or data.get("message") or "等待处理"]
                    service._ctx.state_store.update_execution_log(exec_id, status="paused",
                        reason=_log_final_status[1], updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"))
                elif evt == "resumed":
                    _log_final_status[:] = ["running"]
                    service._ctx.state_store.update_execution_log(exec_id, status="running",
                        reason="", updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"))

                # 告警：步骤 / 工作流错误 / 站点不可达 / 错误弹窗暂停
                if evt in ("step_error", "workflow_error", "site_unreachable", "captcha_failed", "error_pause"):
                    err = data.get("message", "") if evt == "site_unreachable" else ""
                    err = err or data.get("error", "") or data.get("message", "")
                    should_alert = True
                    if evt in ("step_error", "task_error") and _site_unreachable_alerted[0] and _looks_like_site_unreachable(err):
                        should_alert = False
                    if evt == "site_unreachable":
                        _site_unreachable_alerted[0] = True
                    if should_alert:
                        step_no = data.get("step", "?")
                        now_ts = _time.strftime("%Y-%m-%d %H:%M:%S")
                        title = "spider-gd 站点不可达" if evt == "site_unreachable" else "spider-gd 采集异常"
                        alert_text = (
                            f"## ⚠️ {title}\n\n"
                            f"- 工作流: {name}\n"
                            f"- 步骤: {step_no}\n"
                            f"- 错误: {err[:500]}\n"
                            f"- 时间: {now_ts}"
                        )
                        _asyncio.create_task(
                            service._ctx.alert.send_alert(
                                AlertLevel.ERROR, alert_text, event_type="error"
                            )
                        )

                # 跟踪最终状态
                if evt in ("workflow_done",):
                    if _log_final_status[0] == "running":
                        _log_final_status[0] = "success"
                elif evt in ("workflow_error", "aborted", "site_unreachable", "captcha_failed"):
                    already_failed = _log_final_status[0] == "failed"
                    _log_final_status[0] = "failed"
                    if already_failed and len(_log_final_status) > 1:
                        return
                    if evt == "workflow_error":
                        _log_final_status.append(data.get("error", "")[:200])
                    elif evt == "aborted":
                        _log_final_status.append(data.get("message", "")[:200])
                    elif evt == "site_unreachable":
                        _log_final_status.append((data.get("error", "") or data.get("message", ""))[:200])
                    elif evt == "captcha_failed":
                        _log_final_status.append((data.get("error", "") or data.get("message", ""))[:200])
                    elif evt == "error_pause":
                        _log_final_status.append((data.get("error", "") or data.get("message", ""))[:200])
            except Exception:
                pass

        async def _wait_continue() -> bool:
            cont_evt.clear()
            event_queue.put_nowait({"event": "waiting_input", "message": "等待用户操作..."})
            await cont_evt.wait()
            return cont_result[0]

        async def _execute_and_finalize():
            try:
                await runner.run(on_event=_on_event, wait_continue=_wait_continue)
            except Exception as exc:
                _on_event({"event": "workflow_error", "error": str(exc)})
            finally:
                # 更新执行日志最终状态
                final_status = _log_final_status[0]
                reason = _log_final_status[1] if len(_log_final_status) > 1 else ""
                now_ts2 = _time.strftime("%Y-%m-%d %H:%M:%S")
                service._ctx.state_store.update_execution_log(
                    exec_id, status=final_status, reason=reason, updated_at=now_ts2,
                )
                _continue_events.pop(name, None)

        async def _event_generator():
            task = _asyncio.create_task(_execute_and_finalize())
            _browser_background_tasks.add(task)
            task.add_done_callback(_browser_background_tasks.discard)
            while True:
                try:
                    data = await _asyncio.wait_for(event_queue.get(), timeout=0.5)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    if data.get("event") in ("workflow_done", "aborted", "workflow_error"):
                        break
                except _asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    if task.done():
                        while not event_queue.empty():
                            data = event_queue.get_nowait()
                            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                        break
            await _asyncio.shield(task)

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/workflows/{name}/run/continue")
    async def continue_workflow(name: str):
        """发送继续信号给正在运行的 SSE 工作流。"""
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        shared = get_shared_browser()
        state = shared.status()
        active = state.get("run")
        if state["busy"] and active and active["name"] == name:
            try:
                shared.resume(active["id"], active["pause_version"])
                return {"status": "ok"}
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
        entry = _continue_events.get(name)
        if entry:
            entry["cancel"][0] = True
            entry["event"].set()
            return {"status": "ok"}
        return {"status": "error", "message": "没有正在等待的工作流"}

    @app.post("/api/workflows/{name}/run/stop")
    async def stop_workflow(name: str):
        """中止正在运行的 SSE 工作流。"""
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        shared = get_shared_browser()
        state = shared.status()
        active = state.get("run")
        if state["busy"] and active and active["name"] == name:
            try:
                shared.cancel(active["id"])
                return {"status": "ok"}
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
        entry = _continue_events.get(name)
        if entry:
            entry["cancel"][0] = False
            entry["event"].set()
        _stop_flags[name] = [True]
        if entry:
            return {"status": "ok"}
        # 也检查 step-stream
        if name in _stop_flags:
            return {"status": "ok"}
        return {"status": "error", "message": "没有正在等待的工作流"}

    @app.post("/api/tasks/{name}/run/continue")
    async def continue_task(name: str):
        """发送继续信号给正在运行的 SSE 任务。"""
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        shared = get_shared_browser()
        state = shared.status()
        active = state.get("run")
        if state["busy"] and active and active["name"] == name:
            try:
                shared.resume(active["id"], active["pause_version"])
                return {"status": "ok"}
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
        entry = _continue_events.get(name)
        if entry:
            entry["cancel"][0] = True
            entry["event"].set()
            return {"status": "ok"}
        return {"status": "error", "message": "没有正在等待的任务"}

    @app.post("/api/tasks/{name}/run/stop")
    async def stop_task(name: str):
        """中止正在运行的 SSE 任务。"""
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        shared = get_shared_browser()
        state = shared.status()
        active = state.get("run")
        if state["busy"] and active and active["name"] == name:
            try:
                shared.cancel(active["id"])
                return {"status": "ok"}
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
        entry = _continue_events.get(name)
        if entry:
            entry["cancel"][0] = False
            entry["event"].set()
            return {"status": "ok"}
        return {"status": "error", "message": "没有正在等待的任务"}

    # ---- 任务配置 REST API（SQLite 存储） ----
    @app.get("/api/task-configs")
    async def list_task_configs() -> list[dict[str, Any]]:
        """返回所有任务配置（从 SQLite）。"""
        return service._ctx.state_store.list_task_configs()

    @app.get("/api/task-configs/export-all")
    async def export_all_task_configs() -> Any:
        """导出全部任务配置为 ZIP（每个任务一个 .json 文件）。"""
        import json as _json
        import zipfile as _zipfile
        import tempfile as _tmp
        from fastapi.responses import FileResponse
        from starlette.background import BackgroundTask

        configs = service._ctx.state_store.list_task_configs()
        if not configs:
            raise HTTPException(status_code=404, detail="没有可导出的任务配置")

        tmp_zip = Path(_tmp.gettempdir()) / f"tasks_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        with _zipfile.ZipFile(str(tmp_zip), "w", _zipfile.ZIP_DEFLATED) as zf:
            for cfg in configs:
                filename = f"{cfg['id']}.json"
                export_data = {
                    "id": cfg["id"],
                    "name": cfg["name"],
                    "login": cfg.get("login", ""),
                    "indicators": cfg.get("indicators", []),
                    "repeat": cfg.get("repeat", "daily"),
                    "time": cfg.get("time", "09:00"),
                    "weekdays": cfg.get("weekdays", []),
                    "backfill_days": cfg.get("backfill_days", 1),
                    "cover_days": cfg.get("cover_days", 0),
                    "enabled": cfg.get("enabled", True),
                    "remark": cfg.get("remark", ""),
                    "download_dir": cfg.get("download_dir", ""),
                    "download_dir_custom": cfg.get("download_dir_custom", False),
                    "path_template": cfg.get("path_template", ""),
                    "path_folders": cfg.get("path_folders") or [],
                    "path_structure": cfg.get("path_structure", ""),
                    "indicator_windows": cfg.get("indicator_windows") or {},
                    "output_windows": cfg.get("output_windows") or {},
                    "upload_profile_id": cfg.get("upload_profile_id", ""),
                    "auto_upload": bool(cfg.get("auto_upload", False)),
                }
                zf.writestr(filename, _json.dumps(export_data, ensure_ascii=False, indent=2))

        return FileResponse(
            str(tmp_zip),
            media_type="application/zip",
            filename="task_configs_export.zip",
            background=BackgroundTask(tmp_zip.unlink, missing_ok=True),
        )

    @app.post("/api/task-configs/import")
    async def import_task_configs(body: dict[str, Any]) -> dict[str, Any]:
        """批量导入任务配置。body: { "files": [{"id": "...", "name": "...", ...}] }"""
        files = body.get("files", [])
        if not isinstance(files, list) or len(files) == 0:
            raise HTTPException(status_code=400, detail="请提供至少一个任务配置")

        imported: list[str] = []
        errors: list[str] = []
        for f in files:
            cfg_id = f.get("id", "")
            if not cfg_id or not f.get("name"):
                errors.append("跳过: 缺少 id 或 name")
                continue
            try:
                service._ctx.state_store.save_task_config({
                    "id": cfg_id,
                    "name": f["name"],
                    "login": f.get("login", ""),
                    "indicators": f.get("indicators", []),
                    "repeat": f.get("repeat", "daily"),
                    "time": f.get("time", "09:00"),
                    "weekdays": f.get("weekdays", []),
                    "backfill_days": f.get("backfill_days", 1),
                    "cover_days": f.get("cover_days", 0),
                    "enabled": f.get("enabled", True),
                    "remark": f.get("remark", ""),
                    "download_dir": f.get("download_dir", ""),
                    "download_dir_custom": f.get("download_dir_custom", False),
                    "path_template": f.get("path_template", ""),
                    "path_folders": f.get("path_folders") or [],
                    "path_structure": f.get("path_structure", ""),
                    "indicator_windows": f.get("indicator_windows") or {},
                    "output_windows": f.get("output_windows") or {},
                    "upload_profile_id": f.get("upload_profile_id", ""),
                    "auto_upload": bool(f.get("auto_upload", False)),
                })
                imported.append(cfg_id)
            except Exception as exc:
                errors.append(f"{cfg_id}: {exc}")

        # 刷新调度器
        try:
            from spider_gd.app import register_jobs as _register_jobs
            existing = await service._ctx.scheduler.list_jobs()
            for j in existing:
                if j.job_id.startswith("task_"):
                    await service._ctx.scheduler.remove_job(j.job_id)
            await _register_jobs(service._ctx)
        except Exception:
            pass

        return {"status": "ok", "imported": imported, "errors": errors}

    @app.post("/api/task-configs/preview-path")
    async def preview_task_path(req: TaskPathPreviewRequest) -> dict[str, Any]:
        """把任务存储路径模板渲染成预览（和实际执行共用同一套规则）。"""
        from datetime import date as _date

        from spider_gd.core.output_paths import render_task_path

        root = req.root.strip() or str(service._ctx.settings.storage.data_dir)
        try:
            when = _date.fromisoformat(req.exec_date) if req.exec_date else datetime.now().date()
        except ValueError:
            return {"path": "", "relative": "", "segments": [], "error": "执行日期格式错误"}
        ssid = req.ssid.strip()
        try:
            relative = render_task_path(req.template, when, ssid)
        except ValueError as exc:
            return {"path": "", "relative": "", "segments": [], "error": str(exc)}
        target = Path(root) / relative
        return {
            "path": str(target),
            "relative": relative.as_posix(),
            "segments": list(relative.parts),
            "error": "",
        }

    @app.post("/api/task-configs/preview-structure")
    async def preview_task_structure(req: TaskStructurePreviewRequest) -> dict[str, Any]:
        """解析 YAML 风格存储结构，返回每个指标的基础目录。"""
        from datetime import date as _date

        from spider_gd.core.path_structure import build_structure_dirs, resolve_structure, save_as_conflicts

        root = req.root.strip() or str(service._ctx.settings.storage.data_dir)
        try:
            when = _date.fromisoformat(req.exec_date) if req.exec_date else datetime.now().date()
        except ValueError:
            return {"error": "执行日期格式错误", "base": "", "assignments": [], "unassigned": [], "invalid": [],
                    "conflicts": []}
        indicators = [str(item) for item in req.indicators]
        specs_by_indicator = _workflow_outputs_for(service, indicators)
        spec_lookup = {
            (spec.indicator, spec.key): spec
            for specs in specs_by_indicator.values()
            for spec in specs
        }
        try:
            result = resolve_structure(
                req.structure,
                indicators,
                outputs=specs_by_indicator,
                ssid=req.ssid.strip(),
                when=when,
            )
            base, dirs = build_structure_dirs(root, result)
        except ValueError as exc:
            return {"error": str(exc), "base": "", "assignments": [], "unassigned": [], "invalid": [], "conflicts": []}

        assignments = []
        for canonical, parts in result.assignments.items():
            indicator, _, output_key = canonical.partition("::")
            spec = spec_lookup.get((indicator, output_key))
            assignments.append({
                "indicator": indicator,
                "output_key": output_key,
                "output_label": spec.label if spec else output_key,
                "folder": "/".join(parts),
                "path": dirs[canonical],
            })

        unassigned = []
        for canonical in result.unassigned:
            indicator, _, output_key = canonical.partition("::")
            spec = spec_lookup.get((indicator, output_key))
            unassigned.append({
                "indicator": indicator,
                "output_key": output_key,
                "label": spec.label if spec else output_key,
            })

        return {
            "error": "",
            "base": base,
            "assignments": assignments,
            "unassigned": unassigned,
            "invalid": result.invalid,
            # 同名 save_as 落到同一目录才算冲突；前端只标红这些
            "conflicts": save_as_conflicts(result, specs_by_indicator),
        }

    @app.get("/api/task-configs/{config_id}")
    async def get_task_config(config_id: str) -> dict[str, Any]:
        cfg = service._ctx.state_store.get_task_config(config_id)
        if not cfg:
            raise HTTPException(status_code=404, detail="任务配置不存在")
        return cfg

    @app.put("/api/task-configs/{config_id}")
    async def save_task_config(config_id: str, body: dict[str, Any]) -> dict[str, Any]:
        body["id"] = config_id
        service._ctx.state_store.save_task_config(body)

        # 自动刷新调度
        try:
            from spider_gd.app import register_jobs as _register_jobs

            existing = await service._ctx.scheduler.list_jobs()
            for j in existing:
                if j.job_id.startswith("task_"):
                    await service._ctx.scheduler.remove_job(j.job_id)
            await _register_jobs(service._ctx)
        except Exception:
            pass  # 不阻塞保存

        return {"status": "ok", "id": config_id}

    @app.delete("/api/task-configs/{config_id}")
    async def delete_task_config(config_id: str) -> dict[str, Any]:
        ok = service._ctx.state_store.delete_task_config(config_id)
        if not ok:
            raise HTTPException(status_code=404, detail="任务配置不存在")

        # 移除对应的调度任务
        try:
            await service._ctx.scheduler.remove_job(f"task_{config_id}")
        except Exception:
            pass

        return {"status": "ok"}
    # ---- 任务 YAML 执行（保留兼容） ----
    from spider_gd.config.user_home import tasks_dir as _tasks_dir

    def _task_file(name: str) -> Path:
        return _tasks_dir() / f"{name}.yml"

    @app.put("/api/tasks/{name}")
    async def save_task(name: str, req: WorkflowSaveRequest) -> dict[str, Any]:
        text = req.yaml_text
        if not text or not text.strip():
            raise HTTPException(status_code=400, detail="任务内容不能为空")
        try:
            cfg = yaml.safe_load(text)
            if not isinstance(cfg, dict):
                raise ValueError("YAML 必须是字典")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"YAML 语法错误: {e}")
        path = _task_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("任务已保存: {}", path)
        return {"status": "ok", "name": name}

    @app.delete("/api/tasks/{name}")
    async def delete_task(name: str) -> dict[str, Any]:
        path = _task_file(name)
        if path.exists():
            path.unlink()
            logger.info("任务已删除: {}", path)
        return {"status": "ok"}

    async def run_task_stream(name: str, cert_password: str = "", account: str = "", account_password: str = "", download_dir: str = "", headless: str = "false", minimized: str = "false", reference_date: str = "", prepared: dict | None = None):
        """SSE 流式执行任务（登录 + 多个指标）。"""
        import asyncio as _asyncio
        import sys as _sys
        import uuid as _uuid
        import time as _time

        _tools = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        if str(_tools) not in _sys.path:
            _sys.path.insert(0, str(_tools))
        from task_runner import TaskRunner

        cfg = {}
        if prepared is not None:
            cfg = dict(prepared)
            name = str(cfg.get("_control_name") or name)
        else:
            task_path = _task_file(name)
            db_cfg = None
            try:
                db_cfg = service._ctx.state_store.get_task_config(name)
            except Exception:
                db_cfg = None
            if not task_path.exists() and not db_cfg:
                raise HTTPException(status_code=404, detail="任务不存在")

            if task_path.exists():
                raw = task_path.read_text(encoding="utf-8")
                for placeholder, val in [("${CERT_PASSWORD}", cert_password), ("${ACCOUNT}", account), ("${ACCOUNT_PASSWORD}", account_password)]:
                    if val:
                        raw = raw.replace(placeholder, val)
                cfg = yaml.safe_load(raw) or {}
            if db_cfg:
                cfg.update({
                    "name": db_cfg.get("name", cfg.get("name", name)),
                    "login": db_cfg.get("login", cfg.get("login", "")),
                    "indicators": db_cfg.get("indicators", cfg.get("indicators", [])),
                    "stop_on_error": db_cfg.get("stop_on_error", cfg.get("stop_on_error", True)),
                    "start_page": db_cfg.get("start_page", cfg.get("start_page", "https://pm.gd.csg.cn/portal/#/home")),
                    "backfill_days": db_cfg.get("backfill_days", cfg.get("backfill_days", 1)),
                    "cover_days": db_cfg.get("cover_days", cfg.get("cover_days", 0)),
                    "indicator_windows": db_cfg.get("indicator_windows") or cfg.get("indicator_windows") or {},
                    "output_windows": db_cfg.get("output_windows") or cfg.get("output_windows") or {},
                    "path_template": db_cfg.get("path_template", cfg.get("path_template", "")),
                    "path_folders": db_cfg.get("path_folders") or cfg.get("path_folders") or [],
                    "path_structure": db_cfg.get("path_structure", cfg.get("path_structure", "")),
                    "download_dir": db_cfg.get("download_dir", cfg.get("download_dir", "")),
                    "download_dir_custom": db_cfg.get("download_dir_custom", False),
                    "upload_profile_id": db_cfg.get("upload_profile_id", ""),
                    "auto_upload": bool(db_cfg.get("auto_upload", False)),
                    "indicator_retry": db_cfg.get("indicator_retry", cfg.get("indicator_retry", 3)),
                })
            if reference_date.strip():
                cfg["reference_date"] = reference_date.strip()

        # 创建执行日志记录
        now_ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        exec_id = f"tsk_{_uuid.uuid4().hex[:12]}"
        resuming = bool(prepared and prepared.get("_resume_from"))
        if resuming:
            control_name = str((prepared or {}).get("_control_name") or name)
            log_type = "catchup" if control_name == "catchup" else "scheduled_manual"
        else:
            log_type = "catchup" if prepared is not None else "scheduled_manual"
        service._ctx.state_store.create_execution_log(
            ExecutionLogRecord(
                exec_id=exec_id, name=cfg.get("name", name),
                type=log_type, target_id=name,
                status="running", reason="",
                created_at=now_ts, updated_at=now_ts,
            )
        )
        _log_final_status: list[str] = ["running"]

        def _fmt_task_event(data: dict) -> str:
            """格式化任务/工作流事件为可读日志行。"""
            evt = data.get("event", "")
            if evt == "task_step":
                phase = data.get("phase", "")
                step = data.get("step", "?")
                total = data.get("total", "?")
                nm = data.get("name", "")
                status = data.get("status", "")
                msg = data.get("message", "")
                icon = "🔑" if phase == "login" else "📊"
                label = "登录" if phase == "login" else nm
                if phase != "login" and data.get("target_date"):
                    label = f"{label}({data.get('target_date')})"
                if status == "running":
                    return f"{icon} [{step}/{total}] {label}: {msg}"
                elif status == "done":
                    return f"✅ [{step}/{total}] {label} 完成"
                elif status == "error":
                    return f"❌ [{step}/{total}] {label} 失败: {msg}"
                elif status == "skip":
                    return f"⏭ [{step}/{total}] {label}: 跳过"
                return f"{icon} [{step}/{total}] {label}: {status}"
            elif evt == "task_window":
                return f"📅 日期窗口 {data.get('index', '?')}/{data.get('total', '?')}: {data.get('date', '')}"
            elif evt == "repeat_date":
                return f"  📅 {data.get('summary') or data.get('date', '')}"
            elif evt == "task_done":
                return f"🏁 任务完成: {data.get('name', name)}"
            elif evt == "task_error":
                err = data.get("error", "")
                return f"💥 任务异常: {err}"
            elif evt == "aborted":
                msg = data.get("message", "")
                return f"⛔ 任务中止: {msg}"
            # 透传工作流内部事件
            elif evt == "browser_start":
                return "▶ 浏览器启动"
            elif evt == "step_start":
                s, t = data.get("step", "?"), data.get("total", "?")
                act = data.get("action", "")
                summary = data.get("summary", "")
                return f"  ⏳ 步骤 {s}/{t}: {act} → {summary}"
            elif evt == "step_done":
                s = data.get("step", "?")
                return f"  ✅ 步骤 {s} 完成"
            elif evt == "step_error":
                s = data.get("step", "?")
                err = data.get("error", "")
                return f"  ❌ 步骤 {s} 失败: {err}"
            elif evt == "workflow_done":
                return f"  🏁 工作流完成: {data.get('name', '?')}"
            elif evt == "site_unreachable":
                msg = data.get("message", "")
                return f"  🌐 站点不可达: {msg}"
            elif evt == "captcha_failed":
                msg = data.get("error", "")
                shot = data.get("screenshot", "")
                return f"  🧩 验证码失败: {msg}" + (f" 截图: {shot}" if shot else "")
            elif evt == "api_fallback":
                status = data.get("status", "")
                url = data.get("url", "")
                path = data.get("path", "")
                err = data.get("error", "")
                if status == "success":
                    return f"  🔁 API降级成功: {path}"
                if status == "error":
                    return f"  🔁 API降级失败: {url} {err}"
                return f"  🔁 API降级尝试: {url}"
            elif evt == "waiting_input":
                msg = data.get("message", "")
                return f"⏸ 等待用户操作: {msg}"
            elif evt == "error_pause":
                msg = data.get("message", "") or data.get("error", "")
                return f"⛔ 错误暂停: {msg}"
            else:
                return f"📌 {evt}: {json.dumps({k: v for k, v in data.items() if k != 'event'}, ensure_ascii=False)}"

        from spider_gd.app import ensure_dual_nic_ready, network_preflight_label

        net_ok, net_message, net_details = ensure_dual_nic_ready(service._ctx)
        net_label = network_preflight_label(net_message, net_details)
        try:
            service._ctx.state_store.append_execution_log_detail(exec_id, f"🌐 {net_label}: {net_message}")
            for line in net_details[:10]:
                service._ctx.state_store.append_execution_log_detail(exec_id, f"  {line}")
        except Exception:
            pass
        if not net_ok:
            reason = (net_message or f"{net_label}失败")[:200]
            service._ctx.state_store.update_execution_log(
                exec_id,
                status="failed",
                reason=reason,
                updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            try:
                await service._ctx.alert.send_alert(
                    AlertLevel.ERROR,
                    f"spider-gd {net_label}失败\n任务：{cfg.get('name', name)}\n原因：{reason}\n"
                    + "\n".join(net_details[:5]),
                    event_type="error",
                )
            except Exception:
                pass

            async def _network_failed_stream():
                yield f"data: {json.dumps({'event': 'network_preflight', 'ok': False, 'message': net_message, 'details': net_details}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'task_error', 'error': reason}, ensure_ascii=False)}\n\n"

            return StreamingResponse(
                _network_failed_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        from spider_gd.core.output_paths import choose_download_root

        base_dir = str(choose_download_root(
            download_dir,
            str(cfg.get("download_dir") or "") if cfg.get("download_dir_custom") else "",
            str(service._ctx.state_store.get_setting("downloadDir") or ""),
            str(service._ctx.settings.storage.data_dir),
        ))
        wifi_name = _current_wifi_name(
            str(service._ctx.state_store.get_setting("wifiSsid") or "")
        )
        structure = str(cfg.get("path_structure") or "").strip()
        task_indicators = [str(item) for item in (cfg.get("indicators") or [])]
        output_dirs: dict[str, dict[str, str]] = {}
        structure_issues: list[str] = []
        if structure:
            from spider_gd.core.path_structure import (
                build_structure_dirs,
                resolve_structure,
                structure_warnings,
            )

            task_outputs = _workflow_outputs_for(service, task_indicators)
            try:
                result = resolve_structure(
                    structure,
                    task_indicators,
                    outputs=task_outputs,
                    ssid=wifi_name,
                    when=datetime.now().date(),
                )
                dl_dir, flat_dirs = build_structure_dirs(base_dir, result)
                output_dirs = _group_output_dirs(flat_dirs)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"任务存储结构配置错误: {exc}"
                ) from exc
            structure_issues = structure_warnings(result, task_outputs)
            # 结构配置优先；不让我旧的 path_folders 再叠加一层。
            cfg["path_folders"] = []
            Path(dl_dir).mkdir(parents=True, exist_ok=True)
        else:
            template = str(cfg.get("path_template") or "").strip()
            if template:
                from spider_gd.core.output_paths import resolve_task_output_dir

                try:
                    dl_dir = str(resolve_task_output_dir(
                        base_dir, template, datetime.now().date(), ssid=wifi_name,
                    ))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"任务存储路径配置错误: {exc}") from exc
                Path(dl_dir).mkdir(parents=True, exist_ok=True)
            else:
                dl_dir = base_dir
                Path(dl_dir).mkdir(parents=True, exist_ok=True)
        from spider_gd.app import _snapshot_download_files
        if structure_issues:
            for line in structure_issues:
                logger.warning("任务 {}：{}", name, line)
                with contextlib.suppress(Exception):
                    service._ctx.state_store.append_execution_log_detail(exec_id, f"⚠️ {line}")
            with contextlib.suppress(Exception):
                await service._ctx.alert.send_alert(
                    AlertLevel.WARN,
                    f"spider-gd 存储结构需要处理\n任务：{cfg.get('name', name)}\n"
                    + "\n".join(structure_issues[:5]),
                    event_type="error",
                )
        download_snapshot = _snapshot_download_files(dl_dir)
        is_headless, is_minimized = _resolve_browser_visibility(headless, minimized)
        if is_minimized:
            logger.info("任务隐藏运行已切换为离屏可见 Chrome，避免真 headless 下目标元素不可见")
        cfg["_control_name"] = name
        cfg["_exec_id"] = exec_id
        if prepared is not None and prepared.get("_resume_from"):
            cfg["_resume_from"] = prepared.get("_resume_from")
            cfg["_skip_slots"] = list(prepared.get("_skip_slots") or [])
            if prepared.get("_only_slots"):
                cfg["_only_slots"] = list(prepared.get("_only_slots") or [])
        from task_retry import remember_run_plan

        remember_run_plan(service._ctx.state_store, exec_id, cfg)
        station_queue: list[dict[str, str]] = []
        multi_station = _bool_text(service._ctx.state_store.get_setting("multiStationEnabled"))
        if multi_station:
            from spider_gd.core.station_picker import StationPickError, choose_task_stations
            from spider_gd.core.station_profile import parse_station_profiles
            from spider_gd.infrastructure.network.usb_devices import list_usb_devices

            try:
                saved_profiles = parse_station_profiles(
                    service._ctx.state_store.get_setting("stationProfiles") or ""
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                usb_devices = await _asyncio.to_thread(list_usb_devices)
            except Exception as exc:
                logger.warning("读取 UKey 失败，任务按原证书密码继续: {}", exc)
                usb_devices = []
            plugged_ukeys = [
                (item.device_id, item.label) for item in usb_devices if item.ukey_candidate
            ]
            try:
                chosen = await _asyncio.to_thread(
                    choose_task_stations,
                    saved_profiles,
                    plugged_ukeys,
                    fallback_cert_password=cert_password,
                )
            except StationPickError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if chosen is None:
                raise HTTPException(status_code=409, detail="已取消选择场站")
            station_queue = chosen
        runner = TaskRunner(cfg, download_dir=dl_dir, headless=is_headless,
                          minimized=is_minimized,
                          workflow_loader=_make_wf_loader(service),
                          cert_password=cert_password,
                          account=account,
                          account_password=account_password,
                          output_dirs=output_dirs,
                          station_queue=station_queue,
                          exec_id=exec_id,
                          slot_store=service._ctx.state_store)

        event_queue: _asyncio.Queue = _asyncio.Queue()
        cont_evt: _asyncio.Event = _asyncio.Event()
        cont_result: list[bool] = [True]
        _continue_events[name] = {"event": cont_evt, "cancel": cont_result}

        # 告警上下文跟踪
        _current_phase = ""
        _current_target = name
        _current_action = ""
        _current_summary = ""
        _site_unreachable_alerted: list[bool] = [False]

        # ── 事件回调 ──
        def _on_event(data: dict):
            nonlocal _current_phase, _current_target, _current_action, _current_summary
            try:
                event_queue.put_nowait(data)
            except Exception:
                pass
            try:
                line = _fmt_task_event(data)
                service._ctx.state_store.append_execution_log_detail(exec_id, line)
                evt = data.get("event", "")
                if evt in ("error_pause", "waiting_input"):
                    _log_final_status[:] = ["paused", data.get("error") or data.get("message") or "等待处理"]
                    service._ctx.state_store.update_execution_log(exec_id, status="paused",
                        reason=_log_final_status[1], updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"))
                elif evt == "resumed":
                    _log_final_status[:] = ["running"]
                    service._ctx.state_store.update_execution_log(exec_id, status="running",
                        reason="", updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"))

                # 跟踪当前阶段上下文
                if evt == "task_step" and data.get("status") == "running":
                    _current_phase = data.get("phase", "")
                    _current_target = data.get("name", name)
                elif evt == "step_start":
                    _current_action = data.get("action", "")
                    _current_summary = data.get("summary", "")

                # 告警：步骤错误 / 任务错误 / 站点不可达 / 错误弹窗暂停
                if evt in ("step_error", "task_error", "site_unreachable", "captcha_failed", "error_pause"):
                    err = data.get("message", "") if evt == "site_unreachable" else ""
                    err = err or data.get("error", "") or data.get("message", "")
                    should_alert = True
                    if evt in ("step_error", "task_error") and _site_unreachable_alerted[0] and _looks_like_site_unreachable(err):
                        should_alert = False
                    if evt == "site_unreachable":
                        _site_unreachable_alerted[0] = True
                    if should_alert:
                        step_no = data.get("step", "?")
                        now_ts = _time.strftime("%Y-%m-%d %H:%M:%S")
                        title = "spider-gd 站点不可达" if evt == "site_unreachable" else "spider-gd 采集异常"
                        alert_text = (
                            f"## ⚠️ {title}\n\n"
                            f"- 任务: {name}\n"
                            f"- 阶段: {_current_phase or '?'}\n"
                            f"- 目标: {_current_target or '?'}\n"
                            f"- 步骤: {step_no}\n"
                            f"- 错误: {err[:500]}\n"
                            f"- 时间: {now_ts}"
                        )
                        _asyncio.create_task(
                            service._ctx.alert.send_alert(
                                AlertLevel.ERROR, alert_text, event_type="error"
                            )
                        )

                if evt in ("task_done",):
                    failed_count = int(data.get("failed") or 0)
                    if failed_count:
                        _log_final_status[:] = [
                            "failed",
                            str(data.get("message") or "部分指标失败")[:200],
                        ]
                    elif _log_final_status[0] == "running":
                        _log_final_status[0] = "success"
                elif evt in ("task_error", "aborted", "site_unreachable", "captcha_failed"):
                    already_failed = _log_final_status[0] == "failed"
                    _log_final_status[0] = "failed"
                    if already_failed and len(_log_final_status) > 1:
                        return
                    if evt == "task_error":
                        _log_final_status.append(data.get("error", "")[:200])
                    elif evt == "aborted":
                        _log_final_status.append(data.get("message", "")[:200])
                    elif evt == "site_unreachable":
                        _log_final_status.append((data.get("error", "") or data.get("message", ""))[:200])
                    elif evt == "captcha_failed":
                        _log_final_status.append((data.get("error", "") or data.get("message", ""))[:200])
                    elif evt == "error_pause":
                        _log_final_status.append((data.get("error", "") or data.get("message", ""))[:200])
            except Exception:
                pass

        async def _wait_continue() -> bool:
            cont_evt.clear()
            event_queue.put_nowait({"event": "waiting_input", "message": "等待用户操作..."})
            await cont_evt.wait()
            return cont_result[0]

        async def _execute_and_finalize():
            try:
                await runner.run(on_event=_on_event, wait_continue=_wait_continue)
            except Exception as exc:
                _on_event({"event": "task_error", "error": str(exc)})
            finally:
                final_status = _log_final_status[0]
                reason = _log_final_status[1] if len(_log_final_status) > 1 else ""
                try:
                    from spider_gd.app import deliver_task_runner_downloads

                    zip_path = await deliver_task_runner_downloads(
                        service._ctx,
                        exec_id=exec_id,
                        task_config_id=name,
                        task_name=str(cfg.get("name", name)),
                        download_dir=dl_dir,
                        before=download_snapshot,
                        task_status=final_status,
                        reason=reason,
                    )
                    if zip_path:
                        service._ctx.state_store.append_execution_log_detail(
                            exec_id,
                            f"📦 稳定发送压缩包: {zip_path}",
                        )
                except Exception as delivery_exc:
                    service._ctx.state_store.append_execution_log_detail(
                        exec_id,
                        f"⚠️ 稳定发送失败，已进入补发队列: {delivery_exc}",
                    )
                try:
                    from spider_gd.gateway.data_upload_flow import maybe_upload_after_run

                    def _emit_upload(data: dict) -> None:
                        try:
                            event_queue.put_nowait(data)
                        except Exception:
                            pass

                    await maybe_upload_after_run(
                        service._ctx.state_store,
                        profile_id=str(cfg.get("upload_profile_id") or ""),
                        auto_upload=bool(cfg.get("auto_upload")),
                        exec_id=exec_id,
                        download_dir=str(dl_dir),
                        before=download_snapshot,
                        interface_name=str(service._ctx.state_store.get_setting("wifiInterfaceName") or ""),
                        on_event=_emit_upload,
                    )
                except Exception as upload_exc:
                    service._ctx.state_store.append_execution_log_detail(
                        exec_id,
                        f"☁️ 上传异常: {upload_exc}",
                    )
                now_ts2 = _time.strftime("%Y-%m-%d %H:%M:%S")
                service._ctx.state_store.update_execution_log(
                    exec_id, status=final_status, reason=reason, updated_at=now_ts2,
                )
                _continue_events.pop(name, None)

        async def _event_generator():
            task = _asyncio.create_task(_execute_and_finalize())
            _browser_background_tasks.add(task)
            task.add_done_callback(_browser_background_tasks.discard)
            terminal_events = {"task_done", "task_error", "aborted"}
            seen_terminal = False

            def _render(data: dict) -> str:
                return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            while True:
                try:
                    data = await _asyncio.wait_for(event_queue.get(), timeout=0.5)
                    yield _render(data)
                    event_name = data.get("event")
                    if event_name in terminal_events:
                        seen_terminal = True
                    if event_name == "upload_settled":
                        break
                    if seen_terminal and task.done() and event_queue.empty():
                        break
                except _asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    if task.done():
                        while not event_queue.empty():
                            data = event_queue.get_nowait()
                            yield _render(data)
                            if data.get("event") == "upload_settled":
                                break
                        break
            await _asyncio.shield(task)
            while not event_queue.empty():
                yield _render(event_queue.get_nowait())

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # ---- 执行日志 API ----

    @app.get("/api/execution-logs")
    async def list_execution_logs(
        page: int = 1, page_size: int = 20, type: str = "", status: str = "",
        start_time: str = "", end_time: str = "",
    ) -> dict[str, Any]:
        """分页查询执行日志列表。"""
        logs = service._ctx.state_store.list_execution_logs(
            page=page, page_size=page_size, log_type=type, status=status,
            start_time=start_time, end_time=end_time,
        )
        total = service._ctx.state_store.count_execution_logs(
            log_type=type, status=status, start_time=start_time, end_time=end_time
        )
        return {
            "items": [
                {
                    "exec_id": r.exec_id, "name": r.name, "type": r.type,
                    "target_id": r.target_id, "status": r.status,
                    "reason": r.reason,
                    "created_at": r.created_at, "updated_at": r.updated_at,
                }
                for r in logs
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/execution-logs/{exec_id}")
    async def get_execution_log(exec_id: str) -> dict[str, Any]:
        """获取单条执行日志详情（含 detail_log）。"""
        log = service._ctx.state_store.get_execution_log(exec_id)
        if not log:
            raise HTTPException(status_code=404, detail="执行日志不存在")
        return {
            "exec_id": log.exec_id, "name": log.name, "type": log.type,
            "target_id": log.target_id, "status": log.status,
            "reason": log.reason, "detail_log": log.detail_log,
            "created_at": log.created_at, "updated_at": log.updated_at,
        }

    @app.post("/api/execution-logs/batch-delete")
    async def batch_delete_execution_logs(body: ExecutionLogBatchDeleteRequest) -> dict[str, Any]:
        """批量删除执行日志。找不到的忽略。"""
        deleted = service._ctx.state_store.delete_execution_logs(body.exec_ids)
        return {"status": "ok", "deleted": deleted}

    @app.delete("/api/execution-logs/{exec_id}")
    async def delete_execution_log(exec_id: str) -> dict[str, Any]:
        """删除一条执行日志。"""
        ok = service._ctx.state_store.delete_execution_log(exec_id)
        if not ok:
            raise HTTPException(status_code=404, detail="执行日志不存在")
        return {"status": "ok"}

    def _execution_log_text(log: Any) -> str:
        """把一条执行记录整理成可放进压缩包的正文。

        Args:
            log (Any): 执行日志记录，需带名称、状态和 detail_log。

        Returns:
            str: 多行文本。
        """
        return "\n".join([
            f"执行日志: {log.name}",
            f"类型: {log.type}",
            f"目标ID: {log.target_id}",
            f"状态: {log.status}",
            f"原因: {log.reason or '-'}",
            f"开始时间: {log.created_at}",
            f"更新时间: {log.updated_at}",
            f"{'=' * 60}",
            "",
            log.detail_log or "(无详细日志)",
        ])

    def _lookup_task_config(log: Any) -> dict[str, Any] | None:
        """按目标 id 找出任务配置。

        Args:
            log (Any): 执行日志记录。

        Returns:
            dict[str, Any] | None: 任务配置。没有对应任务时为 None。
        """
        getter = getattr(service._ctx.state_store, "get_task_config", None)
        if callable(getter) and log.target_id:
            found = getter(log.target_id)
            if isinstance(found, dict):
                return found
        lister = getattr(service._ctx.state_store, "list_task_configs", None)
        if not callable(lister):
            return None
        for item in lister():
            if item.get("id") == log.target_id or item.get("name") in {log.target_id, log.name}:
                return item if isinstance(item, dict) else None
        return None

    def _workflow_yaml(name: str) -> str:
        """按编号或名称读取一份工作流 YAML。

        Args:
            name (str): 工作流 id 或名称。

        Returns:
            str: YAML 正文。找不到时为空字符串。
        """
        store = service._ctx.state_store
        found = None
        getter = getattr(store, "get_workflow_config", None)
        if callable(getter) and name:
            found = getter(name)
        if not isinstance(found, dict):
            lister = getattr(store, "list_workflow_configs", None)
            if callable(lister):
                for item in lister():
                    if item.get("id") == name or item.get("name") == name:
                        found = item
                        break
        if not isinstance(found, dict):
            return ""
        return str(found.get("yaml_text") or "")

    def _workflow_files(log: Any, task: dict[str, Any] | None) -> dict[str, str]:
        """收集这一次执行用到的工作流。

        指标单跑只带那一份。任务则带登录流程和每个指标。

        Args:
            log (Any): 执行日志记录。
            task (dict[str, Any] | None): 对应的任务配置。

        Returns:
            dict[str, str]: 文件名到 YAML 正文。
        """
        wanted: list[str] = []
        if log.type == "workflow":
            wanted.append(str(log.target_id or log.name or ""))
        elif task:
            login = str(task.get("login") or "").strip()
            if login:
                wanted.append(login)
            for name in task.get("indicators") or []:
                text = str(name or "").strip()
                if text:
                    wanted.append(text)
        else:
            if log.target_id:
                wanted.append(str(log.target_id))
            if log.name and log.name not in wanted:
                wanted.append(str(log.name))
        files: dict[str, str] = {}
        used: set[str] = set()
        for name in wanted:
            body = _workflow_yaml(name)
            if not body.strip():
                continue
            safe = re.sub(r"[^\w\-.]+", "_", name).strip("._") or "workflow"
            filename = f"{safe}.yml"
            index = 2
            while filename in used:
                filename = f"{safe}_{index}.yml"
                index += 1
            used.add(filename)
            files[filename] = body
        return files

    async def _write_execution_bundle(exec_id: str, download_dir: str) -> tuple[Any, Path, str]:
        """生成一份离线诊断 zip，不上传。

        Args:
            exec_id (str): 执行日志 id。
            download_dir (str): 用户下载目录。空则用数据目录。

        Returns:
            tuple[Any, Path, str]: 日志记录、zip 路径、文件名。

        Raises:
            HTTPException: 执行日志不存在时，状态码 404。
        """
        from spider_gd.gateway.execution_bundle import (
            build_execution_zip,
            parse_local_timestamp,
            safe_read_log_window,
            safe_select_runtime_artifacts,
        )

        log = service._ctx.state_store.get_execution_log(exec_id)
        if not log:
            raise HTTPException(status_code=404, detail="执行日志不存在")
        data_dir = Path(service._ctx.settings.storage.data_dir)
        dl_dir = Path(download_dir) if download_dir else data_dir
        log_dir = dl_dir / "执行日志"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w\-.]", "_", log.name)[:64]
        filename = f"{safe_name}_{log.exec_id[:12]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path = log_dir / filename
        started = parse_local_timestamp(log.created_at)
        finished = parse_local_timestamp(log.updated_at)
        task = None if log.type == "workflow" else _lookup_task_config(log)
        runtime_files, notes = safe_select_runtime_artifacts(dl_dir, started, finished)
        runtime_payload: list[tuple[str, Path]] = []
        for path in runtime_files:
            try:
                arc = "runtime/" + path.resolve().relative_to(dl_dir.resolve()).as_posix()
            except ValueError:
                arc = "runtime/" + path.name
            except Exception as exc:
                notes.append(f"跳过现场文件 {path.name}：{exc}")
                continue
            runtime_payload.append((arc, path))
        app_log_text = safe_read_log_window(
            Path(service._ctx.settings.log.file_dir) / "app.log",
            started,
            finished,
        )
        build_execution_zip(
            zip_path,
            execution_text=_execution_log_text(log),
            app_log_text=app_log_text,
            summary={
                "exec_id": log.exec_id,
                "name": log.name,
                "type": log.type,
                "target_id": log.target_id,
                "status": log.status,
                "reason": log.reason,
                "created_at": log.created_at,
                "updated_at": log.updated_at,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            },
            workflows=_workflow_files(log, task),
            task_config=task,
            runtime_files=runtime_payload,
            notes=notes,
        )
        return log, zip_path, filename

    @app.post("/api/execution-logs/{exec_id}/download")
    async def download_execution_log(exec_id: str, req: DownloadLogRequest) -> dict[str, Any]:
        """把这次执行的日志、诊断和失败现场打成一个 zip。"""
        try:
            _log, filepath, filename = await _write_execution_bundle(exec_id, req.download_dir)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("执行诊断包写入失败")
            return {
                "status": "error",
                "message": f"诊断包没有生成：{exc}",
                "path": "",
                "filename": "",
            }
        logger.info("执行诊断包已下载: {} ({} bytes)", filepath, filepath.stat().st_size)
        return {"status": "ok", "path": str(filepath), "filename": filename}

    # ---- 应用设置 API ----

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        """返回所有应用设置（从 SQLite）。"""
        settings = service._ctx.state_store.get_all_settings()
        if settings.get("wifiPassword"):
            settings["wifiPassword"] = "******"
        # 上传配置里的密文不走通用设置接口，避免被设置页整包覆盖或展示。
        settings.pop("dataUploadProfiles", None)
        if settings.get("stationProfiles"):
            settings["stationProfiles"] = mask_station_profiles(settings["stationProfiles"])
        return {"settings": settings}

    @app.put("/api/settings")
    async def save_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """批量保存应用设置。"""
        settings = payload.get("settings", payload) if isinstance(payload, dict) else {}
        if not isinstance(settings, dict):
            settings = {}
        kv = {str(k): str(v) for k, v in settings.items()}
        if not kv.get("wifiPassword", "").strip():
            kv.pop("wifiPassword", None)
        if "stationProfiles" in kv:
            try:
                existing = service._ctx.state_store.get_setting("stationProfiles") or ""
                kv["stationProfiles"] = merge_station_profiles(kv["stationProfiles"], existing)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        from spider_gd.gateway.settings_update import filter_settings_update

        kv = filter_settings_update(kv)
        if not kv:
            return {"status": "ok", "settings": {}}
        service._ctx.state_store.save_settings(kv)
        stored = service._ctx.state_store.get_all_settings()
        if "downloadDir" in kv:
            _sync_browser_download_dir()
        try:
            from spider_gd.app import build_alert_gateway

            service._ctx.alert = build_alert_gateway(service._ctx.settings, service._ctx.state_store)
        except Exception as exc:
            logger.warning("刷新告警网关失败: {}", exc)
        readback = {key: stored.get(key, "") for key in kv}
        if readback.get("wifiPassword"):
            readback["wifiPassword"] = "******"
        return {"status": "ok", "settings": readback}

    @app.get("/api/stations/usb-devices")
    async def list_station_usb_devices() -> dict[str, Any]:
        """列出当前插入的 USB 设备，供场站下拉选择。"""
        from spider_gd.infrastructure.network.usb_devices import list_usb_devices

        try:
            devices = await asyncio.to_thread(list_usb_devices)
        except Exception as exc:
            logger.warning("读取 USB 设备失败: {}", exc)
            return {"devices": [], "error": str(exc)}
        return {"devices": [item.as_dict() for item in devices], "error": ""}

    @app.get("/api/stations/ukey-report")
    async def station_ukey_report() -> dict[str, Any]:
        """检测外部 USB 和证书，判断多把 UKey 能不能分开。"""
        from spider_gd.infrastructure.network.usb_devices import probe_ukeys

        try:
            report = await asyncio.wait_for(asyncio.to_thread(probe_ukeys), timeout=16)
        except Exception as exc:
            logger.warning("UKey 检测失败: {}", exc)
            return {
                "devices": [],
                "ukeys": [],
                "certificates": [],
                "distinguishable": False,
                "summary": f"读取 USB 设备失败：{exc}",
                "error": str(exc),
            }
        return report if isinstance(report, dict) else {
            "summary": str(report),
            "error": "检测结果不是对象",
            "devices": [],
            "ukeys": [],
            "certificates": [],
            "distinguishable": False,
        }

    @app.post("/api/stations/ukey-report/download")
    async def download_ukey_report(req: DownloadLogRequest) -> dict[str, Any]:
        """重新检测 UKey，只把钥匙、证书和绑定关系打成一个 zip。

        设备枚举超时或失败时，把说明写进压缩包并继续。这个接口不因枚举失败返回 500。

        Args:
            req: 下载目录。空则写到数据目录下的诊断包文件夹。

        Returns:
            dict[str, Any]: ``path`` 是 zip 的本地路径。``warning`` 非空表示枚举不完整。
            文件写不出来时 ``status`` 为 ``error``，``message`` 是原因。
        """
        from spider_gd.gateway.execution_bundle import build_ukey_zip, ukey_binding_rows
        from spider_gd.infrastructure.network.usb_devices import gather_ukey_download_parts

        try:
            report, raw, warning = await asyncio.to_thread(gather_ukey_download_parts)
        except Exception as exc:
            logger.warning("UKey 诊断采集失败: {}", exc)
            warning = str(exc)
            report = {
                "summary": f"读取 USB 设备失败：{exc}",
                "error": str(exc),
                "devices": [],
                "ukeys": [],
                "certificates": [],
                "distinguishable": False,
            }
            raw = {"usb": warning, "certificates": warning}
        try:
            data_dir = Path(service._ctx.settings.storage.data_dir)
            dl_dir = Path(req.download_dir) if req.download_dir else data_dir
            out_dir = dl_dir / "诊断包"
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = f"ukey_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            zip_path = out_dir / filename
            saved = service._ctx.state_store.get_setting("stationProfiles") or ""
            bindings = ukey_binding_rows(mask_station_profiles(saved))
            raw_report = report if isinstance(report, dict) else {
                "summary": str(report),
                "error": "检测结果不是对象",
            }
            build_ukey_zip(
                zip_path,
                report=raw_report,
                raw_usb=str(raw.get("usb") or ""),
                raw_certificates=str(raw.get("certificates") or ""),
                bindings=bindings,
            )
        except Exception as exc:
            logger.exception("UKey 诊断包写入失败")
            return {
                "status": "error",
                "message": f"诊断包没有写成文件：{exc}",
                "warning": warning,
                "path": "",
                "filename": "",
            }
        logger.info("UKey 诊断包已下载: {} ({} bytes)", zip_path, zip_path.stat().st_size)
        return {
            "status": "ok",
            "path": str(zip_path),
            "filename": filename,
            "warning": warning,
            "message": warning,
        }

    @app.post("/api/stations/pick")
    async def pick_login_station(
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        """打开本机场站选择窗口，返回选中的场站。

        密码留在服务端。未配置满两个场站时，预览会改用示例场站。

        Args:
            payload: 可选 ``choice``、``forceWindow``、``useSupplied`` 和 ``stations``。

        Returns:
            dict[str, Any]: ``ok`` 为真时含场站名称和 WiFi，不含密码。

        Raises:
            HTTPException: 场站列表不合法，或指定的场站不存在。
        """
        body = payload if isinstance(payload, dict) else {}
        choice = str(body.get("choice") or "").strip()
        force_window = bool(body.get("forceWindow"))
        if body.get("useSupplied"):
            try:
                supplied = json.dumps(body.get("stations") or [], ensure_ascii=False)
                profiles = parse_station_profiles(supplied)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            saved = service._ctx.state_store.get_setting("stationProfiles") or ""
            try:
                profiles = parse_station_profiles(saved)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        profiles, used_demo = profiles_for_window(profiles, force_window=force_window)
        try:
            selected = await asyncio.to_thread(
                pick_station,
                profiles,
                choice=choice or None,
                force_window=force_window,
            )
        except StationPickError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if selected is None:
            return {"ok": False, "cancelled": True, "demo": used_demo, "message": "已取消选择场站"}
        logger.info("已选择场站 {}，WiFi {}", selected.name, selected.wifi_ssid)
        return {
            "ok": True,
            "cancelled": False,
            "demo": used_demo,
            "station": public_station(selected),
            "message": f"已选择场站 {selected.name}，将连接 WiFi {selected.wifi_ssid}",
        }

    @app.post("/api/delivery/test")
    async def test_delivery(payload: dict[str, Any]) -> dict[str, Any]:
        """验证稳定发送配置。默认 dry-run；传 send=true 才真实发送。"""
        from spider_gd.app import (
            _build_delivery_hook,
            _build_reliable_delivery_hook,
            build_delivery_test_plan,
            create_delivery_sample_zip,
            delivery_outbox_receipt,
            send_delivery_test_message,
            verify_delivery_receipt,
        )

        file_path = str(payload.get("filePath", "")).strip()
        send = bool(payload.get("send", False))
        sample_zip = bool(payload.get("sampleZip", False))
        download_remote = bool(payload.get("downloadRemote", False))
        verify = bool(payload.get("verify", False)) or download_remote
        no_remote = bool(payload.get("noRemote", False))
        force_remote_link = bool(payload.get("forceRemoteLink", False))
        require_remote_url = bool(payload.get("requireRemoteUrl", False)) or force_remote_link
        try:
            max_remote_mb = int(payload.get("maxRemoteMb", 200) or 200)
        except (TypeError, ValueError):
            max_remote_mb = 200
        max_remote_bytes = max(1, max_remote_mb) * 1024 * 1024
        previous_overrides = getattr(service._ctx, "delivery_overrides", None)
        temporary_overrides: dict[str, str] = {}
        if force_remote_link:
            temporary_overrides["deliveryFileDelivery"] = "remote_link"
        if temporary_overrides:
            current = previous_overrides or {}
            service._ctx.delivery_overrides = {**current, **temporary_overrides}

        def _add_test_flags(result: dict[str, Any]) -> dict[str, Any]:
            result["force_remote_link"] = force_remote_link
            result["require_remote_url"] = require_remote_url
            result["download_remote"] = download_remote
            if temporary_overrides:
                result["temporary_overrides"] = {"deliveryFileDelivery": "remote_link"}
            return result

        try:
            if not file_path and sample_zip:
                file_path = str(create_delivery_sample_zip(service._ctx.settings.storage.data_dir))
            plan = build_delivery_test_plan(
                service._ctx,
                file_path=file_path or None,
                dry_run=not send,
            )
            _add_test_flags(plan)
            if not send or not plan.get("ok"):
                return plan

            hook = _build_delivery_hook(service._ctx)
            if hook is None:
                plan["ok"] = False
                plan["errors"] = ["delivery.enabled 未开启"]
                return plan

            try:
                if file_path:
                    path = Path(file_path).expanduser().resolve()
                    reliable_hook = _build_reliable_delivery_hook(service._ctx)
                    task_id = f"DELIVERY_UI_TEST_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
                    plan["task_id"] = task_id
                    await reliable_hook(
                        task_id,
                        str(path.parent),
                        str(path),
                        {"total": 1, "success": 1, "failed": 0},
                    )
                else:
                    ok = await send_delivery_test_message(service._ctx)
                    if not ok:
                        raise RuntimeError("稳定发送测试消息发送失败")
            except Exception as exc:
                plan["ok"] = False
                plan["sent"] = False
                plan["errors"] = [str(exc)]
                reader = getattr(service._ctx.state_store, "get_delivery_outbox", None)
                task_id = str(plan.get("task_id") or "")
                if task_id and callable(reader):
                    plan["outbox"] = delivery_outbox_receipt(reader(task_id))
                return plan

            plan["dry_run"] = False
            plan["sent"] = True
            reader = getattr(service._ctx.state_store, "get_delivery_outbox", None)
            task_id = str(plan.get("task_id") or "")
            if task_id and callable(reader):
                plan["outbox"] = delivery_outbox_receipt(reader(task_id))

            outbox = plan.get("outbox")
            remote_url = str(outbox.get("remote_url") or "").strip() if isinstance(outbox, dict) else ""
            errors: list[str] = []
            if require_remote_url and not remote_url:
                errors.append("验收要求 remote_url，但本次发送没有生成远程下载链接")
            if verify and task_id:
                verification = await verify_delivery_receipt(
                    service._ctx,
                    task_id=task_id,
                    verify_remote=not no_remote,
                    download_remote=download_remote,
                    max_remote_bytes=max_remote_bytes,
                )
                plan["verification"] = verification
                if not verification.get("ok"):
                    errors.extend(str(item) for item in verification.get("errors") or [])
            if errors:
                plan["ok"] = False
                plan["errors"] = errors
            return plan
        finally:
            service._ctx.delivery_overrides = previous_overrides

    @app.get("/api/delivery/outbox")
    async def list_delivery_outbox(status: str = "pending", limit: int = 50) -> dict[str, Any]:
        """查看任务产物待补发队列。"""
        from spider_gd.app import delivery_forwarding_summary

        lister = getattr(service._ctx.state_store, "list_delivery_outbox", None)
        summarizer = getattr(service._ctx.state_store, "get_delivery_outbox_summary", None)
        if not callable(lister):
            return {
                "items": [],
                "summary": {"pending": 0, "sending": 0, "sent": 0, "failed": 0, "total": 0},
            }
        safe_limit = max(1, min(200, int(limit)))
        summary = summarizer() if callable(summarizer) else {}
        items = []
        for item in lister(status=status, limit=safe_limit):
            row = dict(item)
            row["forwarding"] = delivery_forwarding_summary(row)
            items.append(row)
        return {"items": items, "summary": summary}

    @app.post("/api/delivery/retry")
    async def retry_delivery(payload: dict[str, Any]) -> dict[str, Any]:
        """手动补发任务产物 outbox。"""
        from spider_gd.app import retry_pending_deliveries

        limit = int(payload.get("limit", 20) or 20)
        safe_limit = max(1, min(200, limit))
        requeued = 0
        if bool(payload.get("includeFailed", False)):
            requeuer = getattr(service._ctx.state_store, "requeue_delivery_outbox", None)
            if callable(requeuer):
                requeued = requeuer(status="failed", limit=safe_limit)
        result = await retry_pending_deliveries(service._ctx, limit=safe_limit)
        result["requeued_failed"] = requeued
        return result

    @app.post("/api/delivery/verify")
    async def verify_delivery(payload: dict[str, Any]) -> dict[str, Any]:
        """核验稳定发送回执：outbox 状态、本地 zip hash 和远程链接。"""
        from spider_gd.app import verify_delivery_receipt

        task_id = str(payload.get("taskId", "") or "").strip()
        receipt_path = str(payload.get("receiptPath", "") or "").strip()
        no_remote = bool(payload.get("noRemote", False))
        download_remote = bool(payload.get("downloadRemote", False))
        max_remote_mb = int(payload.get("maxRemoteMb", 200) or 200)
        return await verify_delivery_receipt(
            service._ctx,
            task_id=task_id,
            receipt_path=receipt_path or None,
            verify_remote=not no_remote,
            download_remote=download_remote,
            max_remote_bytes=max(1, max_remote_mb) * 1024 * 1024,
        )

    @app.post("/api/delivery/outbox/cleanup")
    async def cleanup_delivery_outbox(payload: dict[str, Any]) -> dict[str, Any]:
        """清理任务产物发送 outbox 历史。默认清理已发送记录。"""
        cleaner = getattr(service._ctx.state_store, "cleanup_delivery_outbox", None)
        if not callable(cleaner):
            return {"deleted": 0}
        status = str(payload.get("status", "sent") or "sent")
        limit = int(payload.get("limit", 200) or 200)
        if status not in {"sent", "failed"}:
            status = "sent"
        return {"deleted": cleaner(status=status, limit=max(1, min(1000, limit)))}

    @app.post("/api/delivery/outbox/requeue")
    async def requeue_delivery_outbox(payload: dict[str, Any]) -> dict[str, Any]:
        """把失败的任务产物发送记录恢复为待补发。"""
        requeuer = getattr(service._ctx.state_store, "requeue_delivery_outbox", None)
        if not callable(requeuer):
            return {"requeued": 0}
        status = str(payload.get("status", "failed") or "failed")
        limit = int(payload.get("limit", 200) or 200)
        if status != "failed":
            status = "failed"
        return {"requeued": requeuer(status=status, limit=max(1, min(1000, limit)))}

    # ---- 录制工坊 API ----

    _WORKSHOP_EXPORT_SUBDIR = "录制工坊"

    def _workshop_export_dir(download_dir: str) -> Path:
        base = Path(download_dir) if download_dir else Path(service._ctx.settings.storage.data_dir)
        return base / _WORKSHOP_EXPORT_SUBDIR

    def _resolve_workshop_zip_path(zip_path: Path, download_dir: str) -> Path:
        if not zip_path.exists() or zip_path.suffix.lower() != ".zip":
            raise HTTPException(status_code=404, detail="ZIP 文件不存在")
        export_dir = _workshop_export_dir(download_dir)
        try:
            resolved = zip_path.resolve()
            if not resolved.is_relative_to(export_dir.resolve()):
                raise HTTPException(status_code=400, detail="文件路径无效")
        except ValueError:
            raise HTTPException(status_code=400, detail="文件路径无效")
        return resolved

    @app.get("/api/record-workshop/exports")
    async def list_workshop_exports(download_dir: str = "") -> list[dict[str, Any]]:
        """列出录制工坊目录下的 ZIP 导出文件（含提交状态）。"""
        from spider_gd.core.workshop_zip import read_workshop_zip_meta, resolve_submission_status

        export_dir = _workshop_export_dir(download_dir)
        if not export_dir.exists():
            return []

        zip_files = sorted(export_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        metas = [read_workshop_zip_meta(fp) for fp in zip_files]
        sub_map = service._ctx.state_store.get_workshop_submissions_map(
            [m["export_id"] for m in metas]
        )

        items: list[dict[str, Any]] = []
        for fp, meta in zip(zip_files, metas):
            stat = fp.stat()
            submission = sub_map.get(meta["export_id"])
            status = resolve_submission_status(submission, meta["file_hash"])
            items.append({
                "export_id": meta["export_id"],
                "name": meta["name"],
                "filename": fp.name,
                "path": str(fp.resolve()),
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "status": status,
                "submitted_at": (submission or {}).get("submitted_at") or "",
                "file_hash": meta["file_hash"],
            })
        return items

    @app.post("/api/record-workshop/submit")
    async def submit_workshop_export(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """提交录制工坊 ZIP（远程上传接口未配置时仅本地标记）。"""
        from spider_gd.core.workshop_zip import read_workshop_zip_meta
        from spider_gd.gateway.workshop_uploader import upload_workshop_zip

        zip_path = Path(str(payload.get("path", "")).strip())
        download_dir = str(payload.get("download_dir", "")).strip()
        _resolve_workshop_zip_path(zip_path, download_dir)

        meta = read_workshop_zip_meta(zip_path)
        export_id = meta["export_id"]
        file_hash = meta["file_hash"]

        upload_url = service._ctx.state_store.get_setting("workshopUploadUrl") or ""
        upload_token = service._ctx.state_store.get_setting("workshopUploadToken") or ""
        upload_result = await upload_workshop_zip(
            upload_url, upload_token, zip_path, meta.get("name", zip_path.stem)
        )

        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not upload_result.ok:
            service._ctx.state_store.upsert_workshop_submission(
                export_id=export_id,
                filename=zip_path.name,
                local_path=str(zip_path.resolve()),
                file_hash=file_hash,
                status="failed",
                error_msg=upload_result.message,
                updated_at=now_ts,
            )
            raise HTTPException(status_code=400, detail=upload_result.message)

        service._ctx.state_store.upsert_workshop_submission(
            export_id=export_id,
            filename=zip_path.name,
            local_path=str(zip_path.resolve()),
            file_hash=file_hash,
            status="submitted",
            remote_url=upload_result.remote_url,
            submitted_at=now_ts,
            error_msg="",
            updated_at=now_ts,
        )
        return {
            "status": "ok",
            "export_id": export_id,
            "uploaded": upload_result.uploaded,
            "local_only": upload_result.local_only,
            "message": upload_result.message,
        }

    @app.post("/api/record-workshop/delete")
    async def delete_workshop_export(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """删除录制工坊 ZIP 及对应提交记录，便于重新导出并提交。"""
        from spider_gd.core.workshop_zip import read_workshop_zip_meta

        zip_path = Path(str(payload.get("path", "")).strip())
        download_dir = str(payload.get("download_dir", "")).strip()
        resolved = _resolve_workshop_zip_path(zip_path, download_dir)

        export_id = read_workshop_zip_meta(resolved)["export_id"]
        try:
            resolved.unlink(missing_ok=True)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"删除文件失败: {exc}") from exc

        service._ctx.state_store.delete_workshop_submission(export_id)
        return {"status": "ok", "export_id": export_id, "message": "已删除"}

    @app.post("/api/record-workshop/build")
    async def build_workshop(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """启动交互式工作流构建器，用户在浏览器中录步骤并导出 ZIP。"""
        import shutil as _shutil
        import tempfile as _tmp

        name = str(payload.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="请填写工作流名称")

        download_dir = str(payload.get("download_dir", "")).strip()
        if not download_dir:
            raise HTTPException(status_code=400, detail="请先在设置中配置下载路径")

        url = str(payload.get("url", "")).strip()
        if not url:
            site_url = service._ctx.state_store.get_setting("siteUrl") or ""
            if site_url:
                url = site_url if site_url.startswith(("http://", "https://")) else f"https://{site_url}"
            else:
                url = f"https://{service._ctx.settings.platform.domain}"
        elif not url.startswith(("http://", "https://")):
            url = f"https://{url.rstrip('/')}"

        description = str(payload.get("description", ""))
        export_dir = _workshop_export_dir(download_dir)
        export_dir.mkdir(parents=True, exist_ok=True)

        safe_name = re.sub(r"[^\w\-.]", "_", name)[:64] or "workflow"
        staging = Path(_tmp.mkdtemp(prefix="workshop_build_"))
        output = staging / f"{safe_name}.yml"

        _tools_dir = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        if str(_tools_dir) not in sys.path:
            sys.path.insert(0, str(_tools_dir))
        from build_workflow import build_workflow as _build_workflow

        logger.info("🔧 启动工作流构建器: name={} url={} export_dir={}", name, url, export_dir)

        exported: list[str] = []
        try:
            exported = await _build_workflow(
                url, output, name, description, export_dir=export_dir,
            )
        except Exception as exc:
            logger.error("工作流构建失败: {}", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            _shutil.rmtree(staging, ignore_errors=True)

        return {
            "status": "ok",
            "export_dir": str(export_dir),
            "exported": exported,
            "message": "构建会话已结束",
        }

    # ---- 网络路由 API ----

    @app.get("/api/network/interfaces")
    async def list_interfaces() -> dict[str, Any]:
        """列出系统可用网络接口（Windows: Get-NetAdapter → wmic 兜底, 其他: ifconfig）。"""
        import platform as _plat

        from spider_gd.infrastructure.proc import run_capture

        interfaces: list[dict] = []
        error: str = ""
        if _plat.system() == "Windows":
            # 方案1：PowerShell Get-NetAdapter（Win8+，需 NetAdapter 模块）
            # 统一走 run_capture 做二进制捕获 + 控制台编码解码：
            # PYTHONUTF8=1 时 text=True 会把 cp936 输出解码失败并吞成 None。
            try:
                result = run_capture(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                     "Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, ifIndex | ConvertTo-Json -Compress"],
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    import json as _json
                    logger.debug("PS stdout (前200): {}", result.stdout[:200])
                    raw = _json.loads(result.stdout)
                    items = raw if isinstance(raw, list) else [raw]
                    for it in items:
                        interfaces.append({
                            "name": it.get("Name", ""),
                            "desc": it.get("InterfaceDescription", it.get("Name", "")),
                            "status": it.get("Status", ""),
                            "index": it.get("ifIndex", 0),
                        })
                elif result.returncode != 0:
                    error = f"Get-NetAdapter 失败: {result.stderr.strip()[:200]}"
                    logger.warning("Get-NetAdapter failed: {}", error)
            except Exception as e:
                error = f"PowerShell 异常: {e}"
                logger.warning("获取网络接口失败 (PowerShell): {}", e)

            # 方案2：wmic 兜底（所有 Windows 版本/语言，无需管理员）
            # wmic nic 输出始终是英文，不依赖系统语言
            if not interfaces:
                try:
                    result = run_capture(
                        ["wmic", "nic", "get", "NetConnectionID,Name,NetConnectionStatus,Index",
                         "/format:csv"],
                        timeout=10,
                    )
                    if result.returncode == 0:
                        import csv as _csv
                        import io as _io
                        reader = _csv.DictReader(_io.StringIO(result.stdout))
                        for row in reader:
                            name = (row.get("NetConnectionID") or row.get("Name") or "").strip()
                            if not name:
                                continue
                            status_code = row.get("NetConnectionStatus", "0").strip()
                            # NetConnectionStatus: 0=Disconnected, 2=Connected, 7=MediaDisconnected
                            status = "Up" if status_code == "2" else "Down"
                            interfaces.append({
                                "name": name,
                                "desc": (row.get("Name") or name).strip(),
                                "status": status,
                                "index": int(row.get("Index", "0") or "0"),
                            })
                    elif not error:
                        error = f"wmic 失败: {result.stderr.strip()[:200]}"
                except Exception as e:
                    if not error:
                        error = f"wmic 异常: {e}"
                    logger.warning("获取网络接口失败 (wmic): {}", e)

            # 方案3：netsh 最后兜底（需要注意本地化输出）
            if not interfaces:
                try:
                    result = run_capture(
                        ["netsh", "interface", "show", "interface"],
                        timeout=10,
                    )
                    if result.returncode == 0:
                        # netsh 格式: "Admin State    State          Type             Interface Name"
                        # 英文值: Enabled/Disabled, Connected/Disconnected
                        # 中文值: 已启用/已禁用, 已连接/已断开
                        # 通过索引位置而非关键字匹配来避免本地化问题
                        for line in result.stdout.splitlines():
                            parts = line.split()
                            if len(parts) < 4:
                                continue
                            # 跳过标题行和分隔线
                            if parts[0] in ("Admin", "管理状态", "-----"):
                                continue
                            # parts: [AdminState, State, Type, Name...]
                            name = " ".join(parts[3:])
                            # 判断状态：第二个字段
                            state = parts[1].lower()
                            status = "Up" if state in ("connected", "已连接") else "Down"
                            interfaces.append({
                                "name": name,
                                "desc": name,
                                "status": status,
                                "index": 0,
                            })
                    elif not error:
                        error = f"netsh 失败: {result.stderr.strip()[:200]}"
                except Exception as e:
                    if not error:
                        error = f"netsh 异常: {e}"
                    logger.warning("获取网络接口失败 (netsh): {}", e)
        else:
            try:
                result = run_capture(["ifconfig"], timeout=5)
                for line in result.stdout.splitlines():
                    if line and not line.startswith("\t") and not line.startswith(" "):
                        name = line.split(":")[0].strip()
                        if name and "LOOPBACK" not in name.upper():
                            interfaces.append({"name": name, "desc": name, "status": "Up", "index": 0})
            except Exception:
                pass

        return {"interfaces": interfaces, "error": error}

    @app.get("/api/network/interfaces/debug")
    async def debug_interfaces() -> dict[str, Any]:
        """调试端点：返回三种获取方式的原始输出，用于排查网卡下拉为空的问题。"""
        import platform as _plat

        from spider_gd.infrastructure.proc import run_capture

        debug: dict[str, Any] = {"platform": _plat.system()}
        if _plat.system() != "Windows":
            debug["note"] = "此调试端点仅适用于 Windows"
            return debug

        # 方法1: Get-NetAdapter
        try:
            r1 = run_capture(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 "Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, ifIndex | ConvertTo-Json -Compress"],
                timeout=10,
            )
            debug["ps"] = {
                "returncode": r1.returncode,
                "stdout": r1.stdout[:2000] if r1.stdout else "(empty)",
                "stderr": r1.stderr[:500] if r1.stderr else "(empty)",
            }
        except Exception as e:
            debug["ps"] = {"exception": str(e)}

        # 方法2: wmic
        try:
            r2 = run_capture(
                ["wmic", "nic", "get", "NetConnectionID,Name,NetConnectionStatus,Index", "/format:csv"],
                timeout=10,
            )
            debug["wmic"] = {
                "returncode": r2.returncode,
                "stdout": r2.stdout[:2000] if r2.stdout else "(empty)",
                "stderr": r2.stderr[:500] if r2.stderr else "(empty)",
            }
        except Exception as e:
            debug["wmic"] = {"exception": str(e)}

        # 方法3: netsh
        try:
            r3 = run_capture(
                ["netsh", "interface", "show", "interface"],
                timeout=10,
            )
            debug["netsh"] = {
                "returncode": r3.returncode,
                "stdout": r3.stdout[:2000] if r3.stdout else "(empty)",
                "stderr": r3.stderr[:500] if r3.stderr else "(empty)",
            }
        except Exception as e:
            debug["netsh"] = {"exception": str(e)}

        return debug

    @app.get("/api/network/route-rules")
    async def list_route_rules() -> dict[str, Any]:
        """获取已保存的路由规则。"""
        return {"rules": service._ctx.state_store.list_route_rules()}

    @app.put("/api/network/route-rules")
    async def save_route_rules(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """保存路由规则（原子替换）。"""
        rules = payload.get("rules", [])
        if not isinstance(rules, list):
            raise HTTPException(status_code=400, detail="rules 必须是数组")
        # 校验非空
        for r in rules:
            if not r.get("domain_pattern", "").strip():
                raise HTTPException(status_code=400, detail="域名不能为空")
            if not r.get("interface_name", "").strip():
                raise HTTPException(status_code=400, detail="网卡名称不能为空")
        service._ctx.state_store.save_route_rules(rules)
        return {"status": "ok"}

    @app.post("/api/network/wifi/test")
    async def test_wifi_connection() -> dict[str, Any]:
        """按当前设置尝试连接目标 WiFi。"""
        from spider_gd.app import prepare_wifi_connection

        ok, message, details = prepare_wifi_connection(service._ctx)
        return {
            "ok": ok,
            "message": message,
            "details": details,
        }

    @app.get("/api/network/wifi/current")
    async def get_current_wifi() -> dict[str, str]:
        """只读取当前 WiFi 名称，不触发附近网络扫描。"""
        from spider_gd.infrastructure.network import WifiConnector

        try:
            return {"ssid": WifiConnector().current_ssid(), "error": ""}
        except Exception as exc:
            return {"ssid": "", "error": str(exc)}

    @app.get("/api/network/wifi/networks")
    async def list_wifi_networks(interface_name: str = "") -> dict[str, Any]:
        """扫描附近可用 WiFi，供设置页选择目标 SSID。"""
        from spider_gd.infrastructure.network import WifiConnector

        try:
            connector = WifiConnector()
            networks = connector.list_networks(interface_name)
            current_ssid = connector.current_ssid(interface_name)
            return {
                "networks": [
                    {
                        "ssid": item.ssid,
                        "signal": item.signal,
                        "security": item.security,
                        "current": item.current,
                    }
                    for item in networks
                ],
                "current_ssid": current_ssid,
                "error": "",
            }
        except Exception as exc:
            return {
                "networks": [],
                "current_ssid": "",
                "error": str(exc),
            }

    @app.post("/api/network/route-rules/apply")
    async def apply_route_rules() -> dict[str, Any]:
        """应用路由规则到系统：专网动态 DNS → /32 路由 → 专网/公网验证。"""
        from spider_gd.infrastructure.network import DualNicClosure
        from spider_gd.app import prepare_wifi_connection

        wifi_ok, wifi_message, wifi_details = prepare_wifi_connection(service._ctx)
        if not wifi_ok:
            return {
                "status": "error",
                "applied": 0,
                "message": wifi_message,
                "details": wifi_details,
                "results": [],
            }
        rules = service._ctx.state_store.list_route_rules()
        cache_dir = Path(service._ctx.settings.storage.data_dir) / "network" / "dns_cache"
        closure = DualNicClosure(cache_dir=cache_dir)
        report = closure.apply_rules(rules)
        details = [*wifi_details, *report.details]
        message = f"{wifi_message}；{report.message}" if wifi_details else report.message
        return {
            "status": report.status,
            "applied": report.applied,
            "message": message,
            "details": details,
            "results": [
                {
                    "domain": item.domain,
                    "interface_name": item.interface_name,
                    "status": item.status,
                    "expected_ssid": item.expected_ssid,
                    "current_ssid": item.current_ssid,
                    "resolved_ips": item.resolved_ips,
                    "stale_ips": item.stale_ips,
                    "gateway": item.gateway,
                    "private_ok": item.private_ok,
                    "public_ok": item.public_ok,
                    "routes_ok": item.routes_ok,
                    "error": item.error,
                }
                for item in report.results
            ],
        }

    # ---- 数据管理：导出 / 导入 / 恢复出厂设置 ----

    @app.get("/api/data/export")
    async def export_all_data() -> dict[str, Any]:
        """导出所有用户数据（设置、任务、工作流、路由）为 JSON。"""
        data: dict[str, Any] = {
            "version": 1,
            "exported_at": datetime.now().isoformat(),
            "settings": service._ctx.state_store.get_all_settings(),
            "tasks": service._ctx.state_store.list_task_configs(),
            "workflow_groups": service._ctx.state_store.list_workflow_groups(),
            "workflow_configs": service._ctx.state_store.list_workflow_configs(),
            "route_rules": service._ctx.state_store.list_route_rules(),
        }
        # 收集所有 workflows yaml
        workflows = []
        from spider_gd.config.user_home import bundled_workflows_dir, user_workflows_dir

        seen_names: set[str] = set()
        for wf_dir in (user_workflows_dir(), bundled_workflows_dir()):
            if not wf_dir.exists():
                continue
            for yf in sorted(wf_dir.glob("*.yml")):
                if yf.stem in seen_names:
                    continue
                seen_names.add(yf.stem)
                try:
                    content = yf.read_text("utf-8")
                    workflows.append({"name": yf.stem, "path": str(yf), "content": content})
                except Exception:
                    pass
        data["workflows"] = workflows
        return data

    @app.post("/api/data/import")
    async def import_all_data(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """从 JSON 导入数据（覆盖当前数据，请谨慎使用）。"""
        imported: list[str] = []
        errors: list[str] = []

        # 导入设置
        settings = payload.get("settings", {})
        if isinstance(settings, dict) and settings:
            try:
                kv = {str(k): str(v) for k, v in settings.items()}
                service._ctx.state_store.save_settings(kv)
                imported.append(f"设置 ({len(kv)} 项)")
            except Exception as e:
                errors.append(f"设置导入失败: {e}")

        # 导入任务
        tasks = payload.get("tasks", [])
        if isinstance(tasks, list):
            for t in tasks:
                try:
                    if "id" in t:
                        service._ctx.state_store.save_task_config(t)
                except Exception as e:
                    errors.append(f"任务 {t.get('id','?')} 导入失败: {e}")
            imported.append(f"任务 ({len(tasks)} 个)")

        # 导入路由规则
        rules = payload.get("route_rules", [])
        if isinstance(rules, list):
            try:
                service._ctx.state_store.save_route_rules(rules)
                imported.append(f"路由规则 ({len(rules)} 条)")
            except Exception as e:
                errors.append(f"路由规则导入失败: {e}")

        # 分组先于指标配置导入，保留父子关系。
        workflow_groups = payload.get("workflow_groups", [])
        if isinstance(workflow_groups, list):
            pending = {g["id"]: g for g in workflow_groups if isinstance(g, dict) and g.get("id")}
            imported_count = 0
            while pending:
                ready = [g for g in pending.values() if not g.get("parent_id") or g["parent_id"] not in pending]
                if not ready:
                    errors.append("分组导入失败: 层级有循环")
                    break
                for group in ready:
                    try:
                        service._ctx.state_store.save_workflow_group(group["id"], group["name"], group.get("parent_id"))
                        imported_count += 1
                    except Exception as e:
                        errors.append(f"分组 {group['id']} 导入失败: {e}")
                    del pending[group["id"]]
            imported.append(f"指标分组 ({imported_count} 个)")

        # 导入指标配置 (workflow_configs)
        wf_configs = payload.get("workflow_configs", [])
        if isinstance(wf_configs, list):
            imported_count = 0
            for cfg in wf_configs:
                try:
                    if "id" in cfg:
                        service._ctx.state_store.save_workflow_config(cfg)
                        imported_count += 1
                except Exception as e:
                    errors.append(f"指标配置 {cfg.get('id','?')} 导入失败: {e}")
            imported.append(f"指标配置 ({imported_count} 个)")

        # 导入工作流
        workflows = payload.get("workflows", [])
        if isinstance(workflows, list):
            imported_count = 0
            for wf in workflows:
                try:
                    name = wf.get("name", "")
                    content = wf.get("content", "")
                    if name and content:
                        from spider_gd.config.user_home import user_workflows_dir

                        wf_path = user_workflows_dir(create=True) / f"{name}.yml"
                        wf_path.parent.mkdir(parents=True, exist_ok=True)
                        wf_path.write_text(content, "utf-8")
                        imported_count += 1
                except Exception as e:
                    errors.append(f"工作流 {wf.get('name','?')} 导入失败: {e}")
            imported.append(f"工作流 ({imported_count} 个)")

        return {"status": "ok", "imported": imported, "errors": errors}

    @app.post("/api/data/reset")
    async def factory_reset() -> dict[str, Any]:
        """恢复出厂设置：清空数据库 + 删除 workflows/*.yml。"""
        import shutil as _shutil

        cleared: list[str] = []

        # 清空路由规则
        try:
            service._ctx.state_store.save_route_rules([])
            cleared.append("路由规则")
        except Exception as e:
            return {"status": "error", "message": f"清空路由规则失败: {e}"}

        # 清空任务
        try:
            for cfg in service._ctx.state_store.list_task_configs():
                service._ctx.state_store.delete_task_config(cfg["id"])
            cleared.append("任务")
        except Exception as e:
            return {"status": "error", "message": f"清空任务失败: {e}"}

        # 清空设置
        try:
            service._ctx.state_store.save_settings({})
            cleared.append("设置")
        except Exception as e:
            return {"status": "error", "message": f"清空设置失败: {e}"}

        # 清空指标配置 (workflow_configs 表)
        try:
            for cfg in service._ctx.state_store.list_workflow_configs():
                service._ctx.state_store.delete_workflow_config(cfg["id"])
            for group in service._ctx.state_store.list_workflow_groups():
                service._ctx.state_store.delete_workflow_group(group["id"])
            cleared.append("指标配置")
        except Exception as e:
            return {"status": "error", "message": f"清空指标配置失败: {e}"}

        # 清空执行日志
        try:
            logs = service._ctx.state_store.list_execution_logs(page=1, page_size=10000)
            for log in logs:
                service._ctx.state_store.delete_execution_log(log.exec_id)
            cleared.append("执行日志")
        except Exception as e:
            logger.warning("清空执行日志失败: {}", e)

        # 删除用户目录里的工作流 YAML。安装包自带的 workflows 不动。
        from spider_gd.config.user_home import user_workflows_dir

        wf_dir = user_workflows_dir()
        if wf_dir.exists():
            for yf in wf_dir.glob("*.yml"):
                try:
                    yf.unlink()
                except Exception:
                    pass
            # 也清理子目录
            for sub in wf_dir.iterdir():
                if sub.is_dir():
                    try:
                        _shutil.rmtree(sub)
                    except Exception:
                        pass
            cleared.append("工作流")

        logger.warning("恢复出厂设置完成，已清空: {}", ", ".join(cleared))
        return {"status": "ok", "cleared": cleared, "message": f"已清空: {', '.join(cleared)}，请重启生效"}

    # ---- 文件上传到云端 ----

    @app.post("/api/files/upload")
    async def upload_file_to_cloud(payload: dict[str, Any]) -> dict[str, Any]:
        """将本地文件上传到广东数据服务器。

        body: { localPath, relativePath? }
        - localPath: 本地文件绝对路径
        - relativePath: 远程相对路径（默认 /upload/publicData/{文件名}）
        """
        local_path = Path(str(payload.get("localPath", "")).strip())
        relative_path = str(payload.get("relativePath", "")).strip()

        if not local_path.exists():
            return {"status": "error", "message": f"文件不存在: {local_path}"}
        if local_path.is_dir():
            return {"status": "error", "message": "不支持上传文件夹"}

        upload_base = (service._ctx.state_store.get_setting("workshopUploadUrl") or "").strip()
        upload_token = (service._ctx.state_store.get_setting("workshopUploadToken") or "").strip()

        if not upload_base:
            return {"status": "error", "message": "未配置上传地址（设置 → 录制工坊上传地址）"}

        if not relative_path:
            relative_path = "/upload/publicData"

        from spider_gd.gateway.workshop_uploader import upload_file_to_cloud

        result = await upload_file_to_cloud(
            upload_base,
            upload_token,
            local_path,
            relative_path=relative_path,
        )
        if result.ok:
            return {
                "status": "ok",
                "message": result.message,
                "remote_url": result.remote_url,
                "remote": result.response or {},
            }
        return {
            "status": "error",
            "message": result.message,
            "remote": result.response or {},
        }

    # ---- 数据上传配置与执行 ----

    @app.get("/api/upload-profiles")
    async def list_upload_profiles() -> dict[str, Any]:
        """返回上传配置。敏感字段和 WiFi 密码不会出现在响应里。"""
        from spider_gd.gateway.data_upload_flow import list_public_profiles

        return {"profiles": list_public_profiles(service._ctx.state_store)}

    @app.put("/api/upload-profiles")
    async def put_upload_profiles(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """整包保存上传配置。敏感字段留空时沿用已加密的旧值。"""
        from spider_gd.gateway.data_upload_flow import replace_profiles

        incoming = payload.get("profiles", [])
        if not isinstance(incoming, list):
            raise HTTPException(status_code=400, detail="profiles 必须是数组")
        try:
            profiles = replace_profiles(service._ctx.state_store, incoming)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "profiles": profiles}

    @app.post("/api/upload-profiles/test-upload")
    async def test_upload_profile(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """向接口上传一个小测试文件，不切换 WiFi。"""
        from spider_gd.gateway.data_upload_flow import run_test_upload

        try:
            return await run_test_upload(service._ctx.state_store, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/upload-profiles/test-wifi")
    async def test_upload_wifi(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """测试切换到配置里的 WiFi，已连接则直接提示，否则切过去再切回。"""
        from spider_gd.gateway.data_upload_flow import run_test_wifi

        interface_name = str(service._ctx.state_store.get_setting("wifiInterfaceName") or "")
        try:
            return await asyncio.to_thread(
                run_test_wifi,
                service._ctx.state_store,
                payload,
                None,
                interface_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/data-upload/stream")
    async def stream_data_upload(payload: dict[str, Any] = Body(...)):
        """按选定配置上传文件或文件夹，用 SSE 推送整体进度和每个文件的状态。"""
        from spider_gd.gateway.data_upload_flow import iter_upload_events

        profile_id = str(payload.get("profileId") or payload.get("profile_id") or "").strip()
        raw_paths = payload.get("paths") or []
        if not profile_id:
            raise HTTPException(status_code=400, detail="请选择上传配置")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise HTTPException(status_code=400, detail="请选择要上传的文件或文件夹")
        paths = [str(item) for item in raw_paths if str(item).strip()]
        exec_id = str(payload.get("execId") or payload.get("exec_id") or "").strip()
        log_name = str(payload.get("name") or "数据上传").strip() or "数据上传"
        interface_name = str(service._ctx.state_store.get_setting("wifiInterfaceName") or "")

        async def _generate():
            async for event in iter_upload_events(
                service._ctx.state_store,
                profile_id=profile_id,
                paths=paths,
                exec_id=exec_id,
                interface_name=interface_name,
                log_name=log_name,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/workflows/{name}/run/stream")
    async def post_workflow_stream(name: str, body: WorkflowRunRequest):
        return await run_workflow_stream(name, cert_password=body.cert_password,
            account=body.account, account_password=body.account_password,
            download_dir=body.download_dir, headless=body.headless, minimized=body.minimized)

    @app.get("/api/tasks/{name}/run/stream")
    async def get_task_stream(name: str, cert_password: str = "", account: str = "", account_password: str = "", download_dir: str = "", headless: str = "false", minimized: str = "false", reference_date: str = ""):
        return await run_task_stream(name, cert_password=cert_password, account=account,
            account_password=account_password, download_dir=download_dir, headless=headless,
            minimized=minimized, reference_date=reference_date)

    @app.post("/api/tasks/{name}/run/stream")
    async def post_task_stream(name: str, body: WorkflowRunRequest):
        return await run_task_stream(name, cert_password=body.cert_password,
            account=body.account, account_password=body.account_password,
            download_dir=body.download_dir, headless=body.headless, minimized=body.minimized, reference_date=body.reference_date)

    @app.post("/api/diagnostics/bundle")
    async def download_diagnostics_bundle(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        """打包最近两天的日志、执行记录和失败现场，密钥打星后保存到下载目录。

        Args:
            payload: 可选 ``download_dir``。空则写到数据目录下的「诊断包」文件夹。

        Returns:
            dict[str, Any]: ``path`` 是 zip 路径，``filename`` 是文件名。
        """
        from spider_gd.config.user_home import bundled_workflows_dir, tasks_dir, user_home, user_workflows_dir
        from spider_gd.gateway.diagnostics_bundle import build_diagnostics_zip

        body = payload or {}
        settings = service._ctx.state_store.get_all_settings()
        data_dir = Path(service._ctx.settings.storage.data_dir)
        requested = str(body.get("download_dir") or body.get("downloadDir") or "").strip()
        dl_dir = Path(requested) if requested else data_dir
        out_dir = dl_dir / "诊断包"
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"diagnostics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path = out_dir / filename
        extra_secrets = _diagnostic_secret_values(service._ctx.state_store)
        network, wifi_ssid, ukey = await asyncio.to_thread(_diagnostic_host_facts, service)
        download_dirs = [data_dir]
        if requested:
            download_dirs.append(Path(requested))
        saved_download = str(settings.get("downloadDir") or "").strip()
        if saved_download:
            download_dirs.append(Path(saved_download))
        notes = await asyncio.to_thread(
            build_diagnostics_zip,
            zip_path,
            settings=settings,
            store=service._ctx.state_store,
            log_dir=Path(service._ctx.settings.log.file_dir),
            download_dirs=download_dirs,
            workflow_dirs=[user_workflows_dir(), bundled_workflows_dir()],
            tasks_dir=tasks_dir(),
            install_path=str(Path.cwd()),
            data_path=str(data_dir),
            sqlite_path=str(service._ctx.settings.storage.sqlite_path),
            user_home=str(user_home() or ""),
            app_version=str(body.get("app_version") or ""),
            extra_secrets=extra_secrets,
            network=network,
            wifi_ssid=wifi_ssid,
            ukey=ukey,
        )
        logger.info("诊断包已生成: {} ({})", zip_path, "; ".join(notes) or "ok")
        return {"status": "ok", "path": str(zip_path), "filename": filename}

    @app.post("/api/executions/{exec_id}/resume/stream")
    async def resume_execution_stream(exec_id: str, body: WorkflowRunRequest | None = None):
        """从某次中断的执行继续：跳过已经成功的指标日期，重跑其余项。

        请求体里的 ``slots`` 非空时只重跑这些键（用于「重试失败项」）。
        补数据的快照同样可用，不另建定时任务。
        """
        plan = service._ctx.state_store.get_resume_plan(exec_id)
        if not plan:
            raise HTTPException(status_code=404, detail="这次执行没有可继续的任务快照")
        cfg = dict(plan)
        cfg["_resume_from"] = exec_id
        cfg["_skip_slots"] = sorted(service._ctx.state_store.succeeded_slots(exec_id))
        slots = list(getattr(body, "slots", None) or [])
        if slots:
            cfg["_only_slots"] = [str(item) for item in slots if str(item).strip()]
        name = str(cfg.get("_control_name") or "resume")
        request = body or WorkflowRunRequest()
        return await run_task_stream(
            name,
            cert_password=request.cert_password,
            account=request.account,
            account_password=request.account_password,
            download_dir=request.download_dir,
            headless=request.headless,
            minimized=request.minimized,
            reference_date=request.reference_date,
            prepared=cfg,
        )

    @app.post("/api/catchup/run/stream")
    async def run_catchup_stream(body: CatchupRunRequest):
        """按指定指标和日期补跑一次。不创建、不修改定时任务。"""
        import sys as _sys

        _tools = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        if str(_tools) not in _sys.path:
            _sys.path.insert(0, str(_tools))
        from task_runner import apply_catchup_storage, build_catchup_config, catchup_download_dir

        try:
            cfg = build_catchup_config(
                login=body.login,
                indicators=body.indicators,
                target_dates=body.target_dates,
                target_months=body.target_months,
                indicator_dates=body.indicator_dates,
                indicator_months=body.indicator_months,
                output_dates=body.output_dates,
            )
            cfg["upload_profile_id"] = (body.upload_profile_id or "").strip()
            cfg["auto_upload"] = bool(body.auto_upload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            task_rows = service._ctx.state_store.list_task_configs()
        except Exception:
            logger.exception("读取任务配置失败，补数将不沿用任务存储路径")
            task_rows = []
        cfg = apply_catchup_storage(
            cfg,
            task_rows,
            path_template=body.path_template,
            path_folders=body.path_folders,
            path_structure=body.path_structure,
            download_dir=body.download_dir,
            storage_overridden=body.storage_overridden,
        )
        return await run_task_stream(
            "catchup",
            cert_password=body.cert_password,
            account=body.account,
            account_password=body.account_password,
            download_dir=catchup_download_dir(cfg),
            headless=body.headless,
            minimized=body.minimized,
            prepared=cfg,
        )

    @app.post("/api/workflows/{name}/step/{step_index}/run/stream")
    async def post_step_stream(name: str, step_index: int, body: WorkflowRunRequest):
        return await run_workflow_step_stream(name, step_index, mode=body.mode,
            cert_password=body.cert_password, download_dir=body.download_dir,
            headless=body.headless, skip_steps=body.skip_steps)

    # ---- 共享浏览器 ----

    @app.post("/api/browser/open")
    async def open_shared_browser() -> dict[str, Any]:
        """启动共享 CDP 浏览器。如果已在运行则返回当前状态。"""
        from spider_gd.core.crawler.shared_browser import get_shared_browser

        sb = get_shared_browser()
        if sb.is_running:
            return {"status": "ok", "message": "浏览器已在运行", "browser": sb.status()}
        try:
            domain = service._ctx.settings.platform.domain
            url = f"https://{domain}/portal/#/home"
            if sb._config["mode"] != "managed":
                from spider_gd.core.crawler.shared_browser import default_profile_dir
                sb.configure(mode="managed", profile_dir=str(default_profile_dir()))
            await sb.start(url)
            return {"status": "ok", "message": "浏览器已启动", "browser": sb.status()}
        except Exception as e:
            logger.error("启动共享浏览器失败: {}", e)
            return {"status": "error", "message": str(e)}

    @app.post("/api/browser/close")
    async def close_shared_browser() -> dict[str, Any]:
        """关闭共享浏览器。"""
        from spider_gd.core.crawler.shared_browser import get_shared_browser

        sb = get_shared_browser()
        try:
            await sb.close()
            return {"status": "ok", "message": "浏览器已关闭或断开"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @app.get("/api/browser/status")
    async def shared_browser_status() -> dict[str, Any]:
        """查询共享浏览器状态。"""
        from spider_gd.core.crawler.shared_browser import get_shared_browser

        return get_shared_browser().status()

    @app.post("/api/browser/connect")
    async def connect_browser(body: BrowserConnectionRequest):
        from spider_gd.core.crawler.shared_browser import get_shared_browser, default_profile_dir
        sb = get_shared_browser()
        try:
            values = body.model_dump()
            if not values["profile_dir"]:
                values["profile_dir"] = str(default_profile_dir())
            sb.configure(**values)
            await sb.ensure_connected()
            pages = await sb.follow_active_page()
            return {"status": "ok", "browser": sb.status(), "pages": pages}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @app.get("/api/browser/pages")
    async def browser_pages():
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        return await get_shared_browser().follow_active_page()

    @app.post("/api/browser/select")
    async def select_browser_page(body: BrowserPageRequest):
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        try:
            await get_shared_browser().select_page(body.page_id)
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @app.post("/api/browser/detach")
    async def detach_browser():
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        try:
            await get_shared_browser().detach()
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @app.post("/api/browser/reset")
    async def reset_browser_session():
        """清掉当前绑定页的 cookie 并刷新该页。"""
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        try:
            return {"status": "ok", **await get_shared_browser().reset_cookies_and_reload()}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    @app.post("/api/browser/auth/check")
    async def check_browser_auth():
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        return await get_shared_browser().check_auth()

    @app.post("/api/browser/resume")
    async def resume_browser_run(body: BrowserRunControlRequest):
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        try:
            get_shared_browser().resume(body.run_id, body.pause_version)
            return {"status": "ok"}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    @app.post("/api/browser/cancel")
    async def cancel_browser_run(body: BrowserRunControlRequest):
        from spider_gd.core.crawler.shared_browser import get_shared_browser
        try:
            get_shared_browser().cancel(body.run_id)
            return {"status": "ok"}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

    # ---- 步骤级播放 ----

    @app.get("/api/workflows/{name}/step/{step_index}/run/stream")
    async def run_workflow_step_stream(
        name: str,
        step_index: int,
        mode: str = "single",
        cert_password: str = "",
        download_dir: str = "",
        headless: str = "false",
        skip_steps: str = "",
    ):
        """SSE 流式执行工作流的单个/从某步骤开始的步骤。

        mode:
          single    — 仅执行第 step_index 步
          from_here — 从第 step_index 步开始执行到结束
          to_here   — 执行到第 step_index 步
        skip_steps — 逗号分隔的步骤索引列表（1-based），在 from_here/to_here 模式下跳过这些步骤
        """
        import asyncio as _asyncio
        import sys as _sys

        _tools = Path(__file__).resolve().parent.parent.parent.parent.parent / "tools"
        if str(_tools) not in _sys.path:
            _sys.path.insert(0, str(_tools))
        from workflow_runner import WorkflowRunner
        from spider_gd.core.crawler.shared_browser import get_shared_browser

        # 加载工作流
        db_cfg = service._ctx.state_store.get_workflow_config(name)
        if not db_cfg or not db_cfg.get("yaml_text"):
            path = _wf_path(name)
            if not path.exists():
                raise HTTPException(status_code=404, detail="工作流不存在")
            raw = path.read_text(encoding="utf-8")
        else:
            raw = db_cfg["yaml_text"]
        for placeholder, val in [("${CERT_PASSWORD}", cert_password)]:
            if val:
                raw = raw.replace(placeholder, val)
        cfg = yaml.safe_load(raw) or {}
        steps = list(cfg.get("steps", []))

        if step_index < 1 or step_index > len(steps):
            raise HTTPException(status_code=400, detail=f"步骤索引 {step_index} 超出范围 1-{len(steps)}")

        if mode == "from_here":
            target_steps = steps[step_index - 1:]
        elif mode == "to_here":
            target_steps = steps[:step_index]
        else:
            target_steps = [steps[step_index - 1]]

        # 按原始步骤编号过滤，保持界面上的步骤索引一致。
        if skip_steps and mode in ("from_here", "to_here"):
            disabled = {int(v.strip()) for v in skip_steps.split(",") if v.strip().isdigit()}
            first = step_index if mode == "from_here" else 1
            target_steps = [step for index, step in enumerate(target_steps, first) if index not in disabled]

        # 保留原始步骤编号；登录状态由 WorkflowRunner.run 在执行前核验。

        async def event_stream():
            if not get_shared_browser().is_running:
                yield f"data: {json.dumps({'event': 'error', 'error': '共享浏览器未启动，请先连接浏览器'}, ensure_ascii=False)}\n\n"
                return
            queue = _asyncio.Queue()
            runner = WorkflowRunner({**cfg, "steps": target_steps, "_control_name": name, "_partial_run": True},
                download_dir=(download_dir
                    or str(service._ctx.state_store.get_setting("downloadDir") or "")
                    or str(service._ctx.settings.storage.data_dir)))
            alerted = set()

            def on_event(data):
                queue.put_nowait(data)
                if data.get("event") in ("error_pause", "step_error", "workflow_error"):
                    error = data.get("error") or data.get("message") or "执行异常"
                    if error not in alerted:
                        alerted.add(error)
                        _asyncio.create_task(service._ctx.alert.send_alert(
                            AlertLevel.ERROR, f"工作流 {name} 已暂停或失败：{error}", event_type="error"))

            async def execute():
                try:
                    await runner.run(on_event=on_event)
                except Exception as exc:
                    on_event({"event": "workflow_error", "error": str(exc)})

            task = _asyncio.create_task(execute())
            _browser_background_tasks.add(task)
            task.add_done_callback(_browser_background_tasks.discard)
            # 页面断开不会放行暂停或重放任务，仍可通过浏览器控制面查看 / 取消。
            while not task.done() or not queue.empty():
                try:
                    data = await _asyncio.wait_for(queue.get(), timeout=0.5)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except _asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
            await task

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if enable_web_ui:
        from fastapi.responses import HTMLResponse

        @app.get("/", response_class=HTMLResponse)
        async def dashboard() -> str:
            return _DASHBOARD_HTML

    if enable_mcp and mcp_app is not None:
        app.mount("/mcp", mcp_app)
        logger.info("MCP 端点已挂载于 /mcp（streamable-http）")

    return app


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>广东电力采集系统 · 控制台</title>
<style>
  :root { --bg:#0f1722; --card:#18222f; --line:#26313f; --fg:#e6edf3; --muted:#8b98a8;
          --ok:#2ea043; --warn:#d29922; --err:#f85149; --accent:#388bfd; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:"Microsoft YaHei",system-ui,sans-serif; background:var(--bg); color:var(--fg);
         padding:24px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px; }
  .card h2 { font-size:14px; color:var(--muted); margin-bottom:12px; font-weight:600; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; }
  .ok { background:rgba(46,160,67,.15); color:var(--ok); }
  .warn { background:rgba(210,153,34,.15); color:var(--warn); }
  .err { background:rgba(248,81,73,.15); color:var(--err); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  button { background:var(--accent); color:#fff; border:none; padding:9px 16px; border-radius:6px;
           font-size:13px; cursor:pointer; }
  button:hover { opacity:.9; }
  .row { display:flex; gap:8px; align-items:center; margin-bottom:12px; }
  input { background:#0d1420; border:1px solid var(--line); color:var(--fg); padding:8px;
          border-radius:6px; font-size:13px; }
</style>
</head>
<body>
  <h1>广东电力交易数据采集系统</h1>
  <div class="sub">控制台 · 单机双网卡部署</div>
  <div class="grid">
    <div class="card">
      <h2>系统健康</h2>
      <div id="health">加载中…</div>
    </div>
    <div class="card">
      <h2>手动触发采集</h2>
      <div class="row">
        <input id="date" type="date">
        <button onclick="startCrawl()">立即采集</button>
      </div>
      <div id="crawlResult" class="sub"></div>
    </div>
  </div>
  <div class="card" style="margin-top:16px;">
    <h2>最近任务</h2>
    <table>
      <thead><tr><th>任务</th><th>状态</th><th>创建时间</th><th>完成时间</th></tr></thead>
      <tbody id="tasks"><tr><td colspan="4">加载中…</td></tr></tbody>
    </table>
  </div>
<script>
const pill = s => {
  const c = s==='healthy'||s==='completed'?'ok':(s==='critical'||s==='failed'?'err':'warn');
  return `<span class="pill ${c}">${s}</span>`;
};
async function loadHealth(){
  const r = await (await fetch('/api/health')).json();
  document.getElementById('health').innerHTML =
    pill(r.overall) + '<div class="sub" style="margin-top:8px">' +
    r.checks.map(c=>`${c.module}: ${c.message}`).join('<br>') + '</div>';
}
async function loadTasks(){
  const r = await (await fetch('/api/tasks')).json();
  const tb = document.getElementById('tasks');
  tb.innerHTML = r.tasks.length ? r.tasks.map(t=>
    `<tr><td>${t.name}</td><td>${pill(t.status)}</td>` +
    `<td>${t.created_at||''}</td><td>${t.finished_at||''}</td></tr>`
  ).join('') : '<tr><td colspan="4">暂无任务</td></tr>';
}
async function startCrawl(){
  const date = document.getElementById('date').value || null;
  document.getElementById('crawlResult').textContent = '采集中…';
  const r = await (await fetch('/api/crawl/start',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({target_date:date})})).json();
  document.getElementById('crawlResult').textContent =
    '结果：' + (r.status||JSON.stringify(r));
  loadTasks();
}
loadHealth(); loadTasks(); setInterval(()=>{loadHealth();loadTasks();}, 15000);
</script>
</body>
</html>"""
