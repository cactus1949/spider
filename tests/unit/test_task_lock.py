"""测试任务级文件锁：互斥、释放、过期回收。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from spider_gd.core.crawler.task_lock import TaskLock, TaskLockedError


@pytest.mark.unit
def test_lock_blocks_second_acquire(tmp_path: Path):
    held = TaskLock(tmp_path, "日清算")
    held.acquire()
    try:
        with pytest.raises(TaskLockedError):
            TaskLock(tmp_path, "日清算").acquire()
    finally:
        held.release()


@pytest.mark.unit
def test_lock_released_after_context(tmp_path: Path):
    with TaskLock(tmp_path, "日清算"):
        pass
    # 释放后可再次获取
    with TaskLock(tmp_path, "日清算"):
        pass


@pytest.mark.unit
def test_different_tasks_independent(tmp_path: Path):
    with TaskLock(tmp_path, "任务A"), TaskLock(tmp_path, "任务B"):
        pass  # 不同任务名互不阻塞


@pytest.mark.unit
def test_stale_lock_reclaimed(tmp_path: Path):
    lock = TaskLock(tmp_path, "日清算", stale_after_sec=0)
    lock.acquire()
    time.sleep(0.01)
    # 过期阈值为 0，新锁应回收旧锁
    other = TaskLock(tmp_path, "日清算", stale_after_sec=0)
    other.acquire()
    other.release()
