"""SQLite 任务状态追踪（仅状态，不存业务数据）。

实现 :class:`spider_gd.interfaces.storage.ITaskStateStore`。WAL 模式提升并发读写。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from loguru import logger

from spider_gd.interfaces.storage import (
    ExecutionLogRecord,
    RecordWorkshopRecord,
    TaskItemRecord,
    TaskRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,
    output_dir  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS task_items (
    task_id        TEXT NOT NULL,
    indicator_name TEXT NOT NULL,
    target_date    TEXT NOT NULL,
    status         TEXT NOT NULL,
    file_path      TEXT,
    file_size      INTEGER DEFAULT 0,
    error_msg      TEXT,
    PRIMARY KEY (task_id, indicator_name, target_date)
);
CREATE INDEX IF NOT EXISTS idx_items_status ON task_items(task_id, status);

-- 工作流配置（指标 & 登录流程）
CREATE TABLE IF NOT EXISTS workflow_configs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'indicator',
    needs_login INTEGER NOT NULL DEFAULT 1,
    start_from  TEXT NOT NULL DEFAULT 'dashboard',
    yaml_text   TEXT NOT NULL DEFAULT '',
    group_id    TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- 任务配置（自动化采集定义）
CREATE TABLE IF NOT EXISTS task_configs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    login       TEXT NOT NULL DEFAULT '',
    indicators  TEXT NOT NULL DEFAULT '[]',   -- JSON array
    repeat      TEXT NOT NULL DEFAULT 'daily',
    time        TEXT NOT NULL DEFAULT '09:00',
    weekdays    TEXT NOT NULL DEFAULT '[]',   -- JSON array
    backfill_days INTEGER NOT NULL DEFAULT 1,
    cover_days  INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    remark      TEXT NOT NULL DEFAULT '',
    download_dir TEXT NOT NULL DEFAULT '',
    download_dir_custom INTEGER NOT NULL DEFAULT 0,
    indicator_windows TEXT NOT NULL DEFAULT '',
    output_windows TEXT NOT NULL DEFAULT '',
    path_template TEXT NOT NULL DEFAULT '',
    path_folders TEXT NOT NULL DEFAULT '',
    path_structure TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- 执行日志（指标播放/定时任务自动/手动 的持久化运行记录）
CREATE TABLE IF NOT EXISTS execution_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_id     TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    reason      TEXT DEFAULT '',
    detail_log  TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_logs_status ON execution_logs(status);
CREATE INDEX IF NOT EXISTS idx_exec_logs_created ON execution_logs(created_at DESC);

-- 应用设置（key-value，替代 localStorage）
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 录制工坊（HAR 录制 + 需求描述）
CREATE TABLE IF NOT EXISTS record_workshops (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    har_path    TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'draft',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 自定义路由规则（站点 → 网卡）
CREATE TABLE IF NOT EXISTS route_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_pattern  TEXT NOT NULL,
    interface_name  TEXT NOT NULL,
    expected_ssid   TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS workshop_submissions (
    export_id     TEXT PRIMARY KEY,
    filename      TEXT NOT NULL DEFAULT '',
    local_path    TEXT NOT NULL DEFAULT '',
    file_hash     TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    remote_url    TEXT NOT NULL DEFAULT '',
    submitted_at  TEXT NOT NULL DEFAULT '',
    error_msg     TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS delivery_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL UNIQUE,
    task_dir      TEXT NOT NULL DEFAULT '',
    zip_path      TEXT NOT NULL DEFAULT '',
    summary_json  TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    zip_identity  TEXT NOT NULL DEFAULT '',
    zip_size      INTEGER NOT NULL DEFAULT 0,
    zip_sha256    TEXT NOT NULL DEFAULT '',
    delivery_identity TEXT NOT NULL DEFAULT '',
    remote_url    TEXT NOT NULL DEFAULT '',
    file_sent     INTEGER NOT NULL DEFAULT 0,
    delivery_note TEXT NOT NULL DEFAULT '',
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    delivered_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_status ON delivery_outbox(status, updated_at);
"""


