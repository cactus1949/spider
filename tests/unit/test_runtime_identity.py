"""构建身份来自正在跑的进程，不来自磁盘上被覆盖的新文件。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from spider_gd.build_stamp import BUILD_ID
from spider_gd.runtime_info import runtime_identity

_SPEC = importlib.util.spec_from_file_location(
    "build_backend_stamp",
    Path(__file__).resolve().parents[2] / "build_backend.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
build_backend = importlib.util.module_from_spec(_SPEC)
sys.modules["build_backend_stamp"] = build_backend
_SPEC.loader.exec_module(build_backend)


@pytest.mark.unit
def test_running_process_does_not_read_the_new_stamp_file(tmp_path: Path, monkeypatch):
    (tmp_path / "build_stamp.json").write_text('{"build_id": "from-disk"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    identity = runtime_identity()
    assert identity["build_id"] == BUILD_ID
    assert identity["build_id"] != "from-disk"
    assert identity["pid"] > 0


@pytest.mark.unit
def test_build_stamp_written_for_freeze_is_restored():
    original = build_backend.install_build_stamp("abc123def456")
    try:
        text = (build_backend.PACKAGE / "build_stamp.py").read_text(encoding="utf-8")
        assert "abc123def456" in text
    finally:
        build_backend.restore_build_stamp(original)
    restored = (build_backend.PACKAGE / "build_stamp.py").read_text(encoding="utf-8")
    assert 'BUILD_ID = "source"' in restored


@pytest.mark.unit
def test_frozen_ocr_entrypoint_is_not_run_off_windows(capsys):
    build_backend.assert_frozen_ocr(Path("/no/such/spider-gd-service.exe"))
    assert "只在 Windows" in capsys.readouterr().out
