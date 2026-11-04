"""HAR 分析（纯函数，可离线测试）：提取 API 端点与认证信息。

这是侦察工具最有价值的产出加工环节，直接喂给 ``api_fetcher`` 的配置。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# URL/参数中疑似认证字段（含平台特有的 HdKGccbL）
_SUSPECT_PARAMS = ("HdKGccbL", "token", "sign", "_t")

# 导出类响应的 content-type 关键字（最有价值）
_EXPORT_CTYPES = ("spreadsheet", "excel", "octet-stream", "csv")
_STATIC_CTYPES = ("javascript", "css", "image", "font")


def _classify(content_type: str) -> str:
    ct = content_type.lower()
    if any(x in ct for x in _STATIC_CTYPES):
        return "static"
    if "html" in ct:
        return "page"
    if any(x in ct for x in _EXPORT_CTYPES):
        return "export"
    if "json" in ct:
        return "data"
    return "other"


def _load_har(har: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(har, dict):
        return har
    return json.loads(Path(har).read_text(encoding="utf-8"))


def analyze_har(har: dict[str, Any] | str | Path) -> dict[str, Any]:
    """解析 HAR，按 method+path 去重，分类标注每个请求。

    返回 ``{"raw": [...], "deduped": {...}, "exports": [...]}``。
    """
    data = _load_har(har)
    entries = data.get("log", {}).get("entries", [])

    api_list: list[dict[str, Any]] = []
    for entry in entries:
        req = entry.get("request", {})
        res = entry.get("response", {})
        url = req.get("url", "")
        path = urlparse(url).path
        ctype = next(
            (h["value"] for h in res.get("headers", []) if h["name"].lower() == "content-type"),
            "",
        )
        api_list.append(
            {
                "method": req.get("method", ""),
                "url": url,
                "path": path,
                "status": res.get("status", 0),
                "content_type": ctype,
                "category": _classify(ctype),
                "size": res.get("bodySize", 0),
            }
        )

    deduped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for api in api_list:
        deduped[f"{api['method']} {api['path']}"].append(api)

    exports = [a for a in api_list if a["category"] == "export"]
    return {"raw": api_list, "deduped": dict(deduped), "exports": exports}


def extract_auth(har: dict[str, Any] | str | Path) -> dict[str, Any]:
    """扫描 HAR 提取 Bearer Token / Cookie / 特殊认证参数（HdKGccbL 等）。"""
    data = _load_har(har)
    entries = data.get("log", {}).get("entries", [])

    bearer_tokens: set[str] = set()
    cookies: dict[str, str] = {}
    special_params: dict[str, set[str]] = defaultdict(set)

    for entry in entries:
        req = entry.get("request", {})
        for h in req.get("headers", []):
            name = h["name"].lower()
            if name == "authorization":
                bearer_tokens.add(h["value"])
            elif name == "cookie":
                for kv in h["value"].split(";"):
                    if "=" in kv:
                        k, v = kv.strip().split("=", 1)
                        cookies[k] = v
        url = req.get("url", "")
        qs = parse_qs(urlparse(url).query)
        for suspect in _SUSPECT_PARAMS:
            if suspect in qs:
                special_params[suspect].update(qs[suspect])

    return {
        "bearer_tokens": sorted(bearer_tokens),
        "cookies": cookies,
        "special_params": {k: sorted(v) for k, v in special_params.items()},
    }