class TaskStateStore:
    def __init__(self, db_path: str | Path, enable_wal: bool = True) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        # check_same_thread=False：允许调度器/REST/异步等多线程访问，写操作由 _write_lock 串行化
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if enable_wal and self._db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # 兼容旧库：补齐 task_configs 的下载目录/路径模板列
        self._migrate_task_configs_schema()
        self._migrate_workflow_groups_schema()
        self._migrate_route_rules_schema()
        self._migrate_workshop_submissions_schema()
        self._migrate_delivery_outbox_schema()
        logger.debug("SQLite 状态库就绪：{}", self._db_path)

    def close(self) -> None:
        self._conn.close()

    def _migrate_workflow_groups_schema(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(workflow_configs)")}
        if "group_id" not in columns:
            with self._write_lock:
                self._conn.execute("ALTER TABLE workflow_configs ADD COLUMN group_id TEXT")
                self._conn.commit()

    def list_workflow_groups(self) -> list[dict]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM workflow_groups ORDER BY name")]

    def get_workflow_group(self, group_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM workflow_groups WHERE id=?", (group_id,)).fetchone()
        return dict(row) if row else None

    def save_workflow_group(self, group_id: str, name: str, parent_id: str | None) -> None:
        import time
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO workflow_groups (id,name,parent_id,created_at,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,parent_id=excluded.parent_id,updated_at=excluded.updated_at""",
                (group_id, name, parent_id, now, now),
            )
            self._conn.commit()

    def delete_workflow_group(self, group_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute("DELETE FROM workflow_groups WHERE id=?", (group_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def _migrate_task_configs_schema(self) -> None:
        """为旧库自动添加缺失的列（幂等，不影响已有数据）。"""
        try:
            existing = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(task_configs)").fetchall()
            }
        except Exception:
            return
        for col, spec in [
            ("download_dir", "TEXT NOT NULL DEFAULT ''"),
            ("download_dir_custom", "INTEGER NOT NULL DEFAULT 0"),
            ("indicator_windows", "TEXT NOT NULL DEFAULT ''"),
            ("output_windows", "TEXT NOT NULL DEFAULT ''"),
            ("path_template", "TEXT NOT NULL DEFAULT ''"),
            ("path_folders", "TEXT NOT NULL DEFAULT ''"),
            ("path_structure", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in existing:
                try:
                    self._conn.execute(f"ALTER TABLE task_configs ADD COLUMN {col} {spec}")
                    self._conn.commit()
                    logger.info("数据库迁移：task_configs 添加列 {}", col)
                except Exception as exc:
                    logger.warning("数据库迁移跳过 {}: {}", col, exc)

    def _migrate_route_rules_schema(self) -> None:
        """为旧路由规则表补充可选 SSID 校验列。"""
        try:
            existing = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(route_rules)").fetchall()
            }
        except Exception:
            return
        if "expected_ssid" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE route_rules ADD COLUMN expected_ssid TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
                logger.info("数据库迁移：route_rules 添加列 expected_ssid")
            except Exception as exc:
                logger.warning("数据库迁移跳过 route_rules.expected_ssid: {}", exc)

    # ── 任务配置 CRUD ──

    def list_task_configs(self) -> list[dict]:
        """返回所有任务配置。"""
        rows = self._conn.execute(
            "SELECT * FROM task_configs ORDER BY created_at DESC"
        ).fetchall()
        return [_task_config_row(r) for r in rows]

    def get_task_config(self, config_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM task_configs WHERE id = ?", (config_id,)
        ).fetchone()
        return _task_config_row(row) if row else None

    def save_task_config(self, cfg: dict) -> str:
        """创建或更新任务配置。"""
        import json
        import time

        now = time.time()
        existing = self.get_task_config(cfg["id"])
        with self._write_lock:
            if existing:
                windows_json = _indicator_windows_json(cfg.get("indicator_windows"))
                output_windows_json = _indicator_windows_json(cfg.get("output_windows"))
                folders_json = _path_folders_json(cfg.get("path_folders"))
                self._conn.execute(
                    """UPDATE task_configs SET
                        name=?, login=?, indicators=?, repeat=?, time=?,
                        weekdays=?, backfill_days=?, cover_days=?,
                        enabled=?, remark=?, download_dir=?, download_dir_custom=?, indicator_windows=?,
                        output_windows=?, path_template=?, path_folders=?, path_structure=?, updated_at=?
                    WHERE id=?""",
                    (
                        cfg["name"], cfg.get("login", ""),
                        json.dumps(cfg.get("indicators", [])),
                        cfg.get("repeat", "daily"), cfg.get("time", "09:00"),
                        json.dumps(cfg.get("weekdays", [])),
                        cfg.get("backfill_days", 1), cfg.get("cover_days", 0),
                        int(cfg.get("enabled", True)), cfg.get("remark", ""),
                        cfg.get("download_dir", ""), int(bool(cfg.get("download_dir_custom", False))), windows_json,
                        output_windows_json, cfg.get("path_template", ""), folders_json,
                        cfg.get("path_structure", ""), now, cfg["id"],
                    ),
                )
            else:
                windows_json = _indicator_windows_json(cfg.get("indicator_windows"))
                output_windows_json = _indicator_windows_json(cfg.get("output_windows"))
                folders_json = _path_folders_json(cfg.get("path_folders"))
                self._conn.execute(
                    """INSERT INTO task_configs
                        (id, name, login, indicators, repeat, time, weekdays,
                         backfill_days, cover_days, enabled, remark, download_dir, download_dir_custom,
                         indicator_windows, output_windows, path_template, path_folders, path_structure,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        cfg["id"], cfg["name"], cfg.get("login", ""),
                        json.dumps(cfg.get("indicators", [])),
                        cfg.get("repeat", "daily"), cfg.get("time", "09:00"),
                        json.dumps(cfg.get("weekdays", [])),
                        cfg.get("backfill_days", 1), cfg.get("cover_days", 0),
                        int(cfg.get("enabled", True)), cfg.get("remark", ""),
                        cfg.get("download_dir", ""), int(bool(cfg.get("download_dir_custom", False))), windows_json,
                        output_windows_json, cfg.get("path_template", ""), folders_json,
                        cfg.get("path_structure", ""), now, now,
                    ),
                )
            self._conn.commit()
        return cfg["id"]

    def delete_task_config(self, config_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM task_configs WHERE id = ?", (config_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── 工作流配置 CRUD ──

    def list_workflow_configs(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, name, type, needs_login, start_from, yaml_text, group_id, created_at, updated_at "
            "FROM workflow_configs ORDER BY name"
        ).fetchall()
        return [
            {
                "id": r["id"], "name": r["name"], "type": r["type"],
                "needs_login": bool(r["needs_login"]), "start_from": r["start_from"],
                "yaml_text": r["yaml_text"], "group_id": r["group_id"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def get_workflow_config(self, wf_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_configs WHERE id = ?", (wf_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"], "name": row["name"], "type": row["type"],
            "needs_login": bool(row["needs_login"]), "start_from": row["start_from"],
            "yaml_text": row["yaml_text"], "group_id": row["group_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def save_workflow_config(self, cfg: dict) -> str:
        import time
        now = time.time()
        existing = self.get_workflow_config(cfg["id"])
        with self._write_lock:
            if existing:
                self._conn.execute(
                    """UPDATE workflow_configs SET name=?, type=?, needs_login=?,
                       start_from=?, yaml_text=?, group_id=?, updated_at=? WHERE id=?""",
                    (cfg["name"], cfg.get("type", "indicator"),
                     int(cfg.get("needs_login", True)), cfg.get("start_from", "dashboard"),
                     cfg.get("yaml_text", ""), cfg.get("group_id", existing.get("group_id")), now, cfg["id"]),
                )
            else:
                self._conn.execute(
                    """INSERT INTO workflow_configs (id, name, type, needs_login,
                       start_from, yaml_text, group_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (cfg["id"], cfg["name"], cfg.get("type", "indicator"),
                     int(cfg.get("needs_login", True)), cfg.get("start_from", "dashboard"),
                     cfg.get("yaml_text", ""), cfg.get("group_id"), now, now),
                )
            self._conn.commit()
        return cfg["id"]

    def delete_workflow_config(self, wf_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM workflow_configs WHERE id = ?", (wf_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def migrate_workflows_from_yaml(self, workflows_dir: str = "workflows") -> int:
        """首次启动时将 YAML 文件迁移到 SQLite。返回迁移数量。"""
        from pathlib import Path as _Path

        import yaml as _yaml

        dir_path = _Path(workflows_dir)
        if not dir_path.is_absolute():
            dir_path = _Path.cwd() / dir_path
        if not dir_path.exists():
            return 0

        count = 0
        for f in sorted(dir_path.glob("*.yml")):
            wf_id = f.stem
            if self.get_workflow_config(wf_id):
                continue
            try:
                raw = f.read_text(encoding="utf-8")
                cfg = _yaml.safe_load(raw) or {}
                self.save_workflow_config({
                    "id": wf_id,
                    "name": cfg.get("name", wf_id),
                    "type": cfg.get("type", "indicator"),
                    "needs_login": cfg.get("needs_login", True),
                    "start_from": cfg.get("start_from", "dashboard"),
                    "yaml_text": raw,
                })
                count += 1
            except Exception:
                pass
        return count
    def create_task(self, task: TaskRecord) -> str:
        with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(task_id, name, status, output_dir, created_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    task.name,
                    task.status,
                    task.output_dir,
                    task.created_at,
                    task.finished_at,
                ),
            )
            self._conn.commit()
        return task.task_id

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return TaskRecord(
            task_id=row["task_id"],
            name=row["name"],
            status=row["status"],
            output_dir=row["output_dir"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )

    def update_task_status(self, task_id: str, status: str) -> None:
        with self._write_lock:
            self._conn.execute(
                "UPDATE tasks SET status = ?, "
                "finished_at = CASE WHEN ? IN ('completed', 'failed', 'partial') "
                "THEN datetime('now') ELSE finished_at END "
                "WHERE task_id = ?",
                (status, status, task_id),
            )
            self._conn.commit()

    def create_item(self, item: TaskItemRecord) -> None:
        with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO task_items "
                "(task_id, indicator_name, target_date, status, file_path, file_size, error_msg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.task_id,
                    item.indicator_name,
                    item.target_date,
                    item.status,
                    item.file_path,
                    item.file_size,
                    item.error_msg,
                ),
            )
            self._conn.commit()

    def update_item_status(
        self,
        task_id: str,
        indicator: str,
        target_date: str,
        status: str,
        **kwargs: object,
    ) -> None:
        fields = ["status = ?"]
        values: list[object] = [status]
        for col in ("file_path", "file_size", "error_msg"):
            if col in kwargs:
                fields.append(f"{col} = ?")
                values.append(kwargs[col])
        values.extend([task_id, indicator, target_date])
        with self._write_lock:
            self._conn.execute(
                f"UPDATE task_items SET {', '.join(fields)} "
                "WHERE task_id = ? AND indicator_name = ? AND target_date = ?",
                values,
            )
            self._conn.commit()

    def get_pending_items(self, task_id: str) -> list[TaskItemRecord]:
        rows = self._conn.execute(
            "SELECT * FROM task_items WHERE task_id = ? AND status IN ('pending', 'failed')",
            (task_id,),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def get_items(self, task_id: str) -> list[TaskItemRecord]:
        rows = self._conn.execute(
            "SELECT * FROM task_items WHERE task_id = ?", (task_id,)
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def list_tasks(self, limit: int = 50) -> list[TaskRecord]:
        """最近任务列表（按创建时间倒序），用于 REST/Agent 查询。"""
        rows = self._conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            TaskRecord(
                task_id=r["task_id"],
                name=r["name"],
                status=r["status"],
                output_dir=r["output_dir"],
                created_at=r["created_at"],
                finished_at=r["finished_at"],
            )
            for r in rows
        ]

    def get_success_dates(self, indicator: str) -> list[str]:
        """某指标已成功采集的去重日期（升序），用于缺口检测/补漏。"""
        rows = self._conn.execute(
            "SELECT DISTINCT target_date FROM task_items "
            "WHERE indicator_name = ? AND status = 'success' ORDER BY target_date",
            (indicator,),
        ).fetchall()
        return [r["target_date"] for r in rows]


    # ── 执行日志 CRUD ──

    def create_execution_log(self, record: ExecutionLogRecord) -> str:
        """创建一条执行日志记录。返回 exec_id。"""
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO execution_logs
                   (exec_id, name, type, target_id, status, reason,
                    detail_log, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.exec_id, record.name, record.type, record.target_id,
                    record.status, record.reason, record.detail_log,
                    record.created_at, record.updated_at,
                ),
            )
            self._conn.commit()
        return record.exec_id

    def update_execution_log(self, exec_id: str, **kwargs: object) -> bool:
        """更新执行日志字段（status, reason, detail_log 等）。"""
        allowed = {"status", "reason", "detail_log", "updated_at"}
        fields = []
        values: list[object] = []
        for k, v in kwargs.items():
            if k in allowed:
                fields.append(f"{k} = ?")
                values.append(v)
        if not fields:
            return False
        values.append(exec_id)
        with self._write_lock:
            cur = self._conn.execute(
                f"UPDATE execution_logs SET {', '.join(fields)} WHERE exec_id = ?",
                values,
            )
            self._conn.commit()
        return cur.rowcount > 0

    def append_execution_log_detail(self, exec_id: str, line: str) -> bool:
        """向 detail_log 追加一行文本（线程安全）。"""
        import time as _time
        ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"[{ts}] {line}\n"
        with self._write_lock:
            self._conn.execute(
                "UPDATE execution_logs SET detail_log = detail_log || ?, "
                "updated_at = ? WHERE exec_id = ?",
                (formatted, ts, exec_id),
            )
            self._conn.commit()
        return True

    def get_execution_log(self, exec_id: str) -> ExecutionLogRecord | None:
        row = self._conn.execute(
            "SELECT * FROM execution_logs WHERE exec_id = ?", (exec_id,)
        ).fetchone()
        if not row:
            return None
        return ExecutionLogRecord(
            exec_id=row["exec_id"], name=row["name"], type=row["type"],
            target_id=row["target_id"], status=row["status"],
            reason=row["reason"] or "", detail_log=row["detail_log"] or "",
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_execution_logs(
        self, page: int = 1, page_size: int = 20,
        log_type: str = "", status: str = "",
        start_time: str = "", end_time: str = "",
    ) -> list[ExecutionLogRecord]:
        """分页查询执行日志（按创建时间倒序），可按 type/status 筛选。"""
        conditions: list[str] = []
        params: list[object] = []
        if log_type:
            conditions.append("type = ?")
            params.append(log_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = max(0, (page - 1) * page_size)
        params.extend([page_size, offset])
        rows = self._conn.execute(
            f"SELECT * FROM execution_logs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [
            ExecutionLogRecord(
                exec_id=r["exec_id"], name=r["name"], type=r["type"],
                target_id=r["target_id"], status=r["status"],
                reason=r["reason"] or "", detail_log=r["detail_log"] or "",
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def count_execution_logs(
        self, log_type: str = "", status: str = "",
        start_time: str = "", end_time: str = "",
    ) -> int:
        """统计执行日志总数（支持筛选）。"""
        conditions: list[str] = []
        params: list[object] = []
        if log_type:
            conditions.append("type = ?")
            params.append(log_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS cnt FROM execution_logs {where}", params
        ).fetchone()
        return row["cnt"] if row else 0

    def delete_execution_log(self, exec_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM execution_logs WHERE exec_id = ?", (exec_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_execution_logs(self, exec_ids: list[str]) -> int:
        """批量删除执行日志，返回实际删掉的条数。"""
        ids = [item for item in dict.fromkeys(exec_ids) if item]
        if not ids:
            return 0
        with self._write_lock:
            cur = self._conn.execute(
                f"DELETE FROM execution_logs WHERE exec_id IN ({','.join('?' * len(ids))})", ids
            )
            self._conn.commit()
        return cur.rowcount

    def cleanup_old_execution_logs(self, retention_days: int = 30) -> int:
        """删除超过 retention_days 天的执行日志。返回删除数量。"""
        import time as _time
        cutoff = _time.strftime(
            "%Y-%m-%d %H:%M:%S",
            _time.localtime(_time.time() - retention_days * 86400),
        )
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM execution_logs WHERE created_at < ?", (cutoff,)
            )
            self._conn.commit()
        deleted = cur.rowcount
        if deleted:
            logger.info("清理了 {} 条过期执行日志（{} 天前）", deleted, retention_days)
        return deleted

    # ── 应用设置（key-value） ──

    def get_all_settings(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def save_settings(self, settings: dict[str, str]) -> None:
        """批量保存设置（覆盖已有 key，插入新 key）。"""
        with self._write_lock:
            for k, v in settings.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                    (k, str(v)),
                )
            self._conn.commit()

    def delete_setting(self, key: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            self._conn.commit()
        return cur.rowcount > 0

    # ── 录制工坊 CRUD ──

    def create_workshop(self, record: RecordWorkshopRecord) -> int:
        """创建录制工坊条目，返回自增 id。"""
        with self._write_lock:
            cur = self._conn.execute(
                """INSERT INTO record_workshops
                   (name, har_path, description, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (record.name, record.har_path, record.description,
                 record.status, record.created_at, record.updated_at),
            )
            self._conn.commit()
        return cur.lastrowid

    def update_workshop(self, wid: int, **kwargs: object) -> bool:
        """更新录制工坊字段。"""
        allowed = {"name", "har_path", "description", "status", "updated_at"}
        fields, values = [], []
        for k, v in kwargs.items():
            if k in allowed:
                fields.append(f"{k} = ?")
                values.append(v)
        if not fields:
            return False
        values.append(wid)
        with self._write_lock:
            cur = self._conn.execute(
                f"UPDATE record_workshops SET {', '.join(fields)} WHERE id = ?", values
            )
            self._conn.commit()
        return cur.rowcount > 0

    def get_workshop(self, wid: int) -> RecordWorkshopRecord | None:
        row = self._conn.execute(
            "SELECT * FROM record_workshops WHERE id = ?", (wid,)
        ).fetchone()
        if not row:
            return None
        return RecordWorkshopRecord(
            id=row["id"], name=row["name"], har_path=row["har_path"] or "",
            description=row["description"] or "", status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_workshops(self) -> list[RecordWorkshopRecord]:
        rows = self._conn.execute(
            "SELECT * FROM record_workshops ORDER BY created_at DESC"
        ).fetchall()
        return [
            RecordWorkshopRecord(
                id=r["id"], name=r["name"], har_path=r["har_path"] or "",
                description=r["description"] or "", status=r["status"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def delete_workshop(self, wid: int) -> bool:
        with self._write_lock:
            cur = self._conn.execute("DELETE FROM record_workshops WHERE id = ?", (wid,))
            self._conn.commit()
        return cur.rowcount > 0

    def _migrate_workshop_submissions_schema(self) -> None:
        """确保 workshop_submissions 表存在（兼容旧库）。"""
        try:
            self._conn.execute(
                "SELECT 1 FROM workshop_submissions LIMIT 1"
            ).fetchone()
        except Exception:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS workshop_submissions (
                    export_id     TEXT PRIMARY KEY,
                    filename      TEXT NOT NULL DEFAULT '',
                    local_path    TEXT NOT NULL DEFAULT '',
                    file_hash     TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL DEFAULT 'pending',
                    remote_url    TEXT NOT NULL DEFAULT '',
                    submitted_at  TEXT NOT NULL DEFAULT '',
                    error_msg     TEXT NOT NULL DEFAULT '',
                    updated_at    TEXT NOT NULL DEFAULT ''
                );
            """)
            self._conn.commit()

    def _migrate_delivery_outbox_schema(self) -> None:
        """确保产物发送 outbox 表存在（兼容旧库）。"""
        try:
            self._conn.execute("SELECT 1 FROM delivery_outbox LIMIT 1").fetchone()
        except Exception:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id       TEXT NOT NULL UNIQUE,
                    task_dir      TEXT NOT NULL DEFAULT '',
                    zip_path      TEXT NOT NULL DEFAULT '',
                    summary_json  TEXT NOT NULL DEFAULT '{}',
                    status        TEXT NOT NULL DEFAULT 'pending',
                    attempts      INTEGER NOT NULL DEFAULT 0,
                    zip_identity  TEXT NOT NULL DEFAULT '',
                    zip_size      INTEGER NOT NULL DEFAULT 0,
                    zip_sha256    TEXT NOT NULL DEFAULT '',
                    delivery_identity TEXT NOT NULL DEFAULT '',
                    remote_url    TEXT NOT NULL DEFAULT '',
                    file_sent     INTEGER NOT NULL DEFAULT 0,
                    delivery_note TEXT NOT NULL DEFAULT '',
                    last_error    TEXT NOT NULL DEFAULT '',
                    created_at    TEXT NOT NULL DEFAULT '',
                    updated_at    TEXT NOT NULL DEFAULT '',
                    delivered_at  TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_outbox_status
                ON delivery_outbox(status, updated_at);
            """)
            self._conn.commit()
        try:
            existing = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(delivery_outbox)").fetchall()
            }
        except Exception:
            return
        if "remote_url" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN remote_url TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
                logger.info("数据库迁移：delivery_outbox 添加列 remote_url")
            except Exception as exc:
                logger.warning("数据库迁移跳过 delivery_outbox.remote_url: {}", exc)
        if "file_sent" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN file_sent INTEGER NOT NULL DEFAULT 0"
                )
                self._conn.commit()
                logger.info("数据库迁移：delivery_outbox 添加列 file_sent")
            except Exception as exc:
                logger.warning("数据库迁移跳过 delivery_outbox.file_sent: {}", exc)
        if "zip_identity" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN zip_identity TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
                logger.info("数据库迁移：delivery_outbox 添加列 zip_identity")
            except Exception as exc:
                logger.warning("数据库迁移跳过 delivery_outbox.zip_identity: {}", exc)
        if "zip_size" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN zip_size INTEGER NOT NULL DEFAULT 0"
                )
                self._conn.commit()
                logger.info("数据库迁移：delivery_outbox 添加列 zip_size")
            except Exception as exc:
                logger.warning("数据库迁移跳过 delivery_outbox.zip_size: {}", exc)
        if "zip_sha256" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN zip_sha256 TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
                logger.info("数据库迁移：delivery_outbox 添加列 zip_sha256")
            except Exception as exc:
                logger.warning("数据库迁移跳过 delivery_outbox.zip_sha256: {}", exc)
        if "delivery_identity" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE delivery_outbox "
                    "ADD COLUMN delivery_identity TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
                logger.info("数据库迁移：delivery_outbox 添加列 delivery_identity")
            except Exception as exc:
                logger.warning("数据库迁移跳过 delivery_outbox.delivery_identity: {}", exc)
        if "delivery_note" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE delivery_outbox "
                    "ADD COLUMN delivery_note TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
                logger.info("数据库迁移：delivery_outbox 添加列 delivery_note")
            except Exception as exc:
                logger.warning("数据库迁移跳过 delivery_outbox.delivery_note: {}", exc)
        self._backfill_delivery_zip_metadata()

    def _backfill_delivery_zip_metadata(self) -> None:
        try:
            rows = self._conn.execute(
                """SELECT id, zip_path FROM delivery_outbox
                   WHERE zip_identity = '' OR zip_size = 0 OR zip_sha256 = ''"""
            ).fetchall()
        except Exception:
            return
        updated = 0
        with self._write_lock:
            for row in rows:
                identity, size, sha256 = self._delivery_zip_metadata(str(row["zip_path"]))
                if not identity:
                    continue
                self._conn.execute(
                    """UPDATE delivery_outbox
                       SET zip_identity = ?, zip_size = ?, zip_sha256 = ?
                       WHERE id = ?""",
                    (identity, size, sha256, row["id"]),
                )
                updated += 1
            if updated:
                self._conn.commit()
                logger.info("数据库迁移：delivery_outbox 回填 zip 元数据 {} 条", updated)

    def get_workshop_submission(self, export_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workshop_submissions WHERE export_id = ?", (export_id,)
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def get_workshop_submissions_map(self, export_ids: list[str]) -> dict[str, dict]:
        if not export_ids:
            return {}
        placeholders = ",".join("?" * len(export_ids))
        rows = self._conn.execute(
            f"SELECT * FROM workshop_submissions WHERE export_id IN ({placeholders})",
            export_ids,
        ).fetchall()
        return {r["export_id"]: dict(r) for r in rows}

    def upsert_workshop_submission(
        self,
        *,
        export_id: str,
        filename: str = "",
        local_path: str = "",
        file_hash: str = "",
        status: str = "pending",
        remote_url: str = "",
        submitted_at: str = "",
        error_msg: str = "",
        updated_at: str = "",
    ) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO workshop_submissions
                   (export_id, filename, local_path, file_hash, status,
                    remote_url, submitted_at, error_msg, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(export_id) DO UPDATE SET
                     filename=excluded.filename,
                     local_path=excluded.local_path,
                     file_hash=excluded.file_hash,
                     status=excluded.status,
                     remote_url=excluded.remote_url,
                     submitted_at=excluded.submitted_at,
                     error_msg=excluded.error_msg,
                     updated_at=excluded.updated_at""",
                (
                    export_id, filename, local_path, file_hash, status,
                    remote_url, submitted_at, error_msg, updated_at,
                ),
            )
            self._conn.commit()

    def delete_workshop_submission(self, export_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM workshop_submissions WHERE export_id = ?", (export_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── 任务产物发送 outbox ──

    def enqueue_delivery_outbox(
        self,
        *,
        task_id: str,
        task_dir: str,
        zip_path: str,
        summary: dict[str, object] | None = None,
        error_msg: str = "",
        remote_url: str = "",
        delivery_identity: str = "",
    ) -> int:
        """记录待补发的任务产物；同一 task_id 幂等覆盖路径和错误信息。"""
        import json
        from datetime import datetime

        now = datetime.now().isoformat(timespec="seconds")
        summary_json = json.dumps(summary or {}, ensure_ascii=False)
        zip_identity, zip_size, zip_sha256 = self._delivery_zip_metadata(zip_path)
        with self._write_lock:
            cur = self._conn.execute(
                """INSERT INTO delivery_outbox
                   (task_id, task_dir, zip_path, summary_json, status, attempts,
                    zip_identity, zip_size, zip_sha256,
                    delivery_identity, remote_url, file_sent,
                    delivery_note, last_error,
                    created_at, updated_at, delivered_at)
                   VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, 0, '', ?, ?, ?, '')
                   ON CONFLICT(task_id) DO UPDATE SET
                     task_dir=excluded.task_dir,
                     zip_path=excluded.zip_path,
                     zip_identity=excluded.zip_identity,
                     zip_size=excluded.zip_size,
                     zip_sha256=excluded.zip_sha256,
                     delivery_identity=excluded.delivery_identity,
                     summary_json=excluded.summary_json,
                     status='pending',
                     remote_url=CASE
                       WHEN excluded.remote_url != '' THEN excluded.remote_url
                       WHEN delivery_outbox.zip_path != excluded.zip_path THEN ''
                       WHEN delivery_outbox.zip_identity != excluded.zip_identity THEN ''
                       WHEN delivery_outbox.delivery_identity != excluded.delivery_identity
                       THEN ''
                       ELSE delivery_outbox.remote_url
                     END,
                     file_sent=CASE
                       WHEN delivery_outbox.zip_path = excluded.zip_path
                       AND delivery_outbox.zip_identity = excluded.zip_identity
                       AND delivery_outbox.delivery_identity = excluded.delivery_identity
                       THEN delivery_outbox.file_sent
                       ELSE 0
                     END,
                     delivery_note='',
                     last_error=excluded.last_error,
                     updated_at=excluded.updated_at,
                     delivered_at=''""",
                (
                    task_id,
                    task_dir,
                    zip_path,
                    summary_json,
                    zip_identity,
                    zip_size,
                    zip_sha256,
                    delivery_identity,
                    remote_url,
                    error_msg,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM delivery_outbox WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row["id"] if row else cur.lastrowid)

    @staticmethod
    def _delivery_zip_identity(zip_path: str) -> str:
        """轻量识别同一路径下是否还是同一个 zip 产物。"""
        identity, _size, _sha256 = TaskStateStore._delivery_zip_metadata(zip_path)
        return identity

    @staticmethod
    def _delivery_zip_metadata(zip_path: str) -> tuple[str, int, str]:
        import hashlib

        try:
            path = Path(zip_path).expanduser().resolve()
            stat = path.stat()
            if not path.is_file():
                return "", 0, ""
            digest = hashlib.sha256()
            with path.open("rb") as fp:
                for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return "", 0, ""
        return (
            f"{path}|{stat.st_size}|{stat.st_mtime_ns}",
            int(stat.st_size),
            digest.hexdigest(),
        )

    def list_delivery_outbox(
        self,
        *,
        status: str = "pending",
        limit: int = 50,
    ) -> list[dict]:
        """列出 outbox 记录，默认只返回待补发产物。"""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM delivery_outbox WHERE status = ? "
                "ORDER BY updated_at ASC, id ASC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM delivery_outbox ORDER BY updated_at ASC, id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._delivery_outbox_row_to_dict(row) for row in rows]

    def get_delivery_outbox(self, task_id: str) -> dict | None:
        """读取单条 outbox 记录，用于复用已上传链接或已发送附件状态。"""
        row = self._conn.execute(
            "SELECT * FROM delivery_outbox WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return self._delivery_outbox_row_to_dict(row) if row is not None else None

    @staticmethod
    def _delivery_outbox_row_to_dict(row) -> dict:
        import json

        item = dict(row)
        try:
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
        except Exception:
            item["summary"] = {}
        return item

    def get_delivery_outbox_summary(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM delivery_outbox GROUP BY status"
        ).fetchall()
        summary = {str(row["status"]): int(row["n"]) for row in rows}
        return {
            "pending": summary.get("pending", 0),
            "sending": summary.get("sending", 0),
            "sent": summary.get("sent", 0),
            "failed": summary.get("failed", 0),
            "total": sum(summary.values()),
        }

    def claim_delivery_outbox(self, task_id: str) -> bool:
        """原子认领一条待补发记录，避免并发补发重复发送。"""
        from datetime import datetime

        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock:
            cur = self._conn.execute(
                """UPDATE delivery_outbox
                   SET status='sending', last_error='发送中', updated_at=?
                   WHERE task_id=? AND status='pending'""",
                (now, task_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def mark_delivery_outbox_remote_url(self, task_id: str, remote_url: str) -> bool:
        """记录已上传成功的远程链接，后续补发可复用。"""
        from datetime import datetime

        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock:
            cur = self._conn.execute(
                """UPDATE delivery_outbox
                   SET remote_url=?, updated_at=?
                   WHERE task_id=?""",
                (remote_url, now, task_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def mark_delivery_outbox_file_sent(self, task_id: str) -> bool:
        """记录企业微信文件附件已发送，避免补发时重复发送 zip。"""
        from datetime import datetime

        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock:
            cur = self._conn.execute(
                """UPDATE delivery_outbox
                   SET file_sent=1, updated_at=?
                   WHERE task_id=?""",
                (now, task_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def reset_stale_delivery_outbox(self, *, older_than_sec: int = 600) -> int:
        """把长时间停留在 sending 的记录恢复为 pending，便于崩溃后补发。"""
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(seconds=older_than_sec)).isoformat(
            timespec="seconds"
        )
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock:
            cur = self._conn.execute(
                """UPDATE delivery_outbox
                   SET status='pending', last_error='上次发送中断，等待补发', updated_at=?
                   WHERE status='sending' AND updated_at < ?""",
                (now, cutoff),
            )
            self._conn.commit()
        return cur.rowcount

    def mark_delivery_outbox_delivered(
        self,
        task_id: str,
        delivery_note: str = "",
    ) -> bool:
        from datetime import datetime

        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock:
            cur = self._conn.execute(
                """UPDATE delivery_outbox
                   SET status='sent', updated_at=?, delivered_at=?,
                       last_error='', delivery_note=?
                   WHERE task_id=?""",
                (now, now, delivery_note, task_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def mark_delivery_outbox_failed(
        self,
        task_id: str,
        error_msg: str,
        *,
        status: str = "pending",
    ) -> bool:
        from datetime import datetime

        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock:
            cur = self._conn.execute(
                """UPDATE delivery_outbox
                   SET status=?, attempts=attempts + 1,
                       last_error=?, updated_at=?
                   WHERE task_id=?""",
                (status, error_msg, now, task_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def cleanup_delivery_outbox(self, *, status: str = "sent", limit: int = 200) -> int:
        """清理 outbox 历史记录，默认删除最早的已发送记录。"""
        with self._write_lock:
            cur = self._conn.execute(
                """DELETE FROM delivery_outbox
                   WHERE id IN (
                     SELECT id FROM delivery_outbox
                     WHERE status = ?
                     ORDER BY updated_at ASC, id ASC
                     LIMIT ?
                   )""",
                (status, max(1, limit)),
            )
            self._conn.commit()
        return cur.rowcount

    def requeue_delivery_outbox(self, *, status: str = "failed", limit: int = 200) -> int:
        """把失败记录恢复为 pending，便于配置修复或网络恢复后再次补发。"""
        from datetime import datetime

        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock:
            cur = self._conn.execute(
                """UPDATE delivery_outbox
                   SET status='pending', last_error='手动恢复，等待补发', updated_at=?
                   WHERE id IN (
                     SELECT id FROM delivery_outbox
                     WHERE status = ?
                     ORDER BY updated_at ASC, id ASC
                     LIMIT ?
                   )""",
                (now, status, max(1, limit)),
            )
            self._conn.commit()
        return cur.rowcount

    # ── 路由规则 CRUD ──

    def list_route_rules(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, domain_pattern, interface_name, expected_ssid, enabled "
            "FROM route_rules ORDER BY id"
        ).fetchall()
        return [
            {"id": r["id"], "domain_pattern": r["domain_pattern"],
             "interface_name": r["interface_name"],
             "expected_ssid": r["expected_ssid"],
             "enabled": bool(r["enabled"])}
            for r in rows
        ]

    def save_route_rules(self, rules: list[dict]) -> None:
        """原子替换所有路由规则。"""
        with self._write_lock:
            self._conn.execute("DELETE FROM route_rules")
            for r in rules:
                self._conn.execute(
                    "INSERT INTO route_rules "
                    "(domain_pattern, interface_name, expected_ssid, enabled) "
                    "VALUES (?, ?, ?, ?)",
                    (r.get("domain_pattern", ""), r.get("interface_name", ""),
                     r.get("expected_ssid", "") or r.get("ssid", ""),
                     int(r.get("enabled", True))),
                )
            self._conn.commit()

    def get_task_summary(self, task_id: str) -> dict[str, object]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM task_items WHERE task_id = ? GROUP BY status",
            (task_id,),
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        return {
            "task_id": task_id,
            "total": total,
            "success": by_status.get("success", 0),
            "failed": by_status.get("failed", 0),
            "pending": by_status.get("pending", 0),
            "by_status": by_status,
        }

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> TaskItemRecord:
        return TaskItemRecord(
            task_id=row["task_id"],
            indicator_name=row["indicator_name"],
            target_date=row["target_date"],
            status=row["status"],
            file_path=row["file_path"],
            file_size=row["file_size"],
            error_msg=row["error_msg"],
        )


# ── 辅助函数 ──

def _task_config_row(row: sqlite3.Row) -> dict:
    import json
    def _col(key: str, default: object = "") -> object:
        try:
            return row[key]
        except IndexError:
            return default
    return {
        "id": _col("id"), "name": _col("name"), "login": _col("login", ""),
        "indicators": json.loads(_col("indicators", "[]")),  # type: ignore[arg-type]
        "repeat": _col("repeat", "daily"), "time": _col("time", "09:00"),
        "weekdays": json.loads(_col("weekdays", "[]")),  # type: ignore[arg-type]
        "backfill_days": _col("backfill_days", 1), "cover_days": _col("cover_days", 0),
        "enabled": bool(_col("enabled", 1)), "remark": _col("remark", ""),
        "download_dir": _col("download_dir", ""),
        "download_dir_custom": bool(_col("download_dir_custom", 0)),
        "indicator_windows": _parse_indicator_windows(_col("indicator_windows", "")),
        "output_windows": _parse_indicator_windows(_col("output_windows", "")),
        "path_template": _col("path_template", ""),
        "path_folders": _parse_path_folders(_col("path_folders", "")),
        "path_structure": _col("path_structure", ""),
        "created_at": _col("created_at", 0), "updated_at": _col("updated_at", 0),
    }


def _indicator_windows_json(raw) -> str:
    import json
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict) or not raw:
        return ""
    return json.dumps(raw, ensure_ascii=False)


def _clean_path_folder(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name", "")).strip()
    if not name:
        return None
    children = [_clean_path_folder(child) for child in item.get("children", []) or []]
    return {
        "id": str(item.get("id", "") or ""),
        "name": name,
        "indicators": [str(x) for x in item.get("indicators", []) or [] if isinstance(x, str)],
        "children": [child for child in children if child],
    }


def _path_folders_json(raw) -> str:
    import json
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list) or not raw:
        return ""
    cleaned = [_clean_path_folder(item) for item in raw]
    cleaned = [item for item in cleaned if item]
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else ""


def _parse_path_folders(raw) -> list[dict]:
    import json
    if isinstance(raw, list):
        parsed = raw
    elif not raw:
        return []
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(parsed, list):
        return []
    cleaned = [_clean_path_folder(item) for item in parsed]
    return [item for item in cleaned if item]


def _parse_indicator_windows(raw) -> dict:
    import json
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
