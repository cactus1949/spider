"""录制工坊 ZIP 元数据与提交状态测试。"""

import json
import zipfile
from pathlib import Path

from spider_gd.core.workshop_zip import read_workshop_zip_meta, resolve_submission_status


def test_read_workshop_zip_meta_with_export_id(tmp_path: Path):
    raw = {"export_id": "abc-123", "name": "测试", "description": "desc"}
    zpath = tmp_path / "测试.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("测试.raw.json", json.dumps(raw, ensure_ascii=False))

    meta = read_workshop_zip_meta(zpath)
    assert meta["export_id"] == "abc-123"
    assert meta["name"] == "测试"
    assert len(meta["file_hash"]) == 64


def test_read_workshop_zip_meta_legacy_without_export_id(tmp_path: Path):
    zpath = tmp_path / "old.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("old.yml", "name: old\n")

    meta = read_workshop_zip_meta(zpath)
    assert meta["export_id"].startswith("legacy-")
    assert len(meta["file_hash"]) == 64


def test_resolve_submission_status():
    assert resolve_submission_status(None, "hash1") == "pending"
    assert resolve_submission_status({"status": "submitted", "file_hash": "h1"}, "h1") == "submitted"
    assert resolve_submission_status({"status": "submitted", "file_hash": "h1"}, "h2") == "changed"
    assert resolve_submission_status({"status": "failed", "file_hash": "h1"}, "h1") == "failed"
