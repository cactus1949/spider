"""冻结包要带上源码里的模块，不能只靠开发环境 import 成功。"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_backend_freeze",
    Path(__file__).resolve().parents[2] / "build_backend.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
build_backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_backend)


@pytest.mark.unit
def test_repo_file_is_what_packaging_resolves():
    src = str(build_backend.SRC)
    saved = list(sys.path)
    try:
        while src in sys.path:
            sys.path.remove(src)
        sys.path.insert(0, src)
        importlib.invalidate_caches()
        spec = importlib.util.find_spec("spider_gd.core.report_dates")
        assert spec is not None
        assert spec.origin
        resolved = Path(spec.origin).resolve()
        assert build_backend.PACKAGE.resolve() in resolved.parents
    finally:
        sys.path[:] = saved


@pytest.mark.unit
def test_pyinstaller_archive_contains_source_modules(tmp_path: Path):
    """用和正式打包相同的 hidden-import 打一个最小冻结目录，再读 PYZ。

    这不是 Windows 安装包。它只证明这些参数会把仓库模块写进冻结归档。
    """
    pytest.importorskip("PyInstaller")
    entry = tmp_path / "entry.py"
    entry.write_text("print('smoke')\n", encoding="utf-8")
    dist = tmp_path / "dist"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir", "--noconfirm", "--clean",
        "--name", "pkg-smoke",
        "--distpath", str(dist),
        "--workpath", str(tmp_path / "work"),
        "--specpath", str(tmp_path / "spec"),
        "--paths", str(build_backend.SRC),
        "--exclude-module", "torch",
        "--exclude-module", "tensorflow",
        "--exclude-module", "matplotlib",
    ]
    cmd += build_backend.spider_gd_pyinstaller_args()
    cmd.append(str(entry))
    subprocess.run(cmd, check=True, cwd=tmp_path)
    pyz_files = list(dist.rglob("*.pyz")) + list((tmp_path / "work").rglob("*.pyz"))
    assert pyz_files
    from PyInstaller.archive.readers import ZlibArchiveReader

    names = set(ZlibArchiveReader(str(pyz_files[0])).toc)
    missing = build_backend.missing_frozen_modules(
        names, required=build_backend.source_package_modules(),
    )
    assert missing == []
