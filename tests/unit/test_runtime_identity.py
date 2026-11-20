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
@pytest.mark.parametrize("raw", [None, '  "relative/用户"  ', "~/用户"])
def test_runtime_reports_effective_data_home(tmp_path: Path, monkeypatch, raw):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "stable")
    if raw is None:
        monkeypatch.delenv("SPIDER_GD_HOME", raising=False)
        expected = tmp_path
    else:
        monkeypatch.setenv("SPIDER_GD_HOME", raw)
        expected = (tmp_path / "relative/用户") if "relative" in raw else Path.home() / "用户"
    identity = runtime_identity()
    assert identity.get("data_home") == str(expected.resolve())
    assert identity["cwd"] == str(tmp_path)


@pytest.mark.unit
def test_beta_runtime_reports_default_home_without_migrating(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.delenv("SPIDER_GD_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "profile"))
    from spider_gd.release_channel import default_beta_home

    assert runtime_identity().get("data_home") == str(default_beta_home().resolve())
    assert not (tmp_path / "data").exists()


@pytest.mark.unit
@pytest.mark.parametrize("channel,opposite", [("stable", "beta"), ("beta", "stable")])
def test_frozen_runtime_data_home_is_actual_override_with_bound_channel(tmp_path: Path, monkeypatch, channel, opposite):
    from spider_gd import build_stamp

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(build_stamp, "RELEASE_CHANNEL", channel)
    monkeypatch.setattr(build_stamp, "RELEASE_VERSION", "0.2.8")
    monkeypatch.setenv("SPIDER_GD_CHANNEL", opposite)
    monkeypatch.setenv("SPIDER_GD_HOME", f'  "{tmp_path / "资料"}"  ')
    identity = runtime_identity()
    assert identity["channel"] == channel
    assert identity["data_home"] == str((tmp_path / "资料").resolve())
    assert identity["frozen"] is True


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
