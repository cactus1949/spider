"""Windows 打包参数要带上 WMI 和 pywin32。"""

from __future__ import annotations

import builtins
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_backend",
    Path(__file__).resolve().parents[2] / "build_backend.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
build_backend = importlib.util.module_from_spec(_SPEC)
sys.modules["build_backend"] = build_backend
_SPEC.loader.exec_module(build_backend)


@pytest.mark.unit
def test_windows_pyinstaller_args_include_wmi_and_pywin32(tmp_path: Path):
    dll = tmp_path / "pythoncom311.dll"
    dll.write_bytes(b"MZ")
    args = build_backend.windows_wmi_pyinstaller_args(is_win=True, dlls=[dll])
    hidden = args[args.index("--hidden-import") + 1 :]
    joined = " ".join(args)
    for name in ("wmi", "win32com", "win32com.client", "pythoncom", "pywintypes"):
        assert name in hidden
    assert "--collect-submodules" in args
    assert "--collect-binaries" in args
    assert "win32com" in args
    assert "win32" in args
    assert f"{dll}{build_backend.os.pathsep}." in joined


@pytest.mark.unit
def test_install_command_uses_the_pyinstaller_interpreter():
    python = r"C:\Users\me\AppData\Local\Programs\Python\Python312\python.exe"
    cmd = build_backend.win_extra_install_command(python)
    assert cmd[0] == python
    assert cmd[1:3] == ["-m", "pip"]
    assert cmd[-1] == ".[online,win]"


@pytest.mark.unit
def test_arm64_build_is_rejected_in_the_hint():
    assert build_backend.is_arm64_python("ARM64")
    assert build_backend.is_arm64_python("aarch64")
    assert not build_backend.is_arm64_python("AMD64")
    hint = build_backend.arm64_windows_python_hint("ARM64")
    assert "x64" in hint
    assert "Parallels" in hint
    message = build_backend.wmi_import_failure_message(
        "No module named 'wmi'",
        arch="ARM64",
        python=r"C:\arm\python.exe",
    )
    assert "No module named 'wmi'" in message
    assert "x64" in message


@pytest.mark.unit
def test_ocr_binaries_are_placed_beside_the_pyd(tmp_path: Path):
    capi = tmp_path / "capi"
    capi.mkdir()
    dll = capi / "onnxruntime.dll"
    pyd = capi / "onnxruntime_pybind11_state.pyd"
    dll.write_bytes(b"MZ")
    pyd.write_bytes(b"MZ")
    pairs = build_backend.onnxruntime_native_files(tmp_path)
    assert (dll, "onnxruntime/capi") in pairs
    assert (pyd, "onnxruntime/capi") in pairs
    args = build_backend.ocr_pyinstaller_args(tmp_path)
    joined = " ".join(args)
    assert "--collect-all" in args and "onnxruntime" in joined and "ddddocr" in joined
    assert f"{dll}{build_backend.os.pathsep}onnxruntime/capi" in joined
    assert build_backend.missing_ocr_binaries(joined) == []


@pytest.mark.unit
def test_certificate_window_library_is_packed_on_windows():
    assert build_backend.certificate_ui_pyinstaller_args(is_win=False) == []
    args = build_backend.certificate_ui_pyinstaller_args(is_win=True)
    assert "uiautomation" in args
    assert "comtypes" in args
    sources = build_backend.source_package_modules()
    assert "spider_gd.core.report_dates" in sources
    assert build_backend.missing_frozen_modules(set(sources), required=sources) == []
    assert "spider_gd.core.report_dates" in build_backend.missing_frozen_modules(
        set(), required=["spider_gd.core.report_dates"],
    )
    windows = build_backend.required_frozen_modules(include_windows_ui=True)
    assert "uiautomation" in build_backend.missing_frozen_modules(set(sources), required=windows)
    args = build_backend.spider_gd_pyinstaller_args()
    assert args[:2] == ["--collect-submodules", "spider_gd"]
    joined = " ".join(args)
    for name in sources:
        assert f"--hidden-import {name}" in joined or name in args
    data = args[args.index("--add-data") + 1]
    src, dest = data.split(build_backend.os.pathsep)
    assert Path(src) == build_backend.PACKAGE
    assert dest == "spider_gd"
    imported = build_backend.runtime_tool_imports()
    assert "spider_gd.core.report_dates" in imported
    assert build_backend.missing_repo_modules(imported, sources) == []


