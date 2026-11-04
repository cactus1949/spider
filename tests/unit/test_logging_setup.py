"""测试日志初始化、按次独立 sink 与脱敏。"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from spider_gd.config.logging import _mask_sensitive, attach_task_sink, setup_logging


@pytest.mark.unit
def test_setup_logging_creates_rolling_file(tmp_path: Path):
    setup_logging(tmp_path, persist_to_file=True)
    logger.bind(task="-").info("hello world")
    logger.complete()
    assert (tmp_path / "app.log").exists()


@pytest.mark.unit
def test_attach_task_sink_writes_isolated_file(tmp_path: Path):
    setup_logging(tmp_path, persist_to_file=False)
    sink_id = attach_task_sink(tmp_path, "日清算采集")
    logger.bind(task="日清算采集").info("task message")
    logger.complete()
    logger.remove(sink_id)

    day_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert day_dirs, "应创建按日期目录"
    log_files = list(day_dirs[0].glob("日清算采集_*.log"))
    assert log_files
    assert "task message" in log_files[0].read_text(encoding="utf-8")


@pytest.mark.unit
def test_mask_sensitive_redacts_token_and_password():
    record = {"message": "password=secret123 token: abc.def HdKGccbL=xyz789&next"}
    _mask_sensitive(record)
    assert "secret123" not in record["message"]
    assert "abc.def" not in record["message"]
    assert "xyz789" not in record["message"]
