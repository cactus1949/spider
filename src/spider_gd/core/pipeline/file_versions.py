"""Hash-based local file versioning for downloaded data artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from spider_gd.config.user_home import validate_write_path


@dataclass(frozen=True)
class VersionedSaveResult:
    path: Path
    sha256: str
    changed: bool
    reused_existing: bool = False
    archived_path: Path | None = None


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_versioned_bytes(dest: str | Path, data: bytes) -> VersionedSaveResult:
    dest_path = validate_write_path(dest, "版本文件输出")
    validate_write_path(dest_path.parent, "版本文件输出目录")
    tmp_path = validate_write_path(
        dest_path.parent / f".{dest_path.name}.{datetime.now().strftime('%Y%m%d%H%M%S%f')}.tmp",
        "版本文件临时输出",
    )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_bytes(data)
    return save_versioned_file(tmp_path, dest_path)


def save_versioned_file(source: str | Path, dest: str | Path) -> VersionedSaveResult:
    source_path = validate_write_path(source, "版本文件临时来源")
    dest_path = validate_write_path(dest, "版本文件输出")
    # move/unlink 会修改父目录中的条目，不能消费目录外指向目录内的符号链接。
    validate_write_path(source_path.parent, "版本文件临时来源目录")
    validate_write_path(dest_path.parent, "版本文件输出目录")
    # 已知的清单目标必须先检查，避免拒绝时已经消费临时文件或替换旧输出。
    manifest_path = validate_write_path(dest_path.parent / ".file_versions.json", "版本文件清单")
    validate_write_path(manifest_path.with_suffix(".json.tmp"), "版本文件临时清单")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    new_hash = file_sha256(source_path)
    archived: Path | None = None

    if dest_path.exists():
        old_hash = file_sha256(dest_path)
        if old_hash == new_hash:
            source_path.unlink(missing_ok=True)
            _record_manifest(dest_path, old_hash)
            return VersionedSaveResult(
                path=dest_path,
                sha256=old_hash,
                changed=False,
                reused_existing=True,
            )

        archived = _archive_existing(dest_path, old_hash)
        shutil.move(str(source_path), str(dest_path))
        _record_manifest(dest_path, new_hash, archived_path=archived, archived_sha256=old_hash)
        return VersionedSaveResult(path=dest_path, sha256=new_hash, changed=True, archived_path=archived)

    shutil.move(str(source_path), str(dest_path))
    _record_manifest(dest_path, new_hash)
    return VersionedSaveResult(path=dest_path, sha256=new_hash, changed=True)


def _archive_existing(path: Path, sha256: str) -> Path:
    version_dir = validate_write_path(path.parent / "_versions", "版本文件归档目录")
    version_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archived = version_dir / f"{path.stem}_{stamp}_{sha256[:12]}{path.suffix}"
    counter = 1
    while archived.exists():
        archived = version_dir / f"{path.stem}_{stamp}_{sha256[:12]}_{counter}{path.suffix}"
        counter += 1
    archived = validate_write_path(archived, "版本文件归档")
    shutil.copy2(path, archived)
    return archived


def _record_manifest(
    path: Path,
    current_sha256: str,
    *,
    archived_path: Path | None = None,
    archived_sha256: str = "",
) -> None:
    manifest_path = validate_write_path(path.parent / ".file_versions.json", "版本文件清单")
    tmp = validate_write_path(manifest_path.with_suffix(".json.tmp"), "版本文件临时清单")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    files = manifest.setdefault("files", {})
    rel_name = path.name
    item = files.setdefault(rel_name, {"versions": []})
    item["current_sha256"] = current_sha256
    item["updated_at"] = now
    if archived_path is not None:
        item.setdefault("versions", []).append({
            # 清单会被上传/跨平台读取，路径统一存 POSIX 分隔符。
            "path": archived_path.relative_to(path.parent).as_posix(),
            "sha256": archived_sha256,
            "archived_at": now,
        })
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(manifest_path)
