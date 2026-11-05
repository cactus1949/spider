"""下载产物列表要排除版本清单，并保留本轮重新确认的真实文件。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from spider_gd.app import (
    _changed_download_files,
    _is_internal_download_artifact,
    _snapshot_download_files,
    _zip_download_files,
)
from spider_gd.core.pipeline.file_versions import save_versioned_bytes
from spider_gd.gateway.data_upload_flow import _changed_files

_PDF_NAME = "广东电力现货市场2026年11月运行日报（11.05）.pdf"


def _rewind_manifest_stamps(directory: Path, stamp: str = "2000-01-01T00:00:00") -> None:
    """把清单里的确认时间拨回过去，避免同一秒内重写看不出变化。

    Args:
        directory: 含 ``.file_versions.json`` 的目录。
        stamp: 写回的 ``updated_at``，格式与清单一致。

    Returns:
        None: 覆盖原清单文件。

    Raises:
        OSError: 清单不存在或无法写入时。
        json.JSONDecodeError: 清单不是 JSON 时。

    Examples:
        >>> import json
        >>> import tempfile
        >>> folder = Path(tempfile.mkdtemp())
        >>> manifest = folder / ".file_versions.json"
        >>> _ = manifest.write_text(
        ...     json.dumps({"files": {"a.pdf": {"updated_at": "2026-11-05T16:16:19"}}}),
        ...     encoding="utf-8",
        ... )
        >>> _rewind_manifest_stamps(folder, "2000-01-01T00:00:00")
        >>> json.loads(manifest.read_text(encoding="utf-8"))["files"]["a.pdf"]["updated_at"]
        '2000-01-01T00:00:00'
    """
    manifest_path = directory / ".file_versions.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in payload.get("files", {}).values():
        if isinstance(item, dict):
            item["updated_at"] = stamp
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.unit
def test_internal_artifact_filter_keeps_dot_user_files(tmp_path: Path) -> None:
    assert _is_internal_download_artifact(tmp_path, tmp_path / ".file_versions.json")
    assert _is_internal_download_artifact(tmp_path, tmp_path / ".file_versions.json.tmp")
    assert _is_internal_download_artifact(tmp_path, tmp_path / "_versions" / "old.pdf")
    assert _is_internal_download_artifact(tmp_path, tmp_path / "day" / "_versions" / "old.pdf")
    assert not _is_internal_download_artifact(tmp_path, tmp_path / ".notes.pdf")
    assert not _is_internal_download_artifact(tmp_path, tmp_path / "_versions")


@pytest.mark.unit
def test_changed_download_files_skip_manifest_and_versions(tmp_path: Path) -> None:
    old_pdf = tmp_path / "old.pdf"
    old_pdf.write_bytes(b"%PDF-old")
    before = _snapshot_download_files(tmp_path)

    new_pdf = tmp_path / _PDF_NAME
    new_pdf.write_bytes(b"%PDF-new")
    (tmp_path / ".notes.pdf").write_bytes(b"dot-user")
    (tmp_path / ".file_versions.json").write_text(
        json.dumps(
            {
                "files": {
                    _PDF_NAME: {
                        "versions": [],
                        "current_sha256": "abc",
                        "updated_at": "2026-11-05T16:16:19",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / ".file_versions.json.tmp").write_text("{}", encoding="utf-8")
    versions = tmp_path / "_versions"
    versions.mkdir()
    (versions / "old_copy.pdf").write_bytes(b"archived")
    (tmp_path / "storage_state.json").write_text("{}", encoding="utf-8")

    changed = _changed_download_files(tmp_path, before)

    assert [path.name for path in changed] == [".notes.pdf", _PDF_NAME]
    assert _changed_files(str(tmp_path), before) == changed


@pytest.mark.unit
def test_reused_pdf_is_uploaded_when_only_manifest_changes(tmp_path: Path) -> None:
    day = tmp_path / "2026-11-05"
    pdf = day / _PDF_NAME
    other = day / "其他日报.pdf"
    save_versioned_bytes(pdf, b"%PDF-same")
    save_versioned_bytes(other, b"%PDF-other")
    _rewind_manifest_stamps(day)
    pdf_mtime = pdf.stat().st_mtime_ns
    before = _snapshot_download_files(tmp_path)
    manifest_key = str((day / ".file_versions.json").relative_to(tmp_path))
    assert before.manifests[manifest_key][_PDF_NAME].startswith("2000-01-01T00:00:00\n")

    save_versioned_bytes(pdf, b"%PDF-same")

    assert pdf.stat().st_mtime_ns == pdf_mtime
    changed = _changed_download_files(tmp_path, before)
    assert changed == [pdf]
    assert _changed_files(str(tmp_path), before) == [pdf]

    zip_path = _zip_download_files(tmp_path, changed, task_name="运行日报", exec_id="exec1")
    assert zip_path is not None
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert names == [f"2026-11-05/{_PDF_NAME}"]
    assert all(".file_versions.json" not in name for name in names)
    assert all("_versions" not in Path(name).parts for name in names)


@pytest.mark.unit
def test_content_change_uploads_pdf_not_archived_copy(tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    save_versioned_bytes(pdf, b"%PDF-v1")
    before = _snapshot_download_files(tmp_path)

    save_versioned_bytes(pdf, b"%PDF-v2")

    changed = _changed_download_files(tmp_path, before)
    assert changed == [pdf]
    assert not any("_versions" in path.parts for path in changed)
    assert not any(path.name == ".file_versions.json" for path in changed)
