"""存储链路接口契约（链路3）。

遵循 SQLite 与 Excel 职责分离：
- :class:`ITaskStateStore` 仅做任务状态追踪（SQLite）。
- :class:`ILocalLanding` 负责实际数据文件落地、台账与 zip。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class TaskRecord:
    task_id: str
    name: str
    status: str  # pending/running/completed/failed
    output_dir: str
    created_at: str
    finished_at: str | None = None


@dataclass
class TaskItemRecord:
    task_id: str
    indicator_name: str
    target_date: str
    status: str  # pending/success/failed/skipped
    file_path: str | None = None
    file_size: int = 0
    error_msg: str | None = None


@dataclass
class ExecutionLogRecord:
    """一次执行（指标播放/定时任务）的完整日志记录。"""
    exec_id: str
    name: str
    type: str  # 'workflow' | 'scheduled_auto' | 'scheduled_manual'
    target_id: str  # workflow id 或 task config id
    status: str  # 'running' | 'success' | 'failed'
    reason: str = ""
    detail_log: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class RecordWorkshopRecord:
    """录制工坊条目（HAR 录制 + 需求描述）。"""
    id: int = 0
    name: str = ""
    har_path: str = ""
    description: str = ""
    status: str = "draft"  # 'draft' | 'submitted'
    created_at: str = ""
    updated_at: str = ""


class ITaskStateStore(Protocol):
    """任务状态追踪（SQLite，仅状态不存数据）。"""

    def create_task(self, task: TaskRecord) -> str: ...

    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def update_task_status(self, task_id: str, status: str) -> None: ...

    def create_item(self, item: TaskItemRecord) -> None: ...

    def update_item_status(
        self, task_id: str, indicator: str, target_date: str, status: str, **kwargs: object
    ) -> None: ...

    def get_pending_items(self, task_id: str) -> list[TaskItemRecord]: ...

    def get_items(self, task_id: str) -> list[TaskItemRecord]: ...

    def list_tasks(self, limit: int = 50) -> list[TaskRecord]: ...

    def get_success_dates(self, indicator: str) -> list[str]: ...

    def get_task_summary(self, task_id: str) -> dict[str, object]: ...


class ILocalLanding(Protocol):
    """本地文件落地管理。"""

    def create_task_dir(self, task_name: str, exec_time: str) -> str: ...

    def save_data_file(self, task_dir: str, indicator: str, date: str, data: bytes) -> str: ...

    def generate_ledger(self, task_dir: str, items: list[TaskItemRecord]) -> str: ...

    def compress_task(self, task_dir: str) -> str | None: ...
