"""共享浏览器：单例 CDP Chrome，跨工作流复用。

提供：
- :func:`start` — 启动 CDP Chrome 并连接，导航到指定 URL
- :func:`close` — 关闭 Chrome
- :func:`status` — 返回运行状态和当前页面 URL
- :func:`inject_into` — 将浏览器状态注入 WorkflowRunner
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

# 完整性级别 RID：Low=0x1000、Medium=0x2000（Mandatory Integrity Control）
_IL_LOW = 0x1000
_IL_MEDIUM = 0x2000

# 门户工作台与现货子系统使用不同组件；用户栏来自 10291600 现场 DOM。
GD_WORKBENCH_USER = '[class*="userInfoContainer"] [class*="userName"]'
GD_LOGIN_SUCCESS_SELECTOR = f".ant-menu-horizontal, .el-menu, {GD_WORKBENCH_USER}"


def _migrate_login_selector(config: dict[str, Any]) -> None:
    selector = str(config.get("success_selector") or "")
    parts = [part.strip() for part in selector.split(",")]
    legacy_defaults = ([".el-menu"], [".ant-menu-horizontal", ".el-menu"])
    if config.get("target_origin") == "https://pm.gd.csg.cn" and parts in legacy_defaults:
        config["success_selector"] = GD_LOGIN_SUCCESS_SELECTOR


def _windows_uac_disabled() -> bool:
    """UAC 关闭时 Chromium 沙箱无法初始化。"""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
        ) as key:
            return winreg.QueryValueEx(key, "EnableLUA")[0] == 0
    except OSError:
        return False


def _windows_process_integrity() -> int | None:
    """返回当前进程令牌的完整性级别 RID（Low=0x1000、Medium=0x2000…）。

    进程的完整性级别取自可执行文件上的强制标签：程序文件若带 Low 标签
    （``icacls <dir> /setintegritylevel L``，可被整个目录继承），进程就会以
    Low 完整性运行。低完整性进程无法初始化 Chrome 沙箱，浏览器会在启动
    瞬间以 0x80000003 (STATUS_BREAKPOINT) 退出。
    """
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wt.HANDLE
    advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi32.OpenProcessToken.restype = wt.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wt.BOOL

    token = wt.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None
    try:
        needed = wt.DWORD()
        advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 25, buf, needed, ctypes.byref(needed)):
            return None
        # TOKEN_MANDATORY_LABEL.Label.Sid → SID 的 SubAuthority[0]
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        if not sid:
            return None
        return ctypes.cast(sid + 8, ctypes.POINTER(ctypes.c_uint32))[0]
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(token)


def _chrome_exit_hint() -> str:
    """Chrome 启动即崩（0x80000003）时补充本机环境线索，便于定位而不改启动参数。"""
    if sys.platform != "win32":
        return ""
    if _windows_uac_disabled():
        return "检测到 Windows UAC 已关闭（EnableLUA=0），Chromium 沙箱无法初始化"
    integrity = _windows_process_integrity()
    if integrity is not None and integrity < _IL_MEDIUM:
        return (
            f"检测到当前进程完整性级别过低（0x{integrity:04X}），Chrome 沙箱无法初始化；"
            '通常是项目目录被打了 Low 完整性标签，可用 icacls <项目目录> '
            '/setintegritylevel "(OI)(CI)M" 恢复'
        )
    return ""


def _headless_requested() -> bool:
    import os

    return os.environ.get("SPIDER_BROWSER_HEADLESS", "").lower() in ("1", "true", "yes")


def _allocate_cdp_port() -> int:
    """由系统分配空闲端口，再把非零端口显式传给 Chrome。

    Chrome 的 --remote-debugging-port=0 会启用自动化标记，导致门户空白。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _build_launch_args(
    chrome_path: str, profile: Path, *, headless: bool, cdp_port: int
) -> list[str]:
    """普通方式启动本机 Chrome：保留默认沙箱，不追加 --no-sandbox 之类的开关。"""
    if not 0 < cdp_port < 65536:
        raise ValueError("CDP 必须使用非零有效端口")
    args = [
        chrome_path,
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # 目标站点用瑞数动态令牌，令牌由页面自身定时器/心跳维护。
        # 窗口被遮挡或标签被判定为后台时 Chrome 会节流渲染与定时器，
        # 令牌随之过期，之后的 XHR 会得到网关的 400 空响应（text/html 6 字节）。
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        # 证书/UKey 控件在 127.0.0.1:10443 建 WebSocket。Chrome 147+ 的
        # 本地网络访问检查会以 ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS
        # 拦掉它，页面因此报“数字证书连接失败”。该检查只影响公网页面访问
        # 本机地址，这里关闭的是这一项特性，不关闭其他安全特性。
        "--disable-features=CalculateNativeWinOcclusion,LocalNetworkAccessChecks",
    ]
    if headless:
        args.append("--headless=new" if sys.platform != "win32" else "--headless")
    return args


def _get_chrome_path() -> str | None:
    if sys.platform == "win32":
        for p in [
            "%PROGRAMFILES%\\Google\\Chrome\\Application\\chrome.exe",
            "%PROGRAMFILES(X86)%\\Google\\Chrome\\Application\\chrome.exe",
            "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe",
        ]:
            import os

            expanded = os.path.expandvars(p)
            if os.path.exists(expanded):
                return expanded
        return None
    elif sys.platform == "darwin":
        path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        return path if Path(path).exists() else None
    else:
        import shutil

        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                return found
        return None


