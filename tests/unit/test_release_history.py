"""版本中心 history.json 生成器必须与 Electron 端解析规则一致。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_release_history",
    Path(__file__).resolve().parents[2] / "tools" / "build_release_history.py",
)
assert _SPEC is not None and _SPEC.loader is not None
history = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(history)


def write_source(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "release-history.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_sorts_versions_numerically_and_drops_notes_order_issues(tmp_path):
    source = write_source(tmp_path, {
        "stable": [
            {"version": "0.2.9", "publishedAt": "2026-11-10", "notes": []},
            {"version": "0.2.10", "publishedAt": "2026-11-11", "notes": ["说明"]},
        ],
        "beta": [],
    })
    result = history.build_history(source, "stable")
    assert [item["version"] for item in result["releases"]] == ["0.2.10", "0.2.9"]
    assert result["schemaVersion"] == 1 and result["channel"] == "stable"


def test_beta_ordering_uses_prerelease_number(tmp_path):
    source = write_source(tmp_path, {"beta": [
        {"version": "0.2.8-beta.2", "publishedAt": "2026-11-20", "notes": []},
        {"version": "0.2.8-beta.10", "publishedAt": "2026-11-21", "notes": []},
    ]})
    result = history.build_history(source, "beta")
    assert [item["version"] for item in result["releases"]] == ["0.2.8-beta.10", "0.2.8-beta.2"]


@pytest.mark.parametrize("channel,release", [
    ("stable", {"version": "0.2.8-beta.1", "publishedAt": "2026-11-20", "notes": []}),
    ("beta", {"version": "0.2.8", "publishedAt": "2026-11-20", "notes": []}),
    ("stable", {"version": "0.2.8", "publishedAt": "2026-11-20", "notes": [], "url": "x"}),
    ("stable", {"version": "0.2.8", "publishedAt": "2026-02-30", "notes": []}),
    ("stable", {"version": "0.2.8", "publishedAt": "2026-11-20", "notes": [42]}),
    ("stable", {"version": "0.2.8", "publishedAt": "2026-11-20", "notes": ["x" * 4001]}),
])
def test_rejects_invalid_entries(tmp_path, channel, release):
    source = write_source(tmp_path, {channel: [release]})
    with pytest.raises(ValueError):
        history.build_history(source, channel)


def test_rejects_duplicate_versions(tmp_path):
    entry = {"version": "0.2.8", "publishedAt": "2026-11-20", "notes": []}
    source = write_source(tmp_path, {"stable": [entry, entry]})
    with pytest.raises(ValueError, match="重复"):
        history.build_history(source, "stable")


def test_optional_installer_check_requires_every_file(tmp_path):
    source = write_source(tmp_path, {"stable": [
        {"version": "0.2.8", "publishedAt": "2026-11-20", "notes": []},
        {"version": "0.2.7", "publishedAt": "2026-11-19", "notes": []},
    ]})
    installer_dir = tmp_path / "dist"
    installer_dir.mkdir()
    (installer_dir / "广东电力数据采集-0.2.8-setup.exe").write_bytes(b"x")
    with pytest.raises(ValueError, match="0.2.7"):
        history.build_history(source, "stable", installer_dir)


def test_writes_utf8_file(tmp_path):
    source = write_source(tmp_path, {"beta": [
        {"version": "0.2.8-beta.1", "publishedAt": "2026-11-20", "notes": ["中文说明"]},
    ]})
    out = tmp_path / "out" / "history.json"
    assert history.main(["--channel", "beta", "--source", str(source), "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["releases"][0]["notes"] == ["中文说明"]
