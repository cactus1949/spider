"""共享 pytest fixtures。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """测试 fixtures 根目录。"""
    return FIXTURES_DIR


@pytest.fixture(autouse=True)
def _quiet_loguru() -> Iterator[None]:
    """测试期间将 loguru 写入丢弃 sink，避免污染 CI 日志。"""
    logger.remove()
    logger.add(lambda _msg: None, level="DEBUG")
    yield
    logger.remove()  # 移除全部，兼容测试内部自行 setup_logging 的场景
