"""录制工坊 ZIP 元数据读取（export_id / 内容哈希）。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_workshop_zip_meta(zip_path: Path) -> dict[str, Any]:
    """从 ZIP 内 *.raw.json 读取 export_id；旧包无 ID 时用 legacy-{hash}。"""
    zip_path = Path(zip_path)
    export_id = ""
    name = zip_path.stem
    description = ""

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            raw_names = [n for n in zf.namelist() if n.endswith(".raw.json")]
            if raw_names:
                data = json.loads(zf.read(raw_names[0]).decode("utf-8"))
                export_id = str(data.get("export_id") or "").strip()
                name = str(data.get("name") or name)
                description = str(data.get("description") or "")
    except Exception:
        pass

    file_hash = file_sha256(zip_path)
    if not export_id:
        export_id = f"legacy-{file_hash[:16]}"

    return {
        "export_id": export_id,
        "file_hash": file_hash,
        "name": name,
        "description": description,
    }


def resolve_submission_status(submission: dict[str, Any] | None, current_hash: str) -> str:
    """pending | submitted | changed | failed"""
    if not submission:
        return "pending"
    status = str(submission.get("status") or "pending")
    if status == "submitted":
        if str(submission.get("file_hash") or "") != current_hash:
            return "changed"
        return "submitted"
    if status == "failed":
        return "failed"
    return "pending"
