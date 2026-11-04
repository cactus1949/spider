"""数据存储（链路3）：SQLite 状态追踪（数据落地见 core.pipeline.local_landing）。"""

from __future__ import annotations

from spider_gd.infrastructure.storage.database import TaskStateStore

__all__ = ["TaskStateStore"]
