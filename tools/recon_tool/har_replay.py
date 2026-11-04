"""HAR 回放：基于侦察录制的 HAR 文件离线重现网站环境。

借助 Playwright 的 ``context.route_from_har`` 能力，把所有网络请求拦截并
从 HAR 文件中读取响应，开发者可在不连接专网的情况下进行爬虫逻辑开发与调试。

playwright 延迟导入，便于在无 playwright 的环境导入本模块（如 CLI doctor）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loguru import logger

from recon_tool.stealth import STEALTH_ARGS, STEALTH_INIT_SCRIPT, STEALTH_USER_AGENT


def _infer_start_url(har_path: Path) -> str:
    """从 HAR 文件中提取第一个页面导航的 URL。

    优先选择第一个 ``text/html`` 响应的请求；若不存在则回退到第一个请求。
    """
    with open(har_path, encoding="utf-8") as f:
        har = json.load(f)
    entries = har.get("log", {}).get("entries", [])
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        content_type = (
            entry.get("response", {}).get("content", {}).get("mimeType", "")
        )
        if url and "text/html" in content_type:
            return url
    if entries:
        url = entries[0].get("request", {}).get("url", "")
        if url:
            return url
    raise ValueError(f"无法从 HAR 文件推断起始 URL：{har_path}")


async def run_har_replay(
    har_path: Path,
    *,
    start_url: str | None = None,
    not_found: str = "fallback",
    headless: bool = False,
    storage_state: Path | str | None = None,
) -> None:
    """启动浏览器，用 HAR 文件回放所有网络请求。

    参数：
    - ``har_path``：HAR 文件路径。
    - ``start_url``：起始 URL，默认从 HAR 中第一个页面请求推断。
    - ``not_found``：``"fallback"`` 放行未录制请求；``"abort"`` 严格模式，
      未录制的请求直接中止。
    - ``headless``：默认有头模式，方便开发者观察页面。
    - ``storage_state``：storage_state.json 路径（Cookie + localStorage）；
      为 ``None`` 时自动在 HAR 同目录寻找 ``storage_state.json``。
    """
    from playwright.async_api import async_playwright

    har_path = Path(har_path)
    if not har_path.exists():
        raise FileNotFoundError(f"HAR 文件不存在：{har_path}")

    if start_url is None:
        start_url = _infer_start_url(har_path)
        logger.info("已从 HAR 推断起始 URL：{}", start_url)

    # 自动检测 storage_state
    state_path: Path | None = None
    if storage_state:
        state_path = Path(storage_state)
        if not state_path.exists():
            logger.warning("指定的 storage_state 不存在：{}", state_path)
            state_path = None
    else:
        auto_state = har_path.parent / "storage_state.json"
        if auto_state.exists():
            state_path = auto_state
            logger.info("自动发现 storage_state：{}", state_path)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=headless, channel="chrome", args=STEALTH_ARGS
            )
            logger.info("使用系统 Chrome 启动浏览器")
        except Exception as exc:
            logger.warning("系统 Chrome 未找到，回退到内置 Chromium：{}", exc)
            browser = await p.chromium.launch(headless=headless, args=STEALTH_ARGS)
        context_kwargs: dict = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": STEALTH_USER_AGENT,
        }
        if state_path is not None:
            context_kwargs["storage_state"] = str(state_path)
            logger.info("已加载 storage_state（Cookie + localStorage 已恢复）")
        context = await browser.new_context(**context_kwargs)
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        await context.route_from_har(
            str(har_path),
            not_found=not_found,
            update=False,
        )

        page = await context.new_page()
        await page.goto(start_url)

        logger.info(
            "HAR 回放已启动，所有网络请求将从 HAR 文件读取。关闭浏览器窗口结束。"
        )
        print("HAR 回放已启动，所有网络请求将从 HAR 文件读取。关闭浏览器窗口结束。")

        # 等待用户手动关闭浏览器
        disconnected = asyncio.Event()
        browser.on("disconnected", lambda _b=None: disconnected.set())
        await disconnected.wait()

    logger.success("HAR 回放结束：{}", har_path)
