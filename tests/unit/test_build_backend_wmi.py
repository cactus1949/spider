"""Windows 打包参数要带上 WMI 和 pywin32。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
