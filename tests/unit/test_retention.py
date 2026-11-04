"""测试保留期清理。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from spider_gd.infrastructure.storage.retention import cleanup_old_data


def _make_day(root: Path, month: str, day: str) -> Path:
    d = root / month / day
    d.mkdir(parents=True, exist_ok=True)
    (d / "f.txt").write_text("x")
    return d


@pytest.mark.unit
def test_cleanup_removes_old_keeps_recent(tmp_path: Path):
    old = _make_day(tmp_path, "2026-01", "2026-01-01")
    recent = _make_day(tmp_path, "2026-06", "2026-06-20")
    now = datetime(2026, 6, 23)

    removed = cleanup_old_data(tmp_path, retention_days=30, now=now)

    assert str(old) in removed
    assert not old.exists()
    assert recent.exists()


@pytest.mark.unit
def test_cleanup_dry_run_keeps_files(tmp_path: Path):
    old = _make_day(tmp_path, "2026-01", "2026-01-01")
    now = datetime(2026, 6, 23)
    removed = cleanup_old_data(tmp_path, retention_days=30, now=now, dry_run=True)
    assert str(old) in removed
    assert old.exists()  # dry-run 不实际删除


@pytest.mark.unit
def test_cleanup_noop_when_disabled(tmp_path: Path):
    _make_day(tmp_path, "2026-01", "2026-01-01")
    assert cleanup_old_data(tmp_path, retention_days=0) == []