def default_profile_dir() -> Path:
    import os
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SpiderGD/browser-profiles/default"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/SpiderGD/browser-profiles/default"
    return Path.home() / ".local/share/spider-gd/browser-profiles/default"


# 专用浏览器没打开业务站、停在 Chrome 错误页时给操作人员的说明。
PAGE_LOAD_FAILED_MESSAGE = (
    "业务页面加载失败（chrome-error），请在专用浏览器中刷新并确认 U-Key 证书客户端已运行后重新登录"
)


def is_browser_error_page(url: str) -> bool:
    """判断标签是浏览器错误页，不能当作业务页。

    ``about:blank`` 不算。专用浏览器启动时会短暂停在空白页再跳转。

    Args:
        url: 标签地址，例如 ``chrome-error://chromewebdata/``。

    Returns:
        bool: Chrome / Edge 错误页、崩溃页或 ``chromewebdata`` 时为 True。

    Examples:
        >>> is_browser_error_page("chrome-error://chromewebdata/")
        True
        >>> is_browser_error_page("https://pm.gd.csg.cn/portal/#/home")
        False
        >>> is_browser_error_page("about:blank")
        False
    """
    text = (url or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "chromewebdata" in lowered:
        return True
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme in {"chrome-error", "edge-error"}:
        return True
    host = (parts.netloc or "").lower()
    path = (parts.path or "").lower()
    chrome_hosts = {"crash", "kill", "hang", "badcastcrash", "system"}
    blank_error = scheme == "about" and path == "neterror"
    crashed = scheme == "chrome" and host in chrome_hosts
    return blank_error or crashed


def is_public_portal_url(url: str) -> bool:
    """广东门户首页和登录页也有导航菜单，不能据此认定已登录。"""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    route = parts.fragment.split("?", 1)[0].rstrip("/")
    return (parts.hostname == "pm.gd.csg.cn" and parts.path.rstrip("/") == "/portal"
            and route in ("", "/home", "/login", "/user/login"))


def startup_url(target_origin: str, entry_url: str = "") -> str:
    """业务站点只保留 origin，再拼入口。

    target_origin 若已经带路径或 hash，先收成 scheme://host，
    避免再拼出 ``#/home/portal/#/home``。entry_url 有值时单独作为入口。
    """
    from urllib.parse import urlsplit

    parts = urlsplit((target_origin or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("业务站点必须是 http/https 地址")
    origin = f"{parts.scheme}://{parts.netloc}"
    entry = (entry_url or "").strip()
    if not entry:
        return origin + "/portal/#/home"
    entry_parts = urlsplit(entry)
    if entry_parts.scheme or entry_parts.netloc:
        if f"{entry_parts.scheme}://{entry_parts.netloc}" != origin:
            raise ValueError("入口地址必须属于业务站点")
        return entry
    suffix = entry if entry.startswith("/") else "/" + entry
    return origin + suffix


def validate_local_endpoint(endpoint: str) -> None:
    from urllib.parse import urlsplit
    value = urlsplit(endpoint)
    if (value.scheme not in ("http", "https", "ws", "wss")
            or value.hostname not in ("127.0.0.1", "localhost", "::1")
            or value.username or value.password or value.fragment):
        raise ValueError("只允许本机 loopback Chrome 调试端点")
    if not value.port:
        raise ValueError("请指定 Chrome 调试端口")


def read_authorized_endpoint(profile: Path) -> str:
    import re
    profile = profile.expanduser()
    endpoint_file = profile / "DevToolsActivePort"
    if not endpoint_file.is_file() and re.fullmatch(r"Default|Profile \d+|Guest Profile", profile.name):
        # chrome://version 显示的是具体 Profile；调试端点写在其上一级用户数据根目录。
        profile = profile.parent
        endpoint_file = profile / "DevToolsActivePort"
    if not endpoint_file.is_file():
        raise FileNotFoundError(
            f"未找到 Chrome 授权调试端点：{endpoint_file}。"
            "请先在 Chrome 的 chrome://inspect/#remote-debugging 启用远程调试，"
            "并填写用户数据根目录（不包含末尾的 Default / Profile 目录）。"
        )
    parts = endpoint_file.read_text(encoding="utf-8").splitlines()
    if len(parts) < 2 or not parts[0].isdigit() or not 0 < int(parts[0]) < 65536:
        raise ValueError("DevToolsActivePort 端口无效，请在 Chrome 启用远程调试")
    if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", parts[1]):
        raise ValueError("DevToolsActivePort 路径无效")
    return f"ws://127.0.0.1:{int(parts[0])}{parts[1]}"


class SharedBrowser:
    """本机浏览器会话。接管已有 Context，任务独占，外部进程永不关闭。"""

    def __init__(self, config_path: Path | None = None) -> None:
        self._browser: Any = None
        self._context: Any = None
        self._pages: list[Any] = []
        self._chrome_proc: subprocess.Popen | None = None
        self._playwright: Any = None
        self._current_page_idx = 0
        self._profile_dir: Path | None = None
        self._managed_download_dir: Path | None = None
        self._download_root: Path | None = None
        self._managed_download_tasks: set[asyncio.Task[Any]] = set()
        self._chrome_log: Path | None = None
        self._chrome_log_file: Any = None
        self._page_ids: dict[Any, str] = {}
        self._visibility_probes: dict[Any, tuple[Any, int]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._config_path = config_path
        self._config: dict[str, Any] = {
            "mode": "managed", "endpoint": "http://127.0.0.1:9222",
            "profile_dir": str(default_profile_dir()), "auth_policy": "reuse_only",
            "target_origin": "https://pm.gd.csg.cn",
            "success_selector": GD_LOGIN_SUCCESS_SELECTOR,
            "identity_selector": "", "expected_identity": "",
        }
        self._run: dict[str, Any] | None = None
        self._resume_event = asyncio.Event()
        self._cancel_requested = False
        self._active_runner: Any = None
        if config_path and config_path.exists():
            import json
            try:
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self._config.update(saved.get("config", {}))
                # 旧默认值只认现货页的 Element 菜单；门户工作台使用 Ant 菜单。
                # 只迁移原默认值，保留用户自行设置的成功标志。
                _migrate_login_selector(self._config)
                self._run = saved.get("run")
                if self._run and self._run.get("state") in ("running", "paused"):
                    self._run.update(state="interrupted", reason="软件已重启，请重新连接并检查现场后重新执行")
            except (OSError, ValueError):
                logger.warning("浏览器配置读取失败，使用默认配置")

    def _save(self) -> None:
        if self._config_path is None:
            return
        import json
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"config": self._config, "run": self._run}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._config_path)

    def configure(self, **values: Any) -> None:
        self._require_idle()
        if self._lifecycle_lock.locked():
            raise RuntimeError("浏览器正在连接或断开，请稍后重试")
        allowed = set(self._config)
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"未知浏览器配置: {sorted(unknown)}")
        merged = {**self._config, **values}
        if merged["mode"] not in ("managed", "cdp", "authorized"):
            raise ValueError("不支持的浏览器连接方式")
        if merged["auth_policy"] not in ("reuse_only", "reuse_or_auto", "manual"):
            raise ValueError("不支持的登录策略")
        validate_local_endpoint(merged["endpoint"])
        from urllib.parse import urlsplit
        origin = urlsplit(str(merged["target_origin"]).strip())
        if origin.scheme not in ("http", "https") or not origin.netloc:
            raise ValueError("业务站点必须是 http/https 地址")
        merged["target_origin"] = f"{origin.scheme}://{origin.netloc}"
        _migrate_login_selector(merged)
        if merged["expected_identity"] and not merged["identity_selector"]:
            raise ValueError("核对业务身份需要填写身份元素选择器")
        if self.is_running and any(merged[k] != self._config[k] for k in ("mode", "endpoint", "profile_dir")):
            raise RuntimeError("请先断开当前浏览器再修改连接方式")
        self._config = merged
        self._save()

    @property
    def is_running(self) -> bool:
        return self._browser is not None and bool(self._browser.is_connected())

    def _bind_page(self, page: Any) -> None:
        """把这一页设成当前业务页。

        Args:
            page: Playwright 页面对象。

        Returns:
            None: 结果写在实例上。

        Examples:
            >>> browser = SharedBrowser()
            >>> page = type("P", (), {"context": "ctx"})()
            >>> browser._bind_page(page)
            >>> browser._pages == [page] and browser._context == "ctx"
            True
        """
        self._context = page.context
        self._pages = [page]
        self._current_page_idx = 0

    def _bind_failed_page_if_alone(self) -> None:
        """只有错误页时也绑定它，让界面能标出加载失败。

        旁边还有正常网页时不改绑定，避免把错误页盖过业务页。

        Returns:
            None: 没有单独的错误页时保持原绑定。

        Examples:
            >>> callable(SharedBrowser._bind_failed_page_if_alone)
            True
        """
        live = [page for page in self._page_ids if not page.is_closed()]
        failed = [
            page for page in live
            if is_browser_error_page(str(getattr(page, "url", "") or ""))
        ]
        if live and len(failed) == len(live):
            self._bind_page(failed[-1])
            logger.warning("专用浏览器当前页加载失败：{}", getattr(failed[-1], "url", ""))

    @property
    def page(self) -> Any:
        if not self.is_running:
            raise RuntimeError("浏览器未连接或尚未选择业务页面")
        if self._pages:
            page = self._pages[self._current_page_idx]
            if page.is_closed():
                raise RuntimeError("已绑定的业务页面被关闭，请重新选择页面")
            url = str(getattr(page, "url", "") or "")
            if is_browser_error_page(url):
                raise RuntimeError(PAGE_LOAD_FAILED_MESSAGE)
            return page
        live = [page for page in self._page_ids if not page.is_closed()]
        failed = [
            page for page in live
            if is_browser_error_page(str(getattr(page, "url", "") or ""))
        ]
        if live and len(failed) == len(live):
            raise RuntimeError(PAGE_LOAD_FAILED_MESSAGE)
        raise RuntimeError("浏览器未连接或尚未选择业务页面")

    def _require_idle(self) -> None:
        if self._execution_lock.locked():
            raise RuntimeError("浏览器正被任务占用，请先结束当前任务")

    def status(self) -> dict[str, Any]:
        current_url = ""
        if self._pages and self._current_page_idx < len(self._pages):
            with contextlib.suppress(Exception):
                current_url = self._pages[self._current_page_idx].url
        return {
            "running": self.is_running, "page_count": len(self._page_ids) or len(self._pages),
            "current_url": current_url, "current_page_idx": self._current_page_idx,
            "selected_page_id": self._page_ids.get(self._pages[self._current_page_idx]) if self._pages else None,
            "owns_process": self._chrome_proc is not None and self._chrome_proc.poll() is None,
            "pid": self._chrome_proc.pid if self._chrome_proc else None,
            "download_dir": str(self._managed_download_dir) if self.is_running and self._managed_download_dir else "",
            "busy": self._execution_lock.locked(), "run": self._run,
            "config": dict(self._config),
        }

    async def list_pages(self) -> list[dict[str, Any]]:
        if not self.is_running:
            return []
        import uuid
        result = []
        live = set()
        for context_index, context in enumerate(self._browser.contexts):
            for page in context.pages:
                if page.is_closed():
                    continue
                live.add(page)
                page_id = self._page_ids.setdefault(page, uuid.uuid4().hex)
                title = ""
                with contextlib.suppress(Exception):
                    title = await asyncio.wait_for(page.title(), 2)
                url = str(page.url or "")
                result.append({
                    "id": page_id,
                    "context": context_index,
                    "title": title,
                    "url": url,
                    "load_error": is_browser_error_page(url),
                })
        self._page_ids = {p: key for p, key in self._page_ids.items() if p in live}
        for page in [page for page in self._visibility_probes if page not in self._page_ids]:
            await self._discard_visibility_probe(page)
        return result

    async def select_page(self, page_id: str) -> None:
        async with self._lifecycle_lock:
            self._require_idle()
            await self.list_pages()
            self._require_idle()
            page = next((p for p, key in self._page_ids.items() if key == page_id), None)
            if page is None:
                raise ValueError("页面已关闭或不属于当前连接，请刷新页面列表")
            self._context = page.context
            self._pages = [page]
            self._current_page_idx = 0

    async def follow_active_page(self) -> list[dict[str, Any]]:
        """空闲时把绑定页改成专用浏览器里当前可见的网页标签，并返回页面列表。

        任务占用期间不改绑定。读取可见性走独立脚本环境，不进页面自己的脚本。
        多个窗口同时各有一个可见标签时不猜测，保持原绑定。
        """
        if not self.is_running:
            return []
        if self._execution_lock.locked() or self._lifecycle_lock.locked():
            return await self.list_pages()
        async with self._lifecycle_lock:
            if self._execution_lock.locked() or not self.is_running:
                return await self.list_pages()
            pages = await self.list_pages()
            page = await self._foreground_http_page()
            if page is not None and self._pages != [page]:
                self._context = page.context
                self._pages = [page]
                self._current_page_idx = 0
                logger.info("已跟随浏览器当前标签页: {}", self._page_label(page))
            return pages

    async def reset_cookies_and_reload(self) -> dict[str, Any]:
        """清掉绑定页站点的 cookie 再刷新该页。

        现场常见的半登录/残留会话靠这一下回到干净状态。
        httpOnly cookie 要走 CDP ``Storage.clearDataForOrigin``；
        取不到 CDP 时退回上下文级的 clear_cookies。
        localStorage / sessionStorage 不动（那是工作流里的 reset_site_storage 的事）。
        """
        from urllib.parse import urlsplit

        async with self._lifecycle_lock:
            self._require_idle()
            page = self.page
            url = page.url or ""
            parts = urlsplit(url)
            if not parts.scheme.startswith("http"):
                raise RuntimeError(f"当前绑定页不是网页，无法重置刷新: {url or '空页'}")
            origin = f"{parts.scheme}://{parts.netloc}"

            cleared_by = ""
            try:
                cdp = await page.context.new_cdp_session(page)
                try:
                    await cdp.send(
                        "Storage.clearDataForOrigin",
                        {"origin": origin, "storageTypes": "cookies"},
                    )
                finally:
                    with contextlib.suppress(Exception):
                        await cdp.detach()
                cleared_by = "cdp"
            except Exception as exc:
                logger.warning("CDP 清 cookie 失败，退回 context.clear_cookies: {}", exc)
                await page.context.clear_cookies()
                cleared_by = "context"

            await page.reload(wait_until="domcontentloaded", timeout=30000)
            logger.info("已清 cookie 并刷新: {}（{}）", origin, cleared_by)
            return {"origin": origin, "url": page.url, "cleared_by": cleared_by}

    async def _foreground_http_page(self) -> Any:
        candidates = [
            page for page in self._page_ids
            if not page.is_closed() and str(getattr(page, "url", "")).startswith(("http://", "https://"))
        ]
        # Chrome can report hasFocus() and visibilityState as true for two tabs
        # in the same window. On macOS, ask Chrome which tab is actually selected.
        if sys.platform == "darwin" and len(candidates) > 1:
            active_urls = await self._macos_active_tab_urls()
            native_matches = [page for page in candidates if page.url in active_urls]
            if len(native_matches) == 1:
                return native_matches[0]
        states = await asyncio.gather(*(self._tab_focus(page) for page in candidates), return_exceptions=True)
        visible: list[Any] = []
        focused: list[Any] = []
        for page, state in zip(candidates, states):
            if not isinstance(state, tuple):
                continue
            is_visible, is_focused = state
            if is_focused:
                focused.append(page)
            elif is_visible:
                visible.append(page)
        if len(focused) == 1:
            return focused[0]
        if len(visible) == 1 and not focused:
            return visible[0]
        if not focused and not visible and len(candidates) > 1:
            # 浏览器在后台（最小化/被别的窗口盖住）时没有“当前标签”，
            # 各页的 hasFocus 全是 false。此时跟随最右侧（最新）的业务页：
            # 旧文档的动态签名可能已经失效，新文档才是有效的那份。
            flag = str(self._config.get("follow_newest_tab", "true")).lower()
            if flag not in ("false", "0", "off", "no"):
                target = str(self._config.get("target_origin") or "")
                if not target:
                    with contextlib.suppress(Exception):
                        binding_url = urlsplit(self._pages[0].url)
                        target = f"{binding_url.scheme}://{binding_url.netloc}"
                scoped = [
                    page for page in candidates
                    if target and str(getattr(page, "url", "")).startswith(target)
                ] or candidates
                return scoped[-1]
        return None

    async def _macos_active_tab_urls(self) -> set[str]:
        script = (
            'tell application "Google Chrome"\n'
            'set activeURLs to {}\n'
            'repeat with browserWindow in windows\n'
            'set end of activeURLs to URL of active tab of browserWindow\n'
            'end repeat\n'
            'set AppleScript\'s text item delimiters to linefeed\n'
            'return activeURLs as text\n'
            'end tell'
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run, ["osascript", "-e", script],
                capture_output=True, text=True, timeout=1.5, check=False,
            )
            if result.returncode == 0:
                return {url.strip() for url in result.stdout.splitlines() if url.strip()}
        except (OSError, subprocess.TimeoutExpired):
            pass
        return set()

    async def _tab_focus(self, page: Any) -> tuple[bool, bool] | None:
        try:
            return await asyncio.wait_for(self._read_tab_focus(page), 0.8)
        except Exception:
            await self._discard_visibility_probe(page)
            return None

    async def _read_tab_focus(self, page: Any) -> tuple[bool, bool]:
        session, context_id = self._visibility_probes.get(page, (None, None))
        if session is None:
            session = await page.context.new_cdp_session(page)
            tree = await session.send("Page.getFrameTree")
            world = await session.send("Page.createIsolatedWorld", {
                "frameId": tree["frameTree"]["frame"]["id"],
                "worldName": "spider-gd-tab-visibility",
            })
            context_id = world["executionContextId"]
            self._visibility_probes[page] = (session, context_id)
        result = await session.send("Runtime.evaluate", {
            "expression": (
                "(document.visibilityState==='visible'?'visible':'hidden')"
                "+ ' ' + (document.hasFocus()?'1':'0')"
            ),
            "contextId": context_id,
            "returnByValue": True,
        })
        value = str((result.get("result") or {}).get("value") or "")
        visible, _, focused = value.partition(" ")
        return visible == "visible", focused == "1"

    async def _discard_visibility_probe(self, page: Any) -> None:
        probe = self._visibility_probes.pop(page, None)
        if probe is None:
            return
        with contextlib.suppress(Exception):
            await probe[0].detach()

    @staticmethod
    def _page_label(page: Any) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(str(getattr(page, "url", "")))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment.split("?", 1)[0]))[:160]

    async def _connect(self, endpoint: str) -> None:
        from playwright.async_api import async_playwright
        import httpx
        validate_local_endpoint(endpoint)
        if endpoint.startswith(("http://", "https://")):
            probe_url = endpoint.rstrip("/") + "/json/version"
            try:
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
                    response = await client.get(probe_url)
                    response.raise_for_status()
                    endpoint = response.json()["webSocketDebuggerUrl"]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise RuntimeError(
                        f"{endpoint} 有程序响应，但没有提供 Chrome 调试接口（/json/version 返回 404）。"
                        "请用“打开专用浏览器”，或用独立用户数据目录及 --remote-debugging-port 启动 Chrome；"
                        "日常 Chrome 可尝试“Chrome 当前会话授权连接”。"
                    ) from exc
                raise RuntimeError(f"Chrome 调试接口返回 HTTP {exc.response.status_code}：{probe_url}") from exc
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                raise RuntimeError(
                    f"无法连接 {endpoint}。请确认 Chrome 已使用 --remote-debugging-port 启动，"
                    "或选择“打开专用浏览器”。"
                ) from exc
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f"{endpoint} 返回的不是有效 Chrome 调试接口数据") from exc
            validate_local_endpoint(endpoint)
        self._playwright = await async_playwright().start()
        try:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=60000)
            except Exception as exc:
                if "Browser.setDownloadBehavior" not in str(exc) or "Browser context management is not supported" not in str(exc):
                    raise
                # Chrome may keep the profile process alive after its last tab is
                # closed. Playwright's download setup fails until a tab exists.
                temporary = await self._playwright.chromium.connect_over_cdp(
                    endpoint, timeout=60000, no_defaults=True,
                )
                try:
                    if temporary.contexts and not any(context.pages for context in temporary.contexts):
                        await temporary.contexts[0].new_page()
                        logger.info("旧专用浏览器没有标签页，已补建空白页以恢复连接")
                finally:
                    with contextlib.suppress(Exception):
                        await temporary.close()
                try:
                    self._browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=60000)
                except Exception:
                    logger.warning("Chrome 仍不支持接管下载设置，改用浏览器原有下载设置接入")
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        endpoint, timeout=60000, no_defaults=True,
                    )
            if not self._browser.contexts:
                raise RuntimeError("浏览器没有可复用的 Context；不会自动创建隔离 Context")
            await self.list_pages()
            # 接管后等待用户选择；只有一个业务页面时可安全绑定它。
            from urllib.parse import urlsplit
            matches = [p for p in self._page_ids if urlsplit(p.url).netloc == urlsplit(self._config["target_origin"]).netloc]
            if len(matches) == 1:
                self._bind_page(matches[0])
            elif not self._pages:
                self._bind_failed_page_if_alone()
        except BaseException:
            await self._detach()
            raise

    async def connect(self, endpoint: str) -> None:
        async with self._lifecycle_lock:
            self._require_idle()
            if self.is_running:
                raise RuntimeError("请先断开当前浏览器")
            await self._detach()
            self._chrome_proc = None  # 手动接入的实例不归本次连接拥有
            self._close_chrome_log()
            await self._connect(endpoint)

    async def connect_authorized(self, profile_dir: str) -> None:
        await self.connect(read_authorized_endpoint(Path(profile_dir).expanduser()))

    async def ensure_connected(self) -> None:
        if self.is_running:
            return
        if self._config["mode"] == "cdp":
            await self.connect(self._config["endpoint"])
        elif self._config["mode"] == "authorized":
            await self.connect_authorized(self._config["profile_dir"])
        else:
            await self.start(startup_url(
                self._config["target_origin"], str(self._config.get("entry_url") or "")
            ))

    async def start(self, url: str = "https://pm.gd.csg.cn/portal/#/home") -> None:
        async with self._lifecycle_lock:
            self._require_idle()
            if self.is_running:
                return
            await self._detach()
            chrome_path = _get_chrome_path()
            if not chrome_path:
                raise RuntimeError("找不到系统 Chrome 浏览器")
            profile = Path(self._config["profile_dir"]).expanduser().resolve()
            profile.mkdir(parents=True, exist_ok=True)
            self._profile_dir = profile
            # 上次软件退出后 Chrome 可能仍在。先尝试其已登记端点，不抢占 Profile。
            endpoint_file = profile / "spider-cdp-endpoint"
            if endpoint_file.exists():
                saved_endpoint = ""
                try:
                    saved_endpoint = endpoint_file.read_text().strip()
                    if not saved_endpoint.startswith("ws://"):
                        raise ValueError("旧端点记录需要重新连接")
                    await self._connect(saved_endpoint)
                    self._configure_managed_downloads(profile)
                    blank_pages = [page for page in self._page_ids if page.url == "about:blank"]
                    if not self._page_ids or len(self._page_ids) == len(blank_pages) == 1:
                        page = blank_pages[0] if blank_pages else await self._browser.contexts[0].new_page()
                        self._context = page.context
                        self._pages = [page]
                        self._current_page_idx = 0
                        await self.list_pages()
                        with contextlib.suppress(Exception):
                            await page.goto(url, wait_until="commit", timeout=10000)
                    return
                except Exception as exc:
                    # A live Chrome owns this profile. Starting a second one only
                    # hands off to it and obscures the real connection error.
                    import httpx
                    from urllib.parse import urlsplit
                    try:
                        parts = urlsplit(saved_endpoint)
                        if parts.scheme == "ws" and parts.hostname in ("127.0.0.1", "localhost") and parts.port:
                            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                                probe = await client.get(f"http://127.0.0.1:{parts.port}/json/version")
                                if probe.status_code == 200 and probe.json().get("webSocketDebuggerUrl") == saved_endpoint:
                                    raise RuntimeError(f"已有专用 Chrome 正在运行，但接入失败：{exc}") from exc
                    except (httpx.HTTPError, ValueError):
                        pass
            port = _allocate_cdp_port()
            endpoint = f"http://127.0.0.1:{port}"
            self._chrome_log = profile / "chrome.log"
            self._chrome_log_file = self._chrome_log.open("wb")
            self._chrome_proc = subprocess.Popen(
                _build_launch_args(chrome_path, profile, headless=_headless_requested(), cdp_port=port) + [url],
                stdout=subprocess.DEVNULL, stderr=self._chrome_log_file,
            )
            try:
                import httpx
                async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                    for _ in range(60):
                        code = self._chrome_proc.poll()
                        if code is not None:
                            raise RuntimeError(self._describe_exit("Chrome 启动失败（请检查 Profile 是否已被占用）", code, self._chrome_log_tail()))
                        try:
                            response = await client.get(endpoint + "/json/version")
                            response.raise_for_status()
                            break
                        except httpx.HTTPError:
                            await asyncio.sleep(0.25)
                    else:
                        raise RuntimeError("Chrome 调试端口启动超时")
                await self._connect(endpoint)
                self._configure_managed_downloads(profile)
                endpoint_file.write_text(response.json()["webSocketDebuggerUrl"])
                if not self._pages:
                    pages = [p for p in self._page_ids if p.url == url or p.url == "about:blank"]
                    if len(pages) == 1:
                        self._context = pages[0].context
                        self._pages = [pages[0]]
            except BaseException:
                await self._detach()
                await self._terminate_owned()
                raise

    def set_download_dir(self, directory: str) -> None:
        """同步应用设置的下载目录，已开始的下载继续保存到原路径。"""
        preferred = Path(directory).expanduser().resolve() if directory.strip() else None
        if preferred == self._download_root:
            return
        if self._managed_download_dir is not None:
            destination = preferred or self._profile_dir / "downloads"
            destination.mkdir(parents=True, exist_ok=True)
            self._managed_download_dir = destination
        self._download_root = preferred

    def _configure_managed_downloads(self, profile: Path) -> None:
        """Save a permanent copy of each managed Chrome download."""
        download_dir = self._download_root or profile / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)
        self._managed_download_dir = download_dir
        for context in self._browser.contexts:
            for page in context.pages:
                self._track_managed_downloads(page)
            context.on("page", self._track_managed_downloads)

    def _track_managed_downloads(self, page: Any) -> None:
        def received(download: Any) -> None:
            if self._active_runner is not None:
                return
            task = asyncio.create_task(self._save_managed_download(download))
            self._managed_download_tasks.add(task)
            task.add_done_callback(self._managed_download_tasks.discard)
        page.on("download", received)

    async def _save_managed_download(self, download: Any) -> None:
        if self._managed_download_dir is None:
            return
        # WorkflowRunner saves automated exports into the task's configured
        # location. A second managed-browser copy would create stray files in
        # the global root (and suffix duplicates with random IDs).
        if self._active_runner is not None:
            return
        name = Path(download.suggested_filename).name or "download"
        target = self._managed_download_dir / name
        if target.exists():
            import uuid
            target = target.with_name(f"{target.stem}-{uuid.uuid4().hex[:8]}{target.suffix}")
        try:
            await download.save_as(str(target))
            logger.info("专用浏览器下载已保存: {}（原始文件名: {}）", target, name)
        except Exception:
            logger.exception("专用浏览器下载保存失败: {}", target)

    async def _detach(self) -> None:
        if self._managed_download_tasks:
            done, pending = await asyncio.wait(self._managed_download_tasks, timeout=30)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._managed_download_tasks.clear()
        probes = list(self._visibility_probes.values())
        self._visibility_probes = {}
        for session, _context_id in probes:
            with contextlib.suppress(Exception):
                await session.detach()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._playwright = self._browser = self._context = None
        self._pages = []
        self._page_ids = {}
        self._current_page_idx = 0
        self._managed_download_dir = None

    async def detach(self) -> None:
        async with self._lifecycle_lock:
            self._require_idle()
            await self._detach()

    async def _terminate_owned(self) -> None:
        if self._chrome_proc is not None:
            proc, self._chrome_proc = self._chrome_proc, None
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, timeout=5)
        self._close_chrome_log()

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._require_idle()
            await self._detach()
            await self._terminate_owned()
        # 持久 Profile 不随 close 删除。

    def _chrome_log_tail(self, limit: int = 400) -> str:
        if self._chrome_log is None:
            return ""
        self._close_chrome_log()
        try:
            return self._chrome_log.read_text(encoding="utf-8", errors="replace").strip()[-limit:]
        except OSError:
            return ""

    def _close_chrome_log(self) -> None:
        if self._chrome_log_file is not None:
            self._chrome_log_file.close()
            self._chrome_log_file = None

    @staticmethod
    def _describe_exit(last_error: str, exit_code: int | None, detail: str) -> str:
        message = last_error
        if exit_code is not None:
            message = f"{last_error} (0x{exit_code & 0xFFFFFFFF:08X})"
            if exit_code & 0xFFFFFFFF == 0x80000003:
                message += "；0x80000003 表示断点异常，需结合 Chrome 输出判断崩溃原因"
                hint = _chrome_exit_hint()
                if hint:
                    message += f"，{hint}"
        return f"{message}；Chrome 输出: {detail}" if detail else message

    def inject_into(self, runner: Any) -> None:
        if not self.is_running:
            raise RuntimeError("共享浏览器未启动，无法注入")
        page = self.page
        runner.browser, runner.context = self._browser, page.context
        runner.chrome_proc = None
        runner.pages = [page]
        runner._current_page_idx = 0
        runner._shared_session = self
        runner._protected_pages = set(page.context.pages)

    def sync_from(self, runner: Any) -> None:
        # 保留用户选择的根页面，不把工作流中途的新标签设成全局绑定页。
        pass

    async def check_auth(
        self, page: Any = None, *, require_storage_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """业务探测器：站点、成功元素、可选身份必须同时匹配。

        require_storage_keys 仅为兼容旧调用保留，不再读取 localStorage。
        """
        from urllib.parse import urlsplit
        try:
            try:
                page = page or self.page
            except RuntimeError as exc:
                return {"state": "unauthenticated", "reason": str(exc)}
            if is_browser_error_page(str(getattr(page, "url", "") or "")):
                return {"state": "unauthenticated", "reason": PAGE_LOAD_FAILED_MESSAGE}
            if urlsplit(page.url).netloc != urlsplit(self._config["target_origin"]).netloc:
                return {"state": "unauthenticated", "reason": "当前页面不是配置的业务站点"}
            if await page.locator(".ant-modal-confirm").first.is_visible():
                return {"state": "challenge", "reason": "业务页面存在待处理弹窗"}
            if is_public_portal_url(page.url):
                return {"state": "unauthenticated", "reason":
                        f"当前是门户公开首页或登录页，尚未进入工作台（检查页：{self._page_label(page)}）"}
            selector = self._config["success_selector"]
            matches = page.locator(selector) if selector else None
            visible = False
            if matches is not None:
                for index in range(await matches.count()):
                    if await matches.nth(index).is_visible():
                        visible = True
                        break
            if not visible:
                return {"state": "unauthenticated", "reason":
                        f"尚未出现配置的登录成功标志（检查页：{self._page_label(page)}；选择器：{selector}）"}
            identity = ""
            if self._config["expected_identity"]:
                identity = (await page.locator(self._config["identity_selector"]).first.inner_text(timeout=3000)).strip()
                if identity != self._config["expected_identity"]:
                    return {"state": "wrong_account", "reason": "当前业务身份与配置不一致"}
            return {"state": "authenticated", "identity": identity}
        except Exception as exc:
            return {"state": "unknown", "reason": f"登录状态无法确认: {exc}"}

    async def require_auth(
        self, *, allow_auto: bool = False, page: Any = None,
        require_storage_keys: list[str] | None = None,
    ) -> bool:
        while True:
            auth = await self.check_auth(page, require_storage_keys=require_storage_keys)
            if auth["state"] == "authenticated":
                return True
            # 未知状态和身份不符不能靠“继续”放行，也不能当成未登录去自动登录。
            if allow_auto and self._config["auth_policy"] == "reuse_or_auto" and auth["state"] == "unauthenticated":
                return False
            self.emit({"event": "error_pause", "error": auth["reason"], "message": auth["reason"] + "；请在浏览器完成登录后重新检查"})
            if not await self.wait_continue():
                raise RuntimeError("用户取消登录检查")

    @contextlib.asynccontextmanager
    async def execution(self, name: str, on_event=None):
        import uuid
        # 启动阶段与持有租约阶段分开；获取租约前再次检查以避免并发抢占。
        if self._execution_lock.locked():
            raise RuntimeError("浏览器正被其他任务占用（包括已暂停任务）")
        await self.ensure_connected()
        async with self._lifecycle_lock:
            if self._execution_lock.locked():
                raise RuntimeError("浏览器正被其他任务占用")
            if not self.is_running:
                raise RuntimeError("浏览器连接已断开，请重新连接")
            await self._execution_lock.acquire()
        self._cancel_requested = False
        self._resume_event.clear()
        self._run = {"id": uuid.uuid4().hex, "name": name, "state": "running", "reason": "", "step": 0, "pause_version": 0}
        self._event_sink = on_event
        self._save()
        try:
            await self.list_pages()
            # 必须已经选择。错误页在这里抛出加载失败说明，禁止新建隔离 Context 兜底。
            _bound = self.page
            del _bound
            self.emit({"event": "run_started", "run_id": self._run["id"]})
            self._warn_stale_tabs()
            yield self.emit
            if self._run["state"] == "running":
                self._run["state"] = "succeeded"
        except asyncio.CancelledError:
            self._run.update(state="cancelled", reason="执行连接已中止")
            raise
        except Exception as exc:
            self._run.update(state="failed", reason=str(exc))
            raise
        finally:
            if self._cancel_requested:
                self._run.update(state="cancelled", reason="用户取消")
            self._active_runner = None
            self._event_sink = None
            self._save()
            self._execution_lock.release()

    def _warn_stale_tabs(self) -> None:
        """执行前提示同域多标签页：旧文档的请求签名可能已失效。

        现场证据：pm.gd.csg.cn 的请求签名与文档实例绑定，同一浏览器里多个业务
        标签页会共用一套防护 cookie/密钥；旧文档的请求容易被网关直接 400。
        这里只提示，不擅自关闭用户页面。
        """
        try:
            from urllib.parse import urlsplit

            target = urlsplit(self._config["target_origin"]).netloc
            bound = self.page
            # 只看绑定页所在 Context。另一个 Context 的同域页面不是这份共享会话的冲突页。
            others = [
                page for page in bound.context.pages
                if page is not bound and not page.is_closed()
                and urlsplit(page.url).netloc == target
            ]
            if not others:
                return
            urls = [page.url[:120] for page in others[:5]]
            self.emit({
                "event": "stale_tabs_warning",
                "count": len(others),
                "pages": urls,
                "message": (
                    f"检测到 {len(others)} 个另外的业务标签页，旧文档的请求签名可能已失效；"
                    "建议执行前只保留当前绑定页"
                ),
            })
            logger.warning("  检测到 {} 个另外的业务站点标签页：{}（旧文档签名可能失效）",
                           len(others), "；".join(urls))
        except Exception as exc:
            logger.debug("  多标签检查失败: {}", exc)

    def emit(self, event: dict[str, Any]) -> None:
        if self._run:
            kind = event.get("event")
            if kind in ("error_pause", "waiting_input"):
                if self._run["state"] != "paused":
                    self._resume_event.clear()
                    self._run["pause_version"] += 1
                self._run.update(state="paused", reason=event.get("error") or event.get("message") or "等待人工操作")
            elif kind in ("step_error", "workflow_error", "task_error", "aborted"):
                self._run.update(state="failed", reason=event.get("error") or event.get("message") or "执行失败")
            elif kind == "step_start":
                self._run["step"] = event.get("step", 0)
            event = {**event, "run_id": self._run["id"], "pause_version": self._run["pause_version"]}
            self._save()
        sink = getattr(self, "_event_sink", None)
        if sink:
            sink(event)

    async def wait_continue(self) -> bool:
        if self._cancel_requested:
            return False
        if self._run and self._run["state"] != "paused":
            self.emit({"event": "waiting_input", "message": "请完成手动操作后继续"})
        try:
            await asyncio.wait_for(self._resume_event.wait(), timeout=1800)
        except TimeoutError as exc:
            raise RuntimeError("等待人工处理超过 30 分钟，请检查现场后重新执行") from exc
        self._resume_event.clear()
        if self._cancel_requested:
            return False
        if self._run:
            self._run.update(state="running", reason="")
        self.emit({"event": "resumed"})
        return True

    def resume(self, run_id: str, pause_version: int) -> None:
        if not self._execution_lock.locked() or not self._run or self._run["id"] != run_id:
            raise ValueError("运行实例已结束或发生变化")
        if self._run["state"] != "paused" or self._run["pause_version"] != pause_version:
            raise ValueError("暂停状态已变化，请刷新后再操作")
        self._resume_event.set()

    def cancel(self, run_id: str) -> None:
        if not self._execution_lock.locked() or not self._run or self._run["id"] != run_id:
            raise ValueError("运行实例已结束或发生变化")
        self._cancel_requested = True
        if self._active_runner is not None:
            self._active_runner._stopped = True
        self._resume_event.set()


# 模块级单例，供 API 端点共享
_shared_browser: SharedBrowser | None = None


def get_shared_browser() -> SharedBrowser:
    """获取或创建共享浏览器单例。"""
    global _shared_browser
    if _shared_browser is None:
        import os
        _shared_browser = SharedBrowser(Path(os.environ.get("SPIDER_BROWSER_CONFIG", str(default_profile_dir().parents[1] / "browser-session.json"))))
    return _shared_browser
