"""Playwright 反检测配置：stealth 启动参数 + init script + User-Agent。

用于规避目标网站对自动化浏览器的检测（如 navigator.webdriver、UA 特征等）。
不引入第三方依赖，仅通过原生参数和 JS 注入实现。
"""

import os
import random
import subprocess
import sys
import tempfile
import time

from loguru import logger

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process,LocalNetworkAccessChecks",
    "--disable-infobars",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--no-first-run",
    "--no-default-browser-check",
]

STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
// 隐藏 webdriver 标记（用原生 descriptor 避免 getOwnPropertyDescriptor 检测）
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// 伪造 plugins（返回空 PluginArray 比 [1,2,3,4,5] 更不容易被检测）
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [];
        arr.item = () => null;
        arr.namedItem = () => null;
        arr.refresh = () => {};
        Object.setPrototypeOf(arr, PluginArray.prototype);
        return arr;
    }
});

// 伪造 languages
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });

// 伪造 chrome.runtime（一些检测脚本会检查这个）
window.chrome = { runtime: {} };

// 伪造硬件并发数（headless Chromium 常为 1 或与真实环境不一致）
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

// 伪造设备内存
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

// 伪造平台（与 User-Agent Windows NT 10.0 一致）
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

// 伪造 maxTouchPoints（桌面 Chrome 为 0）
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

// 包装 permissions.query
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);

// 伪造 WebGL 渲染器（headless Chrome 使用 SwiftShader，真实 GPU 用不同字符串）
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    // UNMASKED_VENDOR_WEBGL
    if (parameter === 37445) return 'Google Inc. (NVIDIA)';
    // UNMASKED_RENDERER_WEBGL
    if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return getParameter.call(this, parameter);
};

// 伪造屏幕分辨率（headless 默认 800x600，真实桌面远大于此）
Object.defineProperty(screen, 'width', { get: () => 1920 });
Object.defineProperty(screen, 'height', { get: () => 1080 });
Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
"""

CDP_PORT = random.randint(49152, 65535)
CDP_ENDPOINT = f"http://localhost:{CDP_PORT}"


def get_chrome_path() -> str | None:
    """返回系统 Chrome 可执行文件路径，找不到返回 None。"""
    if sys.platform == "win32":
        candidates = [
            os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        import shutil
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            path = shutil.which(name)
            if path:
                return path
        return None
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


async def connect_or_launch(p, *, headless: bool = False, minimized: bool = False):
    """优先通过 CDP 连接系统 Chrome，失败则启动 Chrome 并连接，最后回退到 launch。

    返回 (browser, chrome_proc, is_real_chrome) 元组：
    - browser: Playwright Browser 实例
    - chrome_proc: 启动的 Chrome 子进程（CDP 模式时非 None），或 None（launch 模式）
    - is_real_chrome: True 表示使用的是系统 Chrome（不应用覆盖 UA）
    """
    # 1. 尝试连接已运行的 Chrome（最小化模式需要新窗口，避免复用已打开的可见窗口）
    if not headless and not minimized:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
            logger.info("已通过 CDP 连接到运行中的 Chrome")
            return browser, None, True
        except Exception:
            pass

    # 2. 启动 Chrome with debug port
    chrome_path = get_chrome_path()
    if chrome_path:
        tmp_profile = tempfile.mkdtemp(prefix="chrome-cdp-")
        launch_args = [
            chrome_path,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={tmp_profile}",
            "--disable-blink-features=AutomationControlled",
            # LocalNetworkAccessChecks：Chrome 147+ 默认拦截公网页面对 127.0.0.1
            # 的访问，证书/UKey 控件的本地 WebSocket 会直接连接失败。
            "--disable-features=IsolateOrigins,site-per-process,Translate,LocalNetworkAccessChecks",
            "--disable-infobars",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if headless:
            launch_args.append("--headless=new")
        elif minimized:
            launch_args.append("--window-size=1440,900")
            launch_args.append("--window-position=-32000,-32000")
            launch_args.append("--disable-backgrounding-occluded-windows")
            launch_args.append("--disable-renderer-backgrounding")
            launch_args.append("--disable-background-timer-throttling")
        proc = subprocess.Popen(
            launch_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("已启动 Chrome (PID={}, 调试端口={})", proc.pid, CDP_PORT)

        # 等待 Chrome 启动并连接（最多等 5 秒）
        for _ in range(10):
            time.sleep(0.5)
            try:
                browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
                logger.info("Chrome 启动成功，已通过 CDP 连接")
                return browser, proc, True
            except Exception:
                continue
        logger.warning("Chrome 启动后 CDP 连接超时")
    else:
        logger.warning("未找到系统 Chrome 可执行文件")

    # 3. 回退到 channel="chrome"
    logger.warning("CDP 连接失败，回退到 channel=chrome")
    try:
        args = list(STEALTH_ARGS)
        if minimized and not headless:
            args.extend(["--window-size=1440,900", "--window-position=-32000,-32000"])
        browser = await p.chromium.launch(headless=headless, channel="chrome", args=args)
        return browser, None, True
    except Exception:
        logger.warning("系统 Chrome 未找到，回退到内置 Chromium")
        args = list(STEALTH_ARGS)
        if minimized and not headless:
            args.extend(["--window-size=1440,900", "--window-position=-32000,-32000"])
        browser = await p.chromium.launch(headless=headless, args=args)
        return browser, None, False