@pytest.mark.unit
def test_missing_data_directory_is_not_packed(tmp_path: Path):
    present = tmp_path / "configs"
    present.mkdir()
    kept = build_backend.existing_data_files(
        [(str(present), "configs"), (str(tmp_path / "workflows"), "workflows")]
    )
    assert kept == [(str(present), "configs")]


@pytest.mark.unit
def test_non_windows_build_does_not_require_wmi():
    args = build_backend.windows_wmi_pyinstaller_args(
        is_win=False,
        dlls=[Path("pythoncom.dll")],
    )
    assert args == []


@pytest.mark.unit
def test_pywin32_dll_search_reads_known_folders(tmp_path: Path):
    folder = tmp_path / "pywin32_system32"
    folder.mkdir()
    (folder / "pywintypes311.dll").write_bytes(b"MZ")
    (folder / "pythoncom311.dll").write_bytes(b"MZ")
    (tmp_path / "unrelated.dll").write_bytes(b"MZ")
    found = {path.name for path in build_backend.pywin32_dlls([tmp_path])}
    assert found == {"pywintypes311.dll", "pythoncom311.dll"}


@pytest.mark.unit
@pytest.mark.parametrize("failure", [None, "wmi", "win32com.client", "pywintypes"])
def test_wmi_probe_exec_initializes_com_before_import_and_always_releases(failure):
    events = []
    pythoncom = SimpleNamespace(
        CoInitialize=lambda: events.append("initialize"),
        CoUninitialize=lambda: events.append("uninitialize"),
    )

    def import_port(name, *args, **kwargs):
        events.append(name)
        if name == "pythoncom":
            return pythoncom
        assert name in ("wmi", "win32com.client", "pywintypes"), "no hardware queries"
        assert "initialize" in events, "WMI imported before COM initialization"
        if name == failure:
            raise ImportError(f"missing {name}")
        return SimpleNamespace(client=SimpleNamespace())

    namespace = {"__builtins__": {**vars(builtins), "__import__": import_port}}
    if failure:
        with pytest.raises(ImportError, match=failure):
            exec(build_backend._WMI_PROBE, namespace)
    else:
        exec(build_backend._WMI_PROBE, namespace)
    expected = ["pythoncom", "initialize", "wmi", "win32com.client", "pywintypes"]
    if failure:
        expected = expected[:expected.index(failure) + 1]
    assert events == [*expected, "uninitialize"]


@pytest.mark.unit
def test_probe_uses_requested_interpreter_and_bytes(monkeypatch):
    def run(command, **options):
        assert command == ["fake-python", "-c", build_backend._WMI_PROBE]
        assert options == {"capture_output": True, "check": False}
        return subprocess.CompletedProcess(command, 1, b"", "导入失败".encode())

    monkeypatch.setattr(build_backend.subprocess, "run", run)
    assert build_backend.probe_wmi_import("fake-python") == "导入失败"


_BUILD_ID = "012345abcdef"
_RELEASE = {"channel": "beta", "version": "0.2.8-beta.1", "serviceName": "spider-gd-service-beta"}


@pytest.mark.unit
@pytest.mark.parametrize("case", [
    "ok", "crt-noise", "exit", "non-json", "json-list", "missing-build-id", "build-id",
    "channel", "version", "timeout", "os-error",
])
def test_frozen_build_runs_exe_and_checks_real_json(monkeypatch, tmp_path, capsys, case):
    assert callable(getattr(build_backend, "assert_frozen_build", None)), (
        "missing executable build self-test"
    )
    monkeypatch.setattr(build_backend, "IS_WIN", True)
    exe = tmp_path / "fake.exe"
    identity = {"build_id": _BUILD_ID, "channel": "beta", "version": "0.2.8-beta.1"}
    if case in ("build-id", "channel", "version"):
        identity[{"build-id": "build_id"}.get(case, case)] = "wrong"
    if case == "missing-build-id":
        del identity["build_id"]
    stdout = (
        b"not JSON" if case == "non-json"
        else json.dumps([] if case == "json-list" else identity).encode()
    )
    if case == "crt-noise":
        # 真实现象：身份 JSON 之后，Windows CRT 追加一行 UTF-16 的 COM 释放报错。
        stdout += b"\r\nW\x00i\x00n\x003\x002\x00 \x00e\x00x\x00c\x00e\x00p\x00t\x00i\x00o\x00n\x00\n\x00"
    calls = []

    def run(command, **options):
        calls.append(command)
        assert command == [str(exe), "--self-test-build"]
        assert options["capture_output"] is True
        assert options["check"] is False
        assert not options.get("text")
        assert "encoding" not in options
        assert 0 < options["timeout"] <= 180
        if case == "timeout":
            raise subprocess.TimeoutExpired(command, options["timeout"])
        if case == "os-error":
            raise OSError("cannot launch fake exe")
        return subprocess.CompletedProcess(
            command, 1 if case == "exit" else 0, stdout, "依赖失败".encode(),
        )

    monkeypatch.setattr(build_backend.subprocess, "run", run)
    if case in ("ok", "crt-noise"):
        build_backend.assert_frozen_build(exe, _RELEASE, _BUILD_ID)
    else:
        with pytest.raises(SystemExit) as error:
            build_backend.assert_frozen_build(exe, _RELEASE, _BUILD_ID)
        assert str(error.value)
        if case == "exit":
            assert "依赖失败" in str(error.value)
        assert "[done]" not in capsys.readouterr().out
    assert len(calls) == 1


