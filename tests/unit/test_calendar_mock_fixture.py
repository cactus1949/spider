"""交易日历 mock 使用的现场样本。"""

from __future__ import annotations

import doctest
import importlib.util
from pathlib import Path

import pytest


def _fixture_module():
    path = Path(__file__).resolve().parents[2] / "mock_site" / "calendar_fixture.py"
    spec = importlib.util.spec_from_file_location("spider_gd_calendar_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_calendar_fixture_doctests_and_captured_week():
    module = _fixture_module()
    failed = doctest.testmod(module, verbose=False).failed
    assert failed == 0
    sample = module.calendar_bootstrap()
    second = sample["captured"]["2026-10-26"][1]
    assert second["title"].endswith("（灾备）")
    assert second["apply"] == "10-26 14:00 ~ 10-26 15:30"
    assert second["delivery"] == "2026-10-28 ~ 2026-10-31"
    assert "2026-10-30" in sample["empty"]
    assert "2026-10-31" in sample["empty"]
    assert sample["shorts"]["2026-10-14"][2] == "交易变更"
