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


def _patch_windows_image(monkeypatch, write):
    """只替换 OS 调用（GetModuleFileNameW）本身；缓冲区与失败分支走真实代码。"""
    from types import SimpleNamespace

    from spider_gd import runtime_info

    calls = []

    class FakeCall:
        argtypes = None
        restype = None

        def __call__(self, module, buffer, size):
            calls.append({"module": module, "size": size, "argtypes": self.argtypes, "restype": self.restype})
            return write(buffer, size)

    call = FakeCall()
    monkeypatch.setattr(runtime_info.os, "name", "nt")
    monkeypatch.setattr(runtime_info, "_kernel32", lambda: SimpleNamespace(GetModuleFileNameW=call))
    return calls


@pytest.mark.unit
def test_runtime_executable_off_windows_is_resolved_sys_executable(monkeypatch):
    from spider_gd import runtime_info

    monkeypatch.setattr(runtime_info.os, "name", "posix")
    assert runtime_info.runtime_executable() == Path(sys.executable).resolve()


@pytest.mark.unit
def test_windows_runtime_executable_reports_real_process_image(monkeypatch):
    from spider_gd import runtime_info

    base = "C:\\Users\\jayden\\AppData\\Local\\Programs\\Python\\Python311\\python.exe"

    def write(buffer, size):
        buffer.value = base
        return len(base)

    calls = _patch_windows_image(monkeypatch, write)
    assert runtime_info.runtime_executable() == Path(base)
    assert len(calls) == 1
    assert calls[0]["module"] is None
    assert calls[0]["size"] >= 32768
    assert calls[0]["argtypes"] is not None and len(calls[0]["argtypes"]) == 3
    assert calls[0]["restype"] is not None


@pytest.mark.unit
def test_windows_runtime_executable_fails_closed_on_zero_result(monkeypatch):
    from spider_gd import runtime_info

    _patch_windows_image(monkeypatch, lambda buffer, size: 0)
    with pytest.raises(OSError):
        runtime_info.runtime_executable()


@pytest.mark.unit
def test_windows_runtime_executable_fails_closed_on_truncation(monkeypatch):
    from spider_gd import runtime_info

    def write(buffer, size):
        buffer.value = "C:\\short"
        return size

    _patch_windows_image(monkeypatch, write)
    with pytest.raises(OSError):
        runtime_info.runtime_executable()


@pytest.mark.unit
def test_runtime_identity_uses_runtime_executable(tmp_path, monkeypatch):
    from spider_gd import runtime_info

    monkeypatch.chdir(tmp_path)
    sentinel = Path(sys.executable).resolve().parent / "real-image-python"
    monkeypatch.setattr(runtime_info, "runtime_executable", lambda: sentinel)
    assert runtime_info.runtime_identity()["executable"] == str(sentinel)


@pytest.mark.unit
def test_importing_runtime_info_creates_no_service_or_home(tmp_path):
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("SPIDER_GD")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import spider_gd.runtime_info"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
