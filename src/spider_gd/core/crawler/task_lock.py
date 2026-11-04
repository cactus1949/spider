"""任务级文件锁：防止同名采集任务并发重叠（定时 + 手动 / 两次定时）。

跨进程安全：用 ``O_CREAT | O_EXCL`` 原子创建锁文件。锁文件记录 pid 与时间，
支持过期回收（默认 6 小时），避免崩溃后遗留死锁。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from loguru import logger


class TaskLockedError(RuntimeError):
    """同名任务已在运行，拒绝重复启动。"""


class TaskLock:
    def __init__(
        self, lock_dir: str | Path, task_name: str, *, stale_after_sec: int = 6 * 3600
    ) -> None:
        safe = "".join(c if c.isalnum() else "_" for c in task_name)
        self._path = Path(lock_dir) / f"{safe}.lock"
        self._stale_after = stale_after_sec
        self._acquired = False

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self._path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age > self._stale_after

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._is_stale():
            logger.warning("发现过期任务锁，回收：{}", self._path)
            self._path.unlink(missing_ok=True)
        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise TaskLockedError(f"任务已在运行（锁存在）：{self._path}") from exc
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {time.time()}")
        self._acquired = True
        logger.debug("获取任务锁：{}", self._path)

    def release(self) -> None:
        if self._acquired:
            self._path.unlink(missing_ok=True)
            self._acquired = False
            logger.debug("释放任务锁：{}", self._path)

    def __enter__(self) -> TaskLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
