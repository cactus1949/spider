"""测试本地落地：目录分层、台账生成、zip 压缩。"""

from __future__ import annotations

import zipfile
import json
from pathlib import Path

import pytest

from spider_gd.core.pipeline.local_landing import LocalLanding
from spider_gd.interfaces.storage import TaskItemRecord


@pytest.fixture
def landing(tmp_path: Path) -> LocalLanding:
    return LocalLanding(tmp_path / "data")


@pytest.mark.unit
def test_create_task_dir_layers_by_date(landing: LocalLanding):
    task_dir = Path(landing.create_task_dir("日清算采集", "20260115_083000"))
    assert task_dir.exists()
    assert "2026-01" in task_dir.parts
    assert "2026-01-15" in task_dir.parts
    assert task_dir.name == "日清算采集_20260115_083000"


@pytest.mark.unit
def test_save_data_file_writes_to_indicator_subdir(landing: LocalLanding):
    task_dir = landing.create_task_dir("日清算采集", "20260115_083000")
    fp = Path(landing.save_data_file(task_dir, "日前出清", "2026-01-14", b"PKxlsx"))
    assert fp.exists()
    assert fp.parent.name == "日前出清"
    assert fp.read_bytes() == b"PKxlsx"


@pytest.mark.unit
def test_save_data_file_reuses_same_hash_and_archives_changed_version(landing: LocalLanding):
    task_dir = landing.create_task_dir("日清算采集", "20260115_083000")
    fp = Path(landing.save_data_file(task_dir, "日前出清", "2026-01-14", b"same"))

    landing.save_data_file(task_dir, "日前出清", "2026-01-14", b"same")

    assert fp.read_bytes() == b"same"
    assert not (fp.parent / "_versions").exists()

    landing.save_data_file(task_dir, "日前出清", "2026-01-14", b"changed")

    assert fp.read_bytes() == b"changed"
    versions = list((fp.parent / "_versions").glob("2026-01-14_*.xlsx"))
    assert len(versions) == 1
    assert versions[0].read_bytes() == b"same"
    manifest = json.loads((fp.parent / ".file_versions.json").read_text(encoding="utf-8"))
    record = manifest["files"]["2026-01-14.xlsx"]
    assert len(record["current_sha256"]) == 64
    assert len(record["versions"]) == 1
    assert record["versions"][0]["path"].startswith("_versions/")


@pytest.mark.unit
def test_generate_ledger_creates_xlsx_with_status(landing: LocalLanding):
    task_dir = landing.create_task_dir("日清算采集", "20260115_083000")
    items = [
        TaskItemRecord("T1", "日前出清", "2026-01-14", "success", "/p/a.xlsx", 1024),
        TaskItemRecord("T1", "实时出清", "2026-01-14", "failed", None, 0, "无数据"),
    ]
    ledger = Path(landing.generate_ledger(task_dir, items))
    assert ledger.exists()
    assert ledger.name == "台账.xlsx"

    from openpyxl import load_workbook

    wb = load_workbook(ledger)
    assert "文件清单" in wb.sheetnames
    assert "执行摘要" in wb.sheetnames
    ws = wb["执行摘要"]
    assert ws.cell(row=2, column=2).value == 1  # 成功数


@pytest.mark.unit
def test_compress_task_creates_zip(landing: LocalLanding):
    task_dir = landing.create_task_dir("日清算采集", "20260115_083000")
    landing.save_data_file(task_dir, "日前出清", "2026-01-14", b"data")
    zip_path = Path(landing.compress_task(task_dir))
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        assert any("2026-01-14.xlsx" in n for n in zf.namelist())
