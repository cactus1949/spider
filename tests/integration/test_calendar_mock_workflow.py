"""交易日历周明细工作流在 mock 页上逐周拼接。"""

from __future__ import annotations

import csv
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MOCK_DIR = _REPO_ROOT / "mock_site"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def calendar_mock_server() -> str:
    import uvicorn

    if str(_MOCK_DIR) not in sys.path:
        sys.path.insert(0, str(_MOCK_DIR))
    import main as mock_main

    port = _free_port()
    config = uvicorn.Config(mock_main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("mock_site 启动超时")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _week_workflow(base_url: str) -> dict[str, Any]:
    path = _REPO_ROOT / "workflows" / "交易日历-周明细.yml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["query_month"] = "2026-10"
    cfg["target_date"] = "2026-10-01"

    def rewrite(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("https://pm.gd.csg.cn", base_url)
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            rewritten = {key: rewrite(item) for key, item in value.items()}
            if rewritten.get("action") == "wait" and "ms" in rewritten:
                rewritten["ms"] = min(int(rewritten["ms"]), 20)
            if "timeout" in rewritten:
                rewritten["timeout"] = min(int(rewritten["timeout"]), 8000)
            return rewritten
        return value

    return rewrite(cfg)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_week_workflow_stitches_captured_and_filled_weeks(
    calendar_mock_server: str, tmp_path: Path,
):
    """按月逐周跑通：现场周明细、月标题补出的周、跨月周都进各自的月文件。"""
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright
    from workflow_runner import WorkflowRunner

    cfg = _week_workflow(calendar_mock_server)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        await page.goto(f"{calendar_mock_server}/portal/#/powerEnterprise")
        runner = WorkflowRunner(cfg, download_dir=str(tmp_path), headless=True)
        context.on("page", runner._on_new_page)
        try:
            await runner.run(lambda _event: None, None, browser=browser, context=context, pages=[page])
        finally:
            await context.close()
            await browser.close()

    october = _rows(tmp_path / "交易日历明细2026-10.csv")
    by_date: dict[str, list[dict[str, str]]] = {}
    for row in october:
        by_date.setdefault(row["日期"], []).append(row)
    assert "2026-10-30" not in by_date
    assert "2026-10-31" not in by_date
    for day in ("2026-10-01", "2026-10-08", "2026-10-15", "2026-10-22", "2026-10-26"):
        assert day in by_date

    first = by_date["2026-10-01"][0]
    assert first["交易名称"] == "2026年10月周双边协商交易(2026-10-01)"
    assert first["申报时间"] == "10-01 09:00 ~ 10-01 10:30"
    assert first["时段"] == "上午"
    assert first["类型"] == "正式"

    captured = by_date["2026-10-26"]
    assert captured[0]["交易名称"].startswith("2026年10月多日分时集中竞争交易")
    assert captured[1]["申报时间"] == "10-26 14:00 ~ 10-26 15:30"
    assert captured[1]["交易时间范围"] == "2026-10-28 ~ 2026-10-31"
    assert captured[1]["时段"] == "下午"
    change = next(row for row in october if row["交易名称"] == "2026年10月交易变更(2026-10-14)")
    assert change["类型"] == ""

    november = _rows(tmp_path / "交易日历明细2026-11.csv")
    assert any(row["日期"] == "2026-11-01" and "周双边协商交易" in row["交易名称"] for row in november)
    assert all(row["日期"].startswith("2026-11") for row in november)
    assert all(not row["日期"].startswith("2026-11") for row in october)
