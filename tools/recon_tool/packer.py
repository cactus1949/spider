"""侦察目录打包为 zip。"""

from __future__ import annotations

import zipfile
from pathlib import Path


def pack_dir(src_dir: Path | str, zip_path: Path | str | None = None) -> Path:
    """将整个侦察目录打成 zip，返回 zip 路径。"""
    src = Path(src_dir)
    target = Path(zip_path) if zip_path else src.with_suffix(".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in src.rglob("*"):
            if file.is_file() and file.resolve() != target.resolve():
                zf.write(file, file.relative_to(src))
    return target
