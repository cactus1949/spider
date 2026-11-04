"""WorkflowRunner generated data outputs use hash-based versioning."""

from __future__ import annotations

import json

import pytest

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
