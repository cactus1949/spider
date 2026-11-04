"""测试 SQLite 任务状态追踪。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.interfaces.storage import TaskItemRecord, TaskRecord


@pytest.fixture
def store(tmp_path: Path) -> TaskStateStore:
    s = TaskStateStore(tmp_path / "spider.db")
    yield s
    s.close()


@pytest.mark.unit
def test_create_and_update_task(store: TaskStateStore):
    store.create_task(
        TaskRecord("T1", "日清算采集", "running", "/out", created_at="2026-06-19T09:00:00")
    )
    store.update_task_status("T1", "completed")
    row = store._conn.execute("SELECT status, finished_at FROM tasks WHERE task_id='T1'").fetchone()
    assert row["status"] == "completed"
    assert row["finished_at"] is not None


@pytest.mark.unit
def test_item_lifecycle_and_summary(store: TaskStateStore):
    store.create_task(TaskRecord("T1", "n", "running", "/o", "2026-06-19T09:00:00"))
    for ind, date in [("日前出清", "20260618"), ("实时出清", "20260618")]:
        store.create_item(TaskItemRecord("T1", ind, date, "pending"))

    store.update_item_status(
        "T1", "日前出清", "20260618", "success", file_path="/o/a.xlsx", file_size=1024
    )
    store.update_item_status("T1", "实时出清", "20260618", "failed", error_msg="无数据")

    summary = store.get_task_summary("T1")
    assert summary["total"] == 2
    assert summary["success"] == 1
    assert summary["failed"] == 1


@pytest.mark.unit
def test_get_pending_items_includes_failed(store: TaskStateStore):
    store.create_task(TaskRecord("T1", "n", "running", "/o", "2026-06-19T09:00:00"))
    store.create_item(TaskItemRecord("T1", "a", "20260618", "success"))
    store.create_item(TaskItemRecord("T1", "b", "20260618", "failed"))
    store.create_item(TaskItemRecord("T1", "c", "20260618", "pending"))
    pending = store.get_pending_items("T1")
    indicators = {i.indicator_name for i in pending}
    assert indicators == {"b", "c"}  # 断点续采：failed + pending


@pytest.mark.unit
def test_wal_mode_enabled(tmp_path: Path):
    store = TaskStateStore(tmp_path / "wal.db", enable_wal=True)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    store.close()
    assert mode.lower() == "wal"


@pytest.mark.unit
def test_concurrent_writes_thread_safety(store: TaskStateStore):
    """多线程并发写操作不应导致 database is locked 或数据丢失。"""
    store.create_task(TaskRecord("T1", "并发测试", "running", "/o", "2026-06-23T09:00:00"))
    errors: list[Exception] = []
    n_items = 50

    def writer(indicator: str):
        try:
            store.create_item(TaskItemRecord("T1", indicator, "20260623", "pending"))
            store.update_item_status("T1", indicator, "20260623", "success")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"idx_{i}",)) for i in range(n_items)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"写入异常: {errors}"
    summary = store.get_task_summary("T1")
    assert summary["total"] == n_items
    assert summary["success"] == n_items


@pytest.mark.unit
def test_concurrent_task_writes(store: TaskStateStore):
    """多线程同时创建和更新任务记录。"""
    errors: list[Exception] = []
    n_tasks = 20

    def create_and_update(i: int):
        tid = f"T{i}"
        try:
            store.create_task(TaskRecord(tid, f"task_{i}", "running", "/o", "2026-06-23T09:00:00"))
            store.update_task_status(tid, "completed")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=create_and_update, args=(i,)) for i in range(n_tasks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"写入异常: {errors}"
    tasks = store.list_tasks(limit=100)
    assert len(tasks) == n_tasks
    assert all(t.status == "completed" for t in tasks)
    assert all(t.finished_at is not None for t in tasks)


@pytest.mark.unit
def test_route_rules_roundtrip_expected_ssid(store: TaskStateStore):
    store.save_route_rules([
        {
            "domain_pattern": "pm.gd.csg.cn",
            "interface_name": "UKey-WiFi",
            "expected_ssid": "CSG-UKEY",
            "enabled": True,
        }
    ])
    rules = store.list_route_rules()
    assert rules == [{
        "id": 1,
        "domain_pattern": "pm.gd.csg.cn",
        "interface_name": "UKey-WiFi",
        "expected_ssid": "CSG-UKEY",
        "enabled": True,
    }]


@pytest.mark.unit
def test_delivery_outbox_roundtrip(store: TaskStateStore):
    zpath = Path(store._db_path).parent / "result.zip"
    zpath.write_bytes(b"zip bytes")
    outbox_id = store.enqueue_delivery_outbox(
        task_id="T1",
        task_dir="/tmp/task",
        zip_path=str(zpath),
        summary={"total": 1, "success": 1},
        error_msg="send failed",
    )

    pending = store.list_delivery_outbox()
    assert outbox_id > 0
    assert len(pending) == 1
    assert pending[0]["task_id"] == "T1"
    assert pending[0]["status"] == "pending"
    assert pending[0]["remote_url"] == ""
    assert pending[0]["file_sent"] == 0
    assert pending[0]["zip_size"] == len(b"zip bytes")
    assert len(pending[0]["zip_sha256"]) == 64
    assert pending[0]["summary"]["success"] == 1

    assert (
        store.mark_delivery_outbox_remote_url("T1", "https://files.example.com/result.zip")
        is True
    )
    pending = store.list_delivery_outbox()
    assert pending[0]["remote_url"] == "https://files.example.com/result.zip"

    assert store.mark_delivery_outbox_file_sent("T1") is True
    pending = store.list_delivery_outbox()
    assert pending[0]["file_sent"] == 1

    assert store.mark_delivery_outbox_failed("T1", "still failed") is True
    pending = store.list_delivery_outbox()
    assert pending[0]["attempts"] == 1
    assert pending[0]["last_error"] == "still failed"

    assert store.claim_delivery_outbox("T1") is True
    assert store.claim_delivery_outbox("T1") is False
    summary = store.get_delivery_outbox_summary()
    assert summary["sending"] == 1

    store._conn.execute(
        "UPDATE delivery_outbox SET updated_at = ? WHERE task_id = ?",
        ("2000-01-01T00:00:00", "T1"),
    )
    store._conn.commit()
    assert store.reset_stale_delivery_outbox(older_than_sec=1) == 1
    assert store.list_delivery_outbox()[0]["status"] == "pending"

    assert store.mark_delivery_outbox_delivered("T1") is True
    sent = store.list_delivery_outbox(status="sent")
    assert sent[0]["status"] == "sent"
    assert sent[0]["delivered_at"]
    assert sent[0]["delivery_note"] == ""

    store.enqueue_delivery_outbox(
        task_id="T_NOTE",
        task_dir="/tmp/task",
        zip_path="/tmp/task/result.zip",
        summary={"total": 1},
        error_msg="waiting",
    )
    assert store.mark_delivery_outbox_delivered("T_NOTE", "企业微信群机器人；远程下载链接")
    note_sent = store.get_delivery_outbox("T_NOTE")
    assert note_sent is not None
    assert note_sent["delivery_note"] == "企业微信群机器人；远程下载链接"
    summary = store.get_delivery_outbox_summary()
    assert summary["sent"] == 2
    assert summary["total"] == 2

    assert store.cleanup_delivery_outbox(status="sent") == 2
    assert store.get_delivery_outbox_summary()["total"] == 0


@pytest.mark.unit
def test_delivery_outbox_requeues_failed_records(store: TaskStateStore):
    store.enqueue_delivery_outbox(
        task_id="T_FAILED",
        task_dir="/tmp/task",
        zip_path="/tmp/task/result.zip",
        summary={"total": 1},
        error_msg="send failed",
    )
    assert store.mark_delivery_outbox_failed("T_FAILED", "file missing", status="failed")
    assert store.get_delivery_outbox_summary()["failed"] == 1

    assert store.requeue_delivery_outbox(status="failed", limit=10) == 1

    pending = store.list_delivery_outbox(status="pending")
    assert pending[0]["task_id"] == "T_FAILED"
    assert pending[0]["last_error"] == "手动恢复，等待补发"
    assert store.get_delivery_outbox_summary()["pending"] == 1


@pytest.mark.unit
def test_delivery_outbox_reenqueue_preserves_delivery_progress(store: TaskStateStore):
    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir="/tmp/task",
        zip_path="/tmp/task/result.zip",
        summary={"total": 1},
        error_msg="waiting",
    )
    assert store.mark_delivery_outbox_remote_url(
        "T_PROGRESS",
        "https://files.example.com/result.zip",
    )
    assert store.mark_delivery_outbox_file_sent("T_PROGRESS")

    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir="/tmp/task",
        zip_path="/tmp/task/result.zip",
        summary={"total": 1, "success": 1},
        error_msg="waiting again",
    )

    item = store.get_delivery_outbox("T_PROGRESS")
    assert item is not None
    assert item["remote_url"] == "https://files.example.com/result.zip"
    assert item["file_sent"] == 1
    assert item["summary"]["success"] == 1


@pytest.mark.unit
def test_delivery_outbox_reenqueue_resets_progress_when_zip_changes(store: TaskStateStore):
    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir="/tmp/task",
        zip_path="/tmp/task/old.zip",
        summary={"total": 1},
        error_msg="waiting",
    )
    assert store.mark_delivery_outbox_remote_url(
        "T_PROGRESS",
        "https://files.example.com/old.zip",
    )
    assert store.mark_delivery_outbox_file_sent("T_PROGRESS")

    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir="/tmp/task",
        zip_path="/tmp/task/new.zip",
        summary={"total": 1, "success": 1},
        error_msg="waiting again",
    )

    item = store.get_delivery_outbox("T_PROGRESS")
    assert item is not None
    assert item["zip_path"] == "/tmp/task/new.zip"
    assert item["remote_url"] == ""
    assert item["file_sent"] == 0
    assert item["summary"]["success"] == 1


@pytest.mark.unit
def test_delivery_outbox_reenqueue_resets_progress_when_same_path_content_changes(
    store: TaskStateStore,
    tmp_path: Path,
):
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"old")
    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1},
        error_msg="waiting",
    )
    assert store.mark_delivery_outbox_remote_url(
        "T_PROGRESS",
        "https://files.example.com/old.zip",
    )
    assert store.mark_delivery_outbox_file_sent("T_PROGRESS")

    zip_path.write_bytes(b"new content")
    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1, "success": 1},
        error_msg="waiting again",
    )

    item = store.get_delivery_outbox("T_PROGRESS")
    assert item is not None
    assert item["zip_path"] == str(zip_path)
    assert item["remote_url"] == ""
    assert item["file_sent"] == 0
    assert item["summary"]["success"] == 1


@pytest.mark.unit
def test_delivery_outbox_reenqueue_resets_progress_when_delivery_identity_changes(
    store: TaskStateStore,
    tmp_path: Path,
):
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"same")
    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1},
        error_msg="waiting",
        delivery_identity="old-target",
    )
    assert store.mark_delivery_outbox_remote_url(
        "T_PROGRESS",
        "https://files.example.com/old.zip",
    )
    assert store.mark_delivery_outbox_file_sent("T_PROGRESS")

    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1, "success": 1},
        error_msg="waiting again",
        delivery_identity="new-target",
    )

    item = store.get_delivery_outbox("T_PROGRESS")
    assert item is not None
    assert item["delivery_identity"] == "new-target"
    assert item["remote_url"] == ""
    assert item["file_sent"] == 0


@pytest.mark.unit
def test_delivery_outbox_reenqueue_resets_progress_when_old_identity_missing(
    store: TaskStateStore,
    tmp_path: Path,
):
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"same")
    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1},
        error_msg="waiting",
    )
    assert store.mark_delivery_outbox_remote_url(
        "T_PROGRESS",
        "https://files.example.com/old.zip",
    )
    assert store.mark_delivery_outbox_file_sent("T_PROGRESS")

    store.enqueue_delivery_outbox(
        task_id="T_PROGRESS",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1, "success": 1},
        error_msg="waiting again",
        delivery_identity="new-target",
    )

    item = store.get_delivery_outbox("T_PROGRESS")
    assert item is not None
    assert item["delivery_identity"] == "new-target"
    assert item["remote_url"] == ""
    assert item["file_sent"] == 0


@pytest.mark.unit
def test_delivery_outbox_migrates_remote_url_column(tmp_path: Path):
    db_path = tmp_path / "old.db"
    zpath = tmp_path / "result.zip"
    zpath.write_bytes(b"old zip")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE delivery_outbox (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id       TEXT NOT NULL UNIQUE,
            task_dir      TEXT NOT NULL DEFAULT '',
            zip_path      TEXT NOT NULL DEFAULT '',
            summary_json  TEXT NOT NULL DEFAULT '{}',
            status        TEXT NOT NULL DEFAULT 'pending',
            attempts      INTEGER NOT NULL DEFAULT 0,
            last_error    TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL DEFAULT '',
            updated_at    TEXT NOT NULL DEFAULT '',
            delivered_at  TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.execute(
        """INSERT INTO delivery_outbox
           (task_id, task_dir, zip_path, summary_json, status, attempts,
            last_error, created_at, updated_at, delivered_at)
           VALUES (?, ?, ?, '{}', 'pending', 0, 'waiting', 't', 't', '')""",
        ("T_EXISTING", str(tmp_path), str(zpath)),
    )
    conn.commit()
    conn.close()

    store = TaskStateStore(db_path)
    try:
        columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(delivery_outbox)").fetchall()
        }
        assert "remote_url" in columns
        assert "file_sent" in columns
        assert "zip_identity" in columns
        assert "zip_size" in columns
        assert "zip_sha256" in columns
        assert "delivery_identity" in columns
        assert "delivery_note" in columns
        existing = store.get_delivery_outbox("T_EXISTING")
        assert existing is not None
        assert existing["zip_identity"]
        assert existing["zip_size"] == len(b"old zip")
        assert len(existing["zip_sha256"]) == 64
        outbox_id = store.enqueue_delivery_outbox(
            task_id="T_OLD",
            task_dir="/tmp/task",
            zip_path="/tmp/task/result.zip",
            summary={},
            error_msg="waiting",
        )
        assert outbox_id > 0
        assert store.list_delivery_outbox()[0]["remote_url"] == ""
        assert store.list_delivery_outbox()[0]["file_sent"] == 0
    finally:
        store.close()


@pytest.mark.unit
def test_task_config_path_template_and_folders_roundtrip(store: TaskStateStore):
    folders = [
        {
            "id": "f1",
            "name": "披露信息",
            "indicators": ["实时节点电价查询导出"],
            "children": [
                {"id": "f2", "name": "交易日历", "indicators": ["交易日历"], "children": []},
            ],
        },
        {"id": "f3", "name": "阳江", "indicators": ["场站-日前交易结果发电侧导出"], "children": []},
    ]
    store.save_task_config({
        "id": "T_PATH",
        "name": "路径任务",
        "path_template": "{SSID}/{YYYY-MM}/{YYYY-MM-DD}",
        "path_folders": folders,
    })

    loaded = store.get_task_config("T_PATH")
    assert loaded["path_template"] == "{SSID}/{YYYY-MM}/{YYYY-MM-DD}"
    assert loaded["path_folders"] == folders

    store.save_task_config({"id": "T_PATH", "name": "路径任务", "path_folders": []})
    assert store.get_task_config("T_PATH")["path_folders"] == []


@pytest.mark.unit
def test_task_config_output_windows_roundtrip(store: TaskStateStore):
    windows = {"现货::日前": {"start": -2, "end": -1}}
    store.save_task_config({"id": "T_OUT", "name": "导出范围", "output_windows": windows})
    assert store.get_task_config("T_OUT")["output_windows"] == windows

    store.save_task_config({"id": "T_OUT", "name": "导出范围", "output_windows": {}})
    assert store.get_task_config("T_OUT")["output_windows"] == {}
