"""浏览器侦察：Playwright HAR 全量录制 + 逐页 HTML/截图快照 + 认证信息提取。

playwright 延迟导入，便于在无 playwright 的环境导入本模块（如纯分析/打包）。

设计要点：
- HAR 全量录制（``record_har_content="attach"``），不做正向 URL 过滤——
  Playwright 的 ``record_har_url_filter`` 只支持正向匹配，无法精确排除静态资源
  且不漏 API/HTML/导出请求；保留全量录制后由 ``har_analyzer`` 分类时忽略静态资源。
  HAR 是 JSON 文本，压缩比高，30 分钟现场录制约 50–150MB，zip 后通常 < 30MB，
  现场磁盘可承受。
- 每次主 frame 导航完成（``framenavigated``）后，自动写入 ``pages/{NN}_{slug}.html``
  与同名 ``.png`` 截图，便于后续离线分析页面结构。
- ``download`` 事件触发时，下载文件保存到 ``downloaded_files/`` 目录。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from loguru import logger

from recon_tool.stealth import connect_or_launch, STEALTH_ARGS, STEALTH_INIT_SCRIPT, STEALTH_USER_AGENT


def _setup_browser_path() -> None:
    """自动设置 PLAYWRIGHT_BROWSERS_PATH，优先使用 exe 旁的 playwright-browsers 目录。"""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return  # 已由外部设置，不覆盖

    # PyInstaller 打包后 sys._MEIPASS 指向 _internal，exe 在其上一级
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
    else:
        exe_dir = Path(__file__).resolve().parent.parent  # tools/

    browsers_dir = exe_dir / "playwright-browsers"
    if browsers_dir.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
        logger.debug("自动设置 PLAYWRIGHT_BROWSERS_PATH={}", browsers_dir)


def sanitize_filename(url: str) -> str:
    """把 URL 转为安全的文件名片段。"""
    slug = re.sub(r"[^0-9A-Za-z._-]+", "_", url)
    return slug[:80].strip("_") or "page"


async def run_browser_recon(
    out_dir: Path | str,
    *,
    start_url: str = "https://pm.gd.csg.cn/",
    headless: bool = False,
) -> dict[str, str]:
    """启动可见浏览器录制 HAR，等待用户手动登录与浏览。

    浏览过程中：
    - 主 frame 每次导航完成，自动保存 ``pages/{NN}_{slug}.html`` + ``.png``。
    - 触发下载时，文件落地到 ``downloaded_files/``。

    返回产出文件路径字典。需要安装 playwright + chromium。
    """
    _setup_browser_path()
    from playwright.async_api import async_playwright

    out = Path(out_dir)
    pages_dir = out / "pages"
    downloads_dir = out / "downloaded_files"
    pages_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    har_path = out / "session.har"

    chrome_proc = None
    async with async_playwright() as p:
        browser, chrome_proc, is_real_chrome = await connect_or_launch(p, headless=headless)
        context = await browser.new_context(
            record_har_path=str(har_path),
            record_har_content="attach",
            record_har_mode="full",
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            # 真实 Chrome 不覆盖 UA，让 sec-ch-ua 与 User-Agent 版本号一致
            **({} if is_real_chrome else {"user_agent": STEALTH_USER_AGENT}),
        )
        # 真实 Chrome 不需要 init script——navigator 属性本身就是正确的
        # init script 的 Object.defineProperty 调用反而可能被 $_ts 检测到
        if not is_real_chrome:
            await context.add_init_script(STEALTH_INIT_SCRIPT)
        page = await context.new_page()

        # 页面快照计数与去重
        page_counter = {"n": 0}
        seen_urls: set[str] = set()

        async def _snapshot_page(target_page, url: str) -> None:
            page_counter["n"] += 1
            idx = f"{page_counter['n']:02d}"
            safe = sanitize_filename(url)
            html_file = pages_dir / f"{idx}_{safe}.html"
            png_file = pages_dir / f"{idx}_{safe}.png"
            try:
                # SPA 页面：先用 networkidle 等异步数据加载完，超时则 fallback 到延迟截图
                try:
                    await target_page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    # networkidle 超时（可能有 websocket 长连接），等 2 秒保底
                    await asyncio.sleep(2)
                html = await target_page.content()
                html_file.write_text(html, encoding="utf-8")
                await target_page.screenshot(path=str(png_file), full_page=True)
                logger.info("已快照 #{}: {}", idx, url)
            except Exception as exc:
                logger.warning("页面快照失败 #{} {}: {}", idx, url, exc)

        async def _save_download(download) -> None:
            try:
                suggested = download.suggested_filename or "download.bin"
                target = downloads_dir / suggested
                await download.save_as(str(target))
                logger.info("下载已保存：{}", target)
            except Exception as exc:
                logger.warning("下载保存失败：{}", exc)

        def _on_download(download) -> None:
            asyncio.create_task(_save_download(download))

        def _make_nav_handler(target_page):
            """为指定 page 创建 framenavigated 处理器"""
            def _on_framenavigated(frame) -> None:
                if frame != target_page.main_frame:
                    return
                url = frame.url or ""
                if not url or url == "about:blank":
                    return
                if url in seen_urls:
                    return
                seen_urls.add(url)
                asyncio.create_task(_snapshot_page(target_page, url))
            return _on_framenavigated

        def _on_new_page(new_page) -> None:
            """监听新标签页（点击链接 target=_blank 或 window.open）"""
            logger.info("检测到新标签页：{}", new_page.url or "(加载中)")
            new_page.on("framenavigated", _make_nav_handler(new_page))
            new_page.on("download", _on_download)

        context.on("page", _on_new_page)

        # 初始页面
        page.on("framenavigated", _make_nav_handler(page))
        page.on("download", _on_download)

        await page.goto(start_url)

        logger.info("浏览器已就绪，请手动登录并浏览各数据页面，完成后关闭浏览器窗口。")
        # 等待浏览器关闭（CDP模式）或初始页面关闭（launch模式）
        try:
            await browser.wait_for_event("disconnected", timeout=0)
        except Exception:
            await page.wait_for_event("close", timeout=0)

        cookies = await context.cookies()
        (out / "cookies.json").write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 保存 storage_state（Cookie + localStorage），供 HAR 回放时恢复本地认证状态
        storage_state_path = out / "storage_state.json"
        await context.storage_state(path=str(storage_state_path))
        logger.info("已保存 storage_state：{}", storage_state_path)
        try:
            await context.close()
        except Exception as exc:
            logger.warning("关闭 context 失败（HAR 可能未完全写入）: {}", exc)
        try:
            await browser.close()
        except Exception as exc:
            logger.warning("关闭 browser 失败: {}", exc)

    # CDP 模式下需要手动关闭 Chrome 进程
    if chrome_proc:
        chrome_proc.terminate()
        logger.info("已关闭 Chrome 进程")

    logger.success("浏览器侦察完成，HAR：{}", har_path)
    return {
        "har": str(har_path),
        "cookies": str(out / "cookies.json"),
        "storage_state": str(storage_state_path),
        "pages_dir": str(pages_dir),
        "downloads_dir": str(downloads_dir),
    }
