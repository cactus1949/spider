"""集成：mode_d_offline 离线链路端到端冒烟（无网络依赖）。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from spider_gd.app import assemble, run_offline_pipeline
from spider_gd.config.settings import load_config


@pytest.mark.integration
async def test_offline_pipeline_end_to_end(tmp_path: Path):
    settings = load_config("configs/mode_d_offline.yaml")
    # 重定向到临时目录，避免污染仓库
    settings.storage.data_dir = str(tmp_path / "data")
    settings.storage.sqlite_path = str(tmp_path / "data" / "spider.db")
    settings.log.persist_to_file = False

    ctx = assemble(settings)
    try:
        result = await run_offline_pipeline(
            ctx,
            task_name="日清算采集",
            indicator="日清算概览",
            target_date="2026-06-18",
            data=b"PK\x03\x04 fake-excel-bytes",
        )
    finally:
        ctx.state_store.close()

    # SQLite 状态
    assert result["summary"]["success"] == 1
    # 台账与数据文件存在
    assert Path(result["ledger"]).exists()
    task_dir = Path(result["task_dir"])
    assert (task_dir / "日清算概览" / "2026-06-18.xlsx").exists()
    # zip 归档包含台账与数据
    zip_path = Path(result["zip"])
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("台账.xlsx" in n for n in names)
    assert any("2026-06-18.xlsx" in n for n in names)


@pytest.mark.integration
def test_assemble_offline_uses_null_alert_and_no_agent():
    settings = load_config("configs/mode_d_offline.yaml")
    settings.log.persist_to_file = False
    settings.storage.sqlite_path = ":memory:"
    ctx = assemble(settings)
    try:
        from spider_gd.gateway.alert.alert_gateway import NullAlertGateway

        assert isinstance(ctx.alert, NullAlertGateway)
        assert ctx.agent is None
    finally:
        ctx.state_store.close()
