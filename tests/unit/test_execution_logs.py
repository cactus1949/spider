"""测试执行日志 CRUD（数据库层）。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.interfaces.storage import ExecutionLogRecord


@pytest.fixture
def store(tmp_path: Path) -> TaskStateStore:
    s = TaskStateStore(tmp_path / "spider.db")
    yield s
    s.close()


def _make_record(name="test", typ="workflow", target="test_wf",
                 status="running", reason="", detail=""):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    return ExecutionLogRecord(
        exec_id=f"{typ[:3]}_{uuid.uuid4().hex[:12]}",
        name=name, type=typ, target_id=target,
        status=status, reason=reason, detail_log=detail,
        created_at=ts, updated_at=ts,
    )


@pytest.mark.unit
class TestExecutionLogCRUD:
    """执行日志 CRUD 完整测试。"""

    def test_create_and_get(self, store: TaskStateStore):
        rec = _make_record(name="日清算概览", typ="workflow")
        eid = store.create_execution_log(rec)
        assert eid == rec.exec_id

        got = store.get_execution_log(eid)
        assert got is not None
        assert got.name == "日清算概览"
        assert got.type == "workflow"
        assert got.status == "running"

    def test_append_detail(self, store: TaskStateStore):
        rec = _make_record()
        eid = store.create_execution_log(rec)

        store.append_execution_log_detail(eid, "▶ 浏览器启动")
        store.append_execution_log_detail(eid, "⏳ 步骤 1/3: click")
        store.append_execution_log_detail(eid, "✅ 步骤 1/3 完成")

        got = store.get_execution_log(eid)
        assert "浏览器启动" in got.detail_log
        assert "步骤 1/3" in got.detail_log

    def test_update_status(self, store: TaskStateStore):
        rec = _make_record()
        eid = store.create_execution_log(rec)

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        store.update_execution_log(eid, status="success", reason="", updated_at=ts)

        got = store.get_execution_log(eid)
        assert got.status == "success"

    def test_update_with_reason(self, store: TaskStateStore):
        rec = _make_record(status="running")
        eid = store.create_execution_log(rec)

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        store.update_execution_log(
            eid, status="failed", reason="元素未找到", updated_at=ts
        )

        got = store.get_execution_log(eid)
        assert got.status == "failed"
        assert got.reason == "元素未找到"

    def test_list_and_count(self, store: TaskStateStore):
        # 创建 3 条不同类型的记录
        for i, (name, typ) in enumerate([
            ("指标A", "workflow"),
            ("任务B", "scheduled_auto"),
            ("任务C", "scheduled_manual"),
        ]):
            rec = _make_record(name=name, typ=typ, target=f"t{i}")
            store.create_execution_log(rec)

        # 全量
        assert store.count_execution_logs() == 3

        # 按类型筛选
        assert store.count_execution_logs(log_type="workflow") == 1
        assert store.count_execution_logs(log_type="scheduled_auto") == 1

    def test_list_pagination(self, store: TaskStateStore):
        for i in range(5):
            rec = _make_record(name=f"log_{i}", typ="workflow", target=f"t{i}")
            store.create_execution_log(rec)

        page1 = store.list_execution_logs(page=1, page_size=2)
        assert len(page1) == 2

        page2 = store.list_execution_logs(page=2, page_size=2)
        assert len(page2) == 2

        page3 = store.list_execution_logs(page=3, page_size=2)
        assert len(page3) == 1

    def test_filter_by_status(self, store: TaskStateStore):
        rec1 = _make_record(name="ok1", status="success")
        store.create_execution_log(rec1)
        rec2 = _make_record(name="fail1", status="failed", reason="超时")
        store.create_execution_log(rec2)

        assert store.count_execution_logs(status="success") == 1
        assert store.count_execution_logs(status="failed") == 1

        items = store.list_execution_logs(status="failed")
        assert items[0].reason == "超时"

    def test_delete(self, store: TaskStateStore):
        rec = _make_record()
        eid = store.create_execution_log(rec)
        assert store.count_execution_logs() == 1

        ok = store.delete_execution_log(eid)
        assert ok
        assert store.count_execution_logs() == 0

        # 删除不存在的应返回 False
        assert not store.delete_execution_log("nonexistent")

    def test_cleanup_old(self, store: TaskStateStore):
        # 创建一条旧日志（时间设为 31 天前）
        old_ts = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() - 31 * 86400),
        )
        rec = ExecutionLogRecord(
            exec_id=f"old_{uuid.uuid4().hex[:12]}",
            name="旧日志", type="workflow", target_id="x",
            status="success", created_at=old_ts, updated_at=old_ts,
        )
        store.create_execution_log(rec)

        # 创建一条新日志
        new_rec = _make_record(name="新日志")
        store.create_execution_log(new_rec)

        # 清理 30 天前
        deleted = store.cleanup_old_execution_logs(retention_days=30)
        assert deleted == 1
        assert store.count_execution_logs() == 1

        remaining = store.get_execution_log(new_rec.exec_id)
        assert remaining is not None
        assert remaining.name == "新日志"

    def test_get_nonexistent_returns_none(self, store: TaskStateStore):
        assert store.get_execution_log("no_such_id") is None

    def test_update_nonexistent_no_error(self, store: TaskStateStore):
        ok = store.update_execution_log("no_such", status="success")
        assert not ok  # no fields updated


@pytest.mark.unit
class TestExecutionLogSchemaMigration:
    """验证 schema 迁移正确运行。"""

    def test_migration_adds_missing_column(self, tmp_path: Path):
        import sqlite3
        db_path = tmp_path / "old.db"

        # 模拟旧库：task_configs 缺少 download_dir 列
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_configs (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                login TEXT DEFAULT '', indicators TEXT DEFAULT '[]',
                repeat TEXT DEFAULT 'daily', time TEXT DEFAULT '09:00',
                weekdays TEXT DEFAULT '[]',
                backfill_days INTEGER DEFAULT 7, cover_days INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1, remark TEXT DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO task_configs (id, name, created_at, updated_at) "
            "VALUES ('test', 'Test', 1.0, 1.0)"
        )
        conn.commit()
        conn.close()

        # 打开 store，应自动迁移
        s = TaskStateStore(str(db_path))
        configs = s.list_task_configs()
        assert len(configs) == 1
        assert configs[0].get("download_dir") == ""
        s.close()

        # 验证列确实被添加
        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        cols = {
            r["name"]
            for r in conn2.execute("PRAGMA table_info(task_configs)").fetchall()
        }
        assert "download_dir" in cols
        conn2.close()
