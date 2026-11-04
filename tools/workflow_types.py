"""
工作流类型系统：统一管理 login / indicator 分类、登录依赖、起点。

设计：
  type: "login" | "indicator"       — 工作流类型（默认自动推断）
  needs_login: true | false         — 仅 indicator，是否需要登录后执行
  start_from: "main" | "dashboard"  — 起点：主界面 or 登录后工作台

推断规则（兼容旧格式）：
  - 首步 action=="login"                        → type=login, start_from=main
  - 有 no_login: true 或注释含"无需登录"         → needs_login=false, start_from=main
  - 其他                                        → needs_login=true, start_from=dashboard
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


def infer_meta(cfg: dict) -> dict:
    """从旧格式 workflow 推断 type / needs_login / start_from。"""
    steps = cfg.get("steps", [])
    first_action = steps[0].get("action", "") if steps else ""

    # type
    inferred_type = cfg.get("type", "")
    if not inferred_type:
        if first_action == "login":
            inferred_type = "login"
        else:
            inferred_type = "indicator"

    # needs_login（仅 indicator）
    needs_login = cfg.get("needs_login", None)
    if needs_login is None:
        if cfg.get("no_login") is True:
            needs_login = False
        elif _hint_no_login(cfg):
            needs_login = False
        else:
            needs_login = True  # 默认需要登录

    # start_from
    start_from = cfg.get("start_from", "")
    if not start_from:
        if inferred_type == "login":
            start_from = "main"
        elif not needs_login:
            start_from = "main"
        else:
            start_from = "dashboard"

    return {
        "type": inferred_type,
        "needs_login": needs_login,
        "start_from": start_from,
    }


def _hint_no_login(cfg: dict) -> bool:
    """从描述/注释中检测是否包含'无需登录'等关键词。"""
    desc = cfg.get("description", "")
    if isinstance(desc, str) and re.search(r"无需登录|无需登陆|公开页面|公开数据", desc):
        return True
    # 也检查 YAML 注释（raw text 需要在外部处理，这里只做描述字段）
    return False


def load_workflow(path: str | Path) -> dict:
    """加载工作流 YAML，自动附加类型元数据。"""
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(raw) or {}
    meta = infer_meta(cfg)
    cfg.setdefault("type", meta["type"])
    cfg.setdefault("needs_login", meta["needs_login"])
    cfg.setdefault("start_from", meta["start_from"])
    return cfg


def migrate_workflow(path: str | Path, dry_run: bool = False) -> dict | None:
    """给单个工作流 YAML 添加 type / needs_login / start_from 字段，返回修改内容。"""
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(raw) or {}

    meta = infer_meta(cfg)
    changed = False

    for key, val in meta.items():
        if key not in cfg:
            cfg[key] = val
            changed = True

    if changed and not dry_run:
        # 按标准顺序排列 key
        ordered = {}
        for key in ("name", "type", "category", "needs_login", "start_from",
                     "description", "no_login", "steps"):
            if key in cfg:
                ordered[key] = cfg.pop(key)
        ordered.update(cfg)  # 其余字段

        # 写回（保留原有格式风格）
        output = yaml.dump(ordered, allow_unicode=True, default_flow_style=False,
                           sort_keys=False, width=120)
        path.write_text(output, encoding="utf-8")

    return {"path": str(path), "changed": changed, "meta": meta}


def find_login_workflow(workflows_dir: str | Path) -> dict | None:
    """在 workflows 目录中查找 type=login 的工作流。"""
    for f in sorted(Path(workflows_dir).glob("*.yml")):
        try:
            cfg = load_workflow(f)
            if cfg.get("type") == "login":
                return cfg
        except Exception:
            pass
    return None
