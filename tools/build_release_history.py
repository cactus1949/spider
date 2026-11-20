#!/usr/bin/env python3
"""把 release-history.json 生成某个发布渠道的 history.json。

版本中心（Electron 主进程）从发布服务器读取 ``<updateUrl>/history.json``：

    正式版：http://47.101.175.39:20152/releases/history.json
    测试版：http://47.101.175.39:20152/releases/beta/history.json

这个文件不是数据库，就是发布服务器上的一个静态 JSON，必须随每次发版一起上传。
本脚本负责校验条目并把内部维护用的 release-history.json 转成渠道文件。

用法::

    python tools/build_release_history.py --channel beta --out frontend/dist/history.json
    python tools/build_release_history.py --channel stable --installer-dir frontend/dist
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "release-history.json"

CHANNEL_PATTERNS = {
    "stable": re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"),
    "beta": re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-beta\.([1-9]\d*)$"),
}
MAX_RELEASES = 200
MAX_NOTES = 100
MAX_NOTE_LENGTH = 4000
ALLOWED_KEYS = {"version", "publishedAt", "notes"}
PRODUCT_NAMES = {
    "stable": "广东电力数据采集",
    "beta": "广东电力数据采集－测试版",
}


def version_key(version: str) -> tuple[int, ...]:
    """按数字段排序，``0.2.10`` 排在 ``0.2.9`` 之前。"""
    parts = [int(item) for item in re.findall(r"\d+", version)]
    if "-beta." in version:
        # 同一基础版本内 beta.10 大于 beta.2；基础版本相同才比较预发布号。
        base = leading_base(version)
        return (*base, 10**9, parts[-1])
    return (*leading_base(version), -1, 0)


def leading_base(version: str) -> tuple[int, int, int]:
    """取 majro.minor.patch 三段数字（beta 后缀忽略）。"""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?$", version)
    if not match:
        raise ValueError(f"无法解析版本号：{version}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def validate_entry(channel: str, item: object, seen: set[str]) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ValueError("版本记录必须是对象")
    extra = set(item) - ALLOWED_KEYS
    if extra:
        raise ValueError(f"版本记录包含不支持的字段：{sorted(extra)}")
    version = item.get("version")
    if not isinstance(version, str) or not CHANNEL_PATTERNS[channel].fullmatch(version):
        raise ValueError(f"版本号与渠道 {channel} 不一致：{version!r}")
    if version in seen:
        raise ValueError(f"版本记录重复：{version}")
    seen.add(version)
    published_at = item.get("publishedAt")
    if not isinstance(published_at, str):
        raise ValueError("publishedAt 必须是 YYYY-MM-DD 字符串")
    try:
        parsed = date.fromisoformat(published_at)
    except ValueError as exc:
        raise ValueError(f"publishedAt 不是合法日期：{published_at}") from exc
    if parsed.isoformat() != published_at:
        raise ValueError(f"publishedAt 必须是 YYYY-MM-DD：{published_at}")
    notes = item.get("notes", [])
    if not isinstance(notes, list) or len(notes) > MAX_NOTES:
        raise ValueError("notes 必须是不超过 100 条的字符串数组")
    if any(not isinstance(note, str) or len(note) > MAX_NOTE_LENGTH for note in notes):
        raise ValueError("notes 每条必须是不超过 4000 字的字符串")
    return {"version": version, "publishedAt": published_at, "notes": list(notes)}


def build_history(source: Path, channel: str, installer_dir: Path | None = None) -> dict[str, object]:
    """构造某个渠道的 history.json 内容。"""
    if channel not in CHANNEL_PATTERNS:
        raise ValueError(f"不支持的渠道：{channel}")
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("release-history.json 顶层必须是对象")
    entries = data.get(channel, [])
    if not isinstance(entries, list) or len(entries) > MAX_RELEASES:
        raise ValueError(f"{channel} 版本记录必须是不超过 200 条的数组")
    seen: set[str] = set()
    releases = [validate_entry(channel, item, seen) for item in entries]
    releases.sort(key=lambda item: version_key(str(item["version"])), reverse=True)
    missing: list[str] = []
    if installer_dir is not None:
        for item in releases:
            name = f"{PRODUCT_NAMES[channel]}-{item['version']}-setup.exe"
            if not (installer_dir / name).is_file():
                missing.append(name)
        if missing:
            raise ValueError("以下安装包不存在，不能写进版本记录：\n  " + "\n  ".join(missing))
    return {"schemaVersion": 1, "channel": channel, "releases": releases}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成发布服务器的 history.json")
    parser.add_argument("--channel", choices=sorted(CHANNEL_PATTERNS), required=True)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, help="输出文件；省略则打印到 stdout")
    parser.add_argument("--installer-dir", type=Path, help="额外校验该目录下每个版本安装包是否存在")
    args = parser.parse_args(argv)
    try:
        history = build_history(args.source, args.channel, args.installer_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    text = json.dumps(history, ensure_ascii=False, indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"[done] {args.channel} history.json -> {args.out}（{len(history['releases'])} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
