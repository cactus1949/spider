"""Channel-independent, process-wide hardware ownership and settled workers."""

from __future__ import annotations

import asyncio
import contextvars
import ctypes
import itertools
import os
import sys
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from functools import partial
from typing import Callable

_WINDOWS = sys.platform == "win32"
_MUTEX_NAME = "Global\\SpiderGD.SharedHardware"
_QUEUE_LOCK = threading.Lock()
_QUEUE_SEQ = itertools.count()
_QUEUE_WAITERS: list[tuple[int, int, str]] = []


class HardwareBusyError(RuntimeError):
    """Another installation or operation owns the shared hardware."""


def _load_kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _native_error(operation):
    code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
    return OSError(code, f"{operation} failed")


class HardwareLease:
    """An acquired OS lock. Borrow only explicitly; closing is idempotent."""

    def __init__(self):
        self._pid = os.getpid()
        self._active = False
        self._fd = None
        self._thread = None
        self._release = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._close_lock = threading.Lock()

    @classmethod
    def try_acquire(cls) -> HardwareLease | None:
        lease = cls()
        if _WINDOWS:
            lease._thread = threading.Thread(
                target=lease._own_windows_mutex,
                name="shared-hardware-lease",
                daemon=True,
            )
            lease._thread.start()
            lease._ready.wait()
            if not lease._active:
                lease._thread.join()
                if lease._error is not None:
                    raise lease._error
                return None
        else:
            import fcntl

            path = f"/tmp/spider-gd-shared-hardware-{os.getuid()}.lock"
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return None
            except BaseException:
                os.close(fd)
                raise
            lease._fd = fd
            lease._active = True
        return lease

    def _own_windows_mutex(self):
        kernel = None
        handle = None
        acquired = False
        try:
            kernel = _load_kernel32()
            handle = kernel.CreateMutexW(None, False, _MUTEX_NAME)
            if not handle:
                raise _native_error("CreateMutexW")
            result = kernel.WaitForSingleObject(handle, 0)
            if result in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
                acquired = self._active = True
                self._ready.set()
                self._release.wait()
            elif result != 0x102:  # WAIT_TIMEOUT
                raise _native_error(f"WaitForSingleObject ({result:#x})")
        except BaseException as exc:
            self._error = exc
        finally:
            try:
                if acquired and not kernel.ReleaseMutex(handle):
                    self._error = _native_error("ReleaseMutex")
            except BaseException as exc:
                self._error = exc
            finally:
                try:
                    if handle and not kernel.CloseHandle(handle) and self._error is None:
                        self._error = _native_error("CloseHandle")
                except BaseException as exc:
                    if self._error is None:
                        self._error = exc
                finally:
                    self._ready.set()

    def _validate(self):
        if self._pid != os.getpid() or not self._active:
            raise RuntimeError("硬件租约已关闭或不属于当前进程")

    def close(self) -> None:
        with self._close_lock:
            if not self._active:
                return
            self._validate()
            self._active = False
            if self._thread is not None:
                self._release.set()
                self._thread.join()
                if self._error is not None:
                    raise self._error
            else:
                # Closing, not unlinking, releases this open-file-description lock.
                os.close(self._fd)
                self._fd = None


@contextmanager
def hardware_operation(lease: HardwareLease | None = None):
    owner = lease is None
    if owner:
        lease = HardwareLease.try_acquire()
        if lease is None:
            raise HardwareBusyError(
                "另一个安装或任务正在使用共享硬件（证书、浏览器或网络），请稍后重试"
            )
    if not isinstance(lease, HardwareLease):
        raise RuntimeError("无效的硬件租约")
    lease._validate()
    try:
        yield lease
    finally:
        if owner:
            lease.close()


async def acquire_hardware_lease_async(
    *,
    priority: int = 1,
    timeout_seconds: float = 1800.0,
    poll_interval: float = 0.4,
    on_wait: Callable[[int], None] | None = None,
) -> HardwareLease | None:
    """按优先级单轨排队获取共享硬件租约（0=手动优先，1=自动定时避让排队）。

    硬件空闲时 0ms 直接返回；占用时循环等待并在释放后按优先级顺序接棒，
    避免手动重采与全自动任务并发冲突或误报失败。
    """
    seq = next(_QUEUE_SEQ)
    token = f"w-{seq}"
    entry = (int(priority), seq, token)
    with _QUEUE_LOCK:
        _QUEUE_WAITERS.append(entry)
        _QUEUE_WAITERS.sort()

    notified = False
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    try:
        while True:
            with _QUEUE_LOCK:
                _QUEUE_WAITERS.sort()
                pos = next((idx for idx, item in enumerate(_QUEUE_WAITERS) if item[2] == token), 0)
            if pos == 0:
                lease = HardwareLease.try_acquire()
                if lease is not None:
                    return lease
            if not notified and on_wait is not None:
                notified = True
                try:
                    on_wait(pos + 1)
                except Exception:
                    pass
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(max(0.05, float(poll_interval)))
    finally:
        with _QUEUE_LOCK:
            try:
                _QUEUE_WAITERS.remove(entry)
            except ValueError:
                pass


async def run_hardware_blocking(func, *args, **kwargs):
    """Never return on cancellation while the actual blocking worker is alive."""
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    worker = loop.run_in_executor(None, context.run, partial(func, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # Retrieve any eventual exception without replacing caller cancellation.
        worker.exception()
        raise
