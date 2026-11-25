"""WorkflowRunner generated data outputs use hash-based versioning."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from spider_gd.core.pipeline.file_versions import save_versioned_bytes, save_versioned_file

from workflow_runner import WorkflowRunner


@pytest.mark.unit
def test_generated_xlsx_reuses_same_hash_and_archives_changed_version(tmp_path):
    target = tmp_path / "api.xlsx"

    WorkflowRunner._save_data_as_xlsx([{"price": 1}], target)
    first_bytes = target.read_bytes()
    WorkflowRunner._save_data_as_xlsx([{"price": 1}], target)

    assert target.read_bytes() == first_bytes
    assert not (tmp_path / "_versions").exists()

    WorkflowRunner._save_data_as_xlsx([{"price": 2}], target)

    archived = sorted((tmp_path / "_versions").glob("api_*.xlsx"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == first_bytes

    manifest = json.loads((tmp_path / ".file_versions.json").read_text(encoding="utf-8"))
    assert manifest["files"]["api.xlsx"]["current_sha256"]
    assert manifest["files"]["api.xlsx"]["versions"][0]["path"].startswith("_versions/")


@pytest.mark.unit
def test_generated_xlsx_bytes_ignore_save_clock(tmp_path, monkeypatch):
    # openpyxl 写入秒级 created/modified 与 ZIP 条目时间，跨秒保存会让相同数据产生不同字节，
    # 哈希版本管理据此误判为新版本。固定时钟前进以确定性复现，不依赖真实 sleep。
    import datetime as dt
    import itertools
    import time as time_mod
    import types

    import openpyxl.packaging.core as ox_core
    import openpyxl.writer.excel as ox_excel

    ticks = itertools.count()

    class _FakeDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2021, 1, 1) + dt.timedelta(seconds=next(ticks) * 5)

    fake = types.SimpleNamespace(datetime=_FakeDateTime, timezone=dt.timezone)
    monkeypatch.setattr(ox_core, "datetime", fake)
    monkeypatch.setattr(ox_excel, "datetime", fake)
    zip_ticks = itertools.count()
    monkeypatch.setattr(
        time_mod, "localtime", lambda *a: (2021, 1, 1, 0, 0, next(zip_ticks) * 5, 0, 0, 0)
    )

    target = tmp_path / "api.xlsx"
    WorkflowRunner._save_data_as_xlsx([{"price": 1}], target)
    first_bytes = target.read_bytes()
    WorkflowRunner._save_data_as_xlsx([{"price": 1}], target)

    assert target.read_bytes() == first_bytes
    assert not (tmp_path / "_versions").exists()

    WorkflowRunner._save_data_as_xlsx([{"price": 2}], target)
    assert len(list((tmp_path / "_versions").glob("api_*.xlsx"))) == 1


@pytest.mark.unit
def test_generated_text_reuses_same_hash_and_archives_changed_version(tmp_path):
    target = tmp_path / "extracted.json"

    WorkflowRunner._save_versioned_text(target, '{"a": 1}')
    WorkflowRunner._save_versioned_text(target, '{"a": 1}')

    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    assert not (tmp_path / "_versions").exists()

    WorkflowRunner._save_versioned_text(target, '{"a": 2}')

    archived = sorted((tmp_path / "_versions").glob("extracted_*.json"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == '{"a": 1}'


@pytest.fixture
def beta_home(tmp_path, monkeypatch):
    home = tmp_path / "beta"
    home.mkdir()
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    return home


def test_beta_versioned_file_saves_readable_bytes_and_metadata(beta_home):
    source = beta_home / "download.tmp"
    target = beta_home / "data" / "report.txt"
    source.write_bytes(b"first")
    first = save_versioned_file(source, target)
    assert first.changed and target.read_bytes() == b"first"
    assert not source.exists()
    source.write_bytes(b"first")
    reused = save_versioned_file(source, target)
    assert reused.reused_existing and not source.exists()
    source.write_bytes(b"second")
    changed = save_versioned_file(source, target)
    assert target.read_bytes() == b"second"
    assert changed.archived_path.read_bytes() == b"first"
    manifest = json.loads((target.parent / ".file_versions.json").read_text())
    item = manifest["files"][target.name]
    assert item["current_sha256"] == changed.sha256
    assert item["versions"][0]["path"] == changed.archived_path.relative_to(target.parent).as_posix()


@pytest.mark.parametrize("source_kind", ["external", "symlink", "external_link"])
@pytest.mark.parametrize("same_bytes", [False, True])
def test_beta_versioned_file_rejects_consumed_external_source(
    beta_home, tmp_path, source_kind, same_bytes,
):
    outside = tmp_path / "outside.tmp"
    outside.write_bytes(b"new")
    source = outside
    if source_kind == "symlink":
        source = beta_home / "download.tmp"
        source.symlink_to(outside)
    elif source_kind == "external_link":
        inside = beta_home / "download.tmp"
        inside.write_bytes(b"new")
        source = tmp_path / "external-link.tmp"
        source.symlink_to(inside)
    target = beta_home / "report.txt"
    previous = b"new" if same_bytes else b"old"
    target.write_bytes(previous)
    with pytest.raises(ValueError, match="测试版"):
        save_versioned_file(source, target)
    assert outside.read_bytes() == b"new"
    assert source.exists()
    assert source.is_symlink() == (source_kind in {"symlink", "external_link"})
    assert target.read_bytes() == previous
    assert not (beta_home / ".file_versions.json").exists()


@pytest.mark.parametrize("target_kind", ["external", "directory_link", "leaf_link"])
def test_beta_versioned_file_rejects_external_destination(beta_home, tmp_path, target_kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "report.txt"
    outside_file.write_bytes(b"keep")
    target = outside_file
    if target_kind == "directory_link":
        link = beta_home / "linked"
        link.symlink_to(outside, target_is_directory=True)
        target = link / "report.txt"
    elif target_kind == "leaf_link":
        target = beta_home / "report.txt"
        target.symlink_to(outside_file)
    source = beta_home / "download.tmp"
    source.write_bytes(b"new")
    with pytest.raises(ValueError, match="测试版"):
        save_versioned_file(source, target)
    assert source.read_bytes() == b"new"
    assert outside_file.read_bytes() == b"keep"
    assert sorted(p.name for p in outside.iterdir()) == ["report.txt"]
    if target_kind == "leaf_link":
        assert target.is_symlink()


@pytest.mark.parametrize("metadata", [".file_versions.json", ".file_versions.json.tmp"])
@pytest.mark.parametrize("previous", [None, b"old", b"new"])
@pytest.mark.parametrize("dangling", [False, True])
def test_beta_versioned_file_preflights_metadata_before_consuming_input(
    beta_home, tmp_path, metadata, previous, dangling,
):
    outside = tmp_path / "outside.json"
    if not dangling:
        outside.write_bytes(b'{"sentinel": true}')
    link = beta_home / metadata
    link.symlink_to(outside)
    source = beta_home / "download.tmp"
    source.write_bytes(b"new")
    target = beta_home / "report.txt"
    if previous is not None:
        target.write_bytes(previous)
    with pytest.raises(ValueError, match="测试版"):
        save_versioned_file(source, target)
    assert source.read_bytes() == b"new"
    assert link.is_symlink()
    if previous is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == previous
    assert not (beta_home / "_versions").exists()
    assert outside.exists() == (not dangling)
    if not dangling:
        assert outside.read_bytes() == b'{"sentinel": true}'


@pytest.mark.parametrize("archive_kind", ["directory_link", "selected_leaf_link"])
def test_beta_versioned_file_rejects_archive_escape_before_replacing_output(
    beta_home, tmp_path, monkeypatch, archive_kind,
):
    from spider_gd.core.pipeline import file_versions

    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 11, 12, 1, 2, 3)

    monkeypatch.setattr(file_versions, "datetime", FixedDatetime)
    outside = tmp_path / "outside"
    outside.mkdir()
    version_dir = beta_home / "_versions"
    target = beta_home / "report.txt"
    target.write_bytes(b"old")
    source = beta_home / "download.tmp"
    source.write_bytes(b"new")
    if archive_kind == "directory_link":
        version_dir.symlink_to(outside, target_is_directory=True)
    else:
        version_dir.mkdir()
        # A safe occupied candidate forces selection of the counter-suffixed leaf.
        (version_dir / "report_20261112_010203_cba06b5736fa.txt").write_bytes(b"occupied")
        (version_dir / "report_20261112_010203_cba06b5736fa_1.txt").symlink_to(outside / "archive.txt")
    with pytest.raises(ValueError, match="测试版"):
        save_versioned_file(source, target)
    assert source.read_bytes() == b"new"
    assert target.read_bytes() == b"old"
    assert list(outside.iterdir()) == []
    if archive_kind == "selected_leaf_link":
        assert (version_dir / "report_20261112_010203_cba06b5736fa_1.txt").is_symlink()


def test_beta_versioned_bytes_saves_in_home(beta_home):
    target = beta_home / "new" / "report.txt"
    result = save_versioned_bytes(target, b"readable")
    assert result.path == target
    assert target.read_bytes() == b"readable"
    manifest = json.loads((target.parent / ".file_versions.json").read_text())
    assert manifest["files"]["report.txt"]["current_sha256"] == result.sha256


@pytest.mark.parametrize("target_kind", ["external", "directory_link", "leaf_link", "staging_link"])
def test_beta_versioned_bytes_rejects_escape_before_writing(
    beta_home, tmp_path, monkeypatch, target_kind,
):
    from spider_gd.core.pipeline import file_versions

    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 11, 12, 1, 2, 3)

    monkeypatch.setattr(file_versions, "datetime", FixedDatetime)
    outside = tmp_path / "outside"
    target = beta_home / "report.txt"
    link = None
    if target_kind == "external":
        target = outside / "new" / "report.txt"
    elif target_kind == "directory_link":
        link = beta_home / "linked"
        link.symlink_to(outside, target_is_directory=True)
        target = link / "report.txt"
    else:
        link = target if target_kind == "leaf_link" else (
            beta_home / ".report.txt.20261112010203000000.tmp"
        )
        link.symlink_to(outside / "sentinel.txt")
    with pytest.raises(ValueError, match="测试版"):
        save_versioned_bytes(target, b"new")
    assert not outside.exists()
    if link is not None:
        assert link.is_symlink()
    assert not (beta_home / ".file_versions.json").exists()


def test_stable_versioned_outputs_keep_external_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "stable")
    monkeypatch.setenv("SPIDER_GD_HOME", str(tmp_path / "home"))
    target = tmp_path / "external" / "report.txt"
    first = save_versioned_bytes(target, b"old")
    source = tmp_path / "source.tmp"
    source.write_bytes(b"new")
    changed = save_versioned_file(source, target)
    assert first.path == changed.path == target
    assert target.read_bytes() == b"new"
    assert changed.archived_path.read_bytes() == b"old"
    assert not source.exists()


@pytest.mark.parametrize("metadata", [".file_versions.json", ".file_versions.json.tmp"])
def test_beta_versioned_bytes_metadata_rejection_preserves_previous_output(
    beta_home, tmp_path, metadata,
):
    target = beta_home / "report.txt"
    target.write_bytes(b"old")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"keep")
    link = beta_home / metadata
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="测试版"):
        save_versioned_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert outside.read_bytes() == b"keep"
    assert link.is_symlink()
    staged = list(beta_home.glob(".report.txt.*.tmp"))
    assert len(staged) == 1 and staged[0].read_bytes() == b"new"
    assert not (beta_home / "_versions").exists()