@pytest.mark.unit
def test_frozen_build_non_windows_explicitly_skips(monkeypatch, tmp_path, capsys):
    assert callable(getattr(build_backend, "assert_frozen_build", None)), (
        "missing executable build self-test"
    )
    monkeypatch.setattr(build_backend, "IS_WIN", False)
    monkeypatch.setattr(
        build_backend.subprocess, "run", lambda *a, **k: pytest.fail("native exe must not run"),
    )
    build_backend.assert_frozen_build(tmp_path / "fake.exe", _RELEASE, _BUILD_ID)
    output = capsys.readouterr().out
    assert "[skip]" in output
    assert "Windows" in output
    assert "[done]" not in output


@pytest.mark.unit
@pytest.mark.parametrize("result", ["ok", "missing-exe", "wrong-build"])
def test_pyinstaller_success_requires_exe_and_its_self_test(monkeypatch, tmp_path, capsys, result):
    output = tmp_path / "output"
    monkeypatch.setattr(build_backend, "OUTPUT", output)
    monkeypatch.setattr(build_backend, "BUILD", tmp_path / "build")
    monkeypatch.setattr(build_backend, "IS_WIN", True)
    monkeypatch.setattr(build_backend, "EXE_SUFFIX", ".exe")
    monkeypatch.setattr(build_backend, "DATA_FILES", [])
    for name in (
        "assert_repo_package_wins", "clean_build", "restore_build_stamp", "assert_frozen_modules",
        "assert_frozen_ocr", "assert_ocr_frozen",
    ):
        monkeypatch.setattr(build_backend, name, lambda *a, **k: None)
    monkeypatch.setattr(build_backend, "stage_release_data", lambda *a: [])
    monkeypatch.setattr(build_backend, "install_build_stamp", lambda *a, **k: "original")
    monkeypatch.setattr(build_backend, "current_build_id", lambda: _BUILD_ID)
    monkeypatch.setattr(build_backend, "onnxruntime_package_dir", lambda: tmp_path)
    monkeypatch.setattr(build_backend, "ocr_pyinstaller_args", lambda *a: [])
    monkeypatch.setattr(build_backend, "_pywin32_search_roots", lambda: [])
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    calls = []

    def run(command, **options):
        calls.append(command)
        if command[:3] == [sys.executable, "-m", "PyInstaller"]:
            if result != "missing-exe":
                (output / "spider-gd-service-beta.exe").write_bytes(b"fake; never execute")
            return subprocess.CompletedProcess(command, 0)
        assert command == [str(output / "spider-gd-service-beta.exe"), "--self-test-build"]
        identity = {
            "channel": "beta", "version": "0.2.8-beta.1",
            "build_id": _BUILD_ID if result == "ok" else "ffffffffffff",
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(identity).encode(), b"")

    monkeypatch.setattr(build_backend.subprocess, "run", run)
    if result == "ok":
        build_backend.run_pyinstaller(_RELEASE)
        assert len(calls) == 2, "archive inspection cannot replace running the frozen self-test"
    else:
        with pytest.raises(SystemExit, match=r"exe|build_id|构建"):
            build_backend.run_pyinstaller(_RELEASE)
        assert not (output / ".env").exists()
        assert "[done]" not in capsys.readouterr().out
