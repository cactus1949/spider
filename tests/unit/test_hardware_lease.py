"""Shared OS lease and settled blocking-worker lifetime (no real hardware)."""

import asyncio
import contextvars
import os
import subprocess
import sys
import threading

import pytest


def lease_module():
    from spider_gd.infrastructure import hardware_lease

    return hardware_lease


def test_same_process_and_explicit_borrow():
    hw = lease_module()
    owner = hw.HardwareLease.try_acquire()
    assert owner is not None
    try:
        assert hw.HardwareLease.try_acquire() is None
        with hw.hardware_operation(owner) as borrowed:
            assert borrowed is owner
            with hw.hardware_operation(borrowed):
                assert hw.HardwareLease.try_acquire() is None
        assert hw.HardwareLease.try_acquire() is None
        with pytest.raises(hw.HardwareBusyError), hw.hardware_operation():
            pytest.fail("busy operation entered")
    finally:
        owner.close()
        owner.close()
    with pytest.raises(RuntimeError), hw.hardware_operation(owner):
        pytest.fail("closed lease borrowed")
    with hw.hardware_operation():
        pass


def test_two_process_channels_and_exit_release(tmp_path, monkeypatch):
    hw = lease_module()
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("HOME", str(tmp_path / "owner"))
    script = """
from spider_gd.infrastructure.hardware_lease import HardwareLease
import sys
lease = HardwareLease.try_acquire()
print('owned' if lease else 'busy', flush=True)
if lease:
    sys.stdin.readline()
"""
    env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path), SPIDER_GD_CHANNEL="stable")
    with hw.hardware_operation():
        result = subprocess.run(
            [sys.executable, "-c", script],
            input="\n",
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "busy"
    env.update(HOME=str(tmp_path / "other"), SPIDER_GD_CHANNEL="beta")
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout.readline().strip() == "owned"
        assert hw.HardwareLease.try_acquire() is None
        child.communicate("\n", timeout=10)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    with hw.hardware_operation():
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_error", [False, True])
async def test_repeated_cancellation_settles_worker_before_releasing(worker_error):
    hw = lease_module()
    entered = threading.Event()
    exit_barrier = threading.Event()

    def worker():
        entered.set()
        assert exit_barrier.wait(10)
        if worker_error:
            raise ValueError("eventual worker failure")
        return 42

    async def operation():
        with hw.hardware_operation():
            await hw.run_hardware_blocking(worker)

    task = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            assert hw.HardwareLease.try_acquire() is None
    finally:
        exit_barrier.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with hw.hardware_operation():
        assert await hw.run_hardware_blocking(lambda: 42) == 42


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_error", [False, True])
async def test_cancel_all_settles_real_worker_before_releasing(worker_error):
    hw = lease_module()
    entered = threading.Event()
    exit_barrier = threading.Event()
    exited = threading.Event()
    loop = asyncio.get_running_loop()
    unhandled = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    existing_tasks = asyncio.all_tasks()

    def worker():
        entered.set()
        try:
            assert exit_barrier.wait(10)
            if worker_error:
                raise ValueError("eventual worker failure")
            return 42
        finally:
            exited.set()

    async def operation():
        with hw.hardware_operation():
            await hw.run_hardware_blocking(worker)

    owner = asyncio.create_task(operation())
    cancelled_tasks = set()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for attempt in range(3):
            # Simulate shutdown: cancel the owner and all newly created nested tasks.
            tasks = asyncio.all_tasks() - existing_tasks
            cancelled_tasks.update(tasks)
            for task in tasks:
                task.cancel(f"cancel-all-{attempt}")
            done, _ = await asyncio.wait({owner}, timeout=0.03)
            assert not done, "owner released the lease while its worker was alive"
            assert not exited.is_set()
            assert hw.HardwareLease.try_acquire() is None
        exit_barrier.set()
        with pytest.raises(asyncio.CancelledError, match="cancel-all-0"):
            await owner
        assert exited.is_set()
        with hw.hardware_operation():
            pass
        assert unhandled == []
    finally:
        exit_barrier.set()
        await asyncio.gather(owner, *cancelled_tasks, return_exceptions=True)
        await asyncio.to_thread(exited.wait, 5)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_error", [False, True])
async def test_unprotected_worker_preserves_context_result_and_exception(worker_error):
    hw = lease_module()
    context = contextvars.ContextVar("hardware-context", default="missing")
    token = context.set("owner-context")
    result = object()
    error = ValueError("real worker failure")

    def worker(value, *, expected_context):
        assert context.get() == expected_context
        context.set("worker-context")
        if worker_error:
            raise error
        return value

    try:
        if worker_error:
            with pytest.raises(ValueError, match="real worker failure") as raised:
                await hw.run_hardware_blocking(worker, result, expected_context="owner-context")
            assert raised.value is error
        else:
            assert (
                await hw.run_hardware_blocking(worker, result, expected_context="owner-context")
                is result
            )
        assert context.get() == "owner-context"
    finally:
        context.reset(token)


class NativeFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeKernel32:
    def __init__(self, wait=0, release=True, create=True):
        self.calls = []
        self.CreateMutexW = NativeFunction(
            lambda *args: self.record("create", args, 123 if create else 0)
        )
        self.WaitForSingleObject = NativeFunction(lambda *args: self.record("wait", args, wait))
        self.ReleaseMutex = NativeFunction(lambda *args: self.record("release", args, release))
        self.CloseHandle = NativeFunction(lambda *args: self.record("close", args, True))

    def record(self, name, args, result):
        self.calls.append((name, threading.get_ident(), args))
        return result


@pytest.mark.parametrize("wait", [0, 0x80, 0x102, 0xFFFFFFFF, 7])
def test_fake_windows_affinity_acquired_busy_abandoned_error(monkeypatch, wait):
    hw = lease_module()
    native = FakeKernel32(wait)
    monkeypatch.setattr(hw, "_WINDOWS", True)
    monkeypatch.setattr(hw, "_load_kernel32", lambda: native)
    if wait not in (0, 0x80, 0x102):
        with pytest.raises(OSError, match="failed"):
            hw.HardwareLease.try_acquire()
    else:
        lease = hw.HardwareLease.try_acquire()
        if wait == 0x102:
            assert lease is None
        else:
            assert lease is not None
            assert [c[0] for c in native.calls] == ["create", "wait"]
            lease.close()
            lease.close()
    names = [c[0] for c in native.calls]
    assert names == (
        ["create", "wait", "release", "close"] if wait in (0, 0x80) else ["create", "wait", "close"]
    )
    assert len({c[1] for c in native.calls}) == 1
    assert native.calls[0][1] != threading.get_ident()
    assert native.calls[0][2] == (None, False, "Global\\SpiderGD.SharedHardware")
    assert native.calls[1][2] == (123, 0)


def test_fake_windows_release_and_create_failures(monkeypatch):
    hw = lease_module()
    monkeypatch.setattr(hw, "_WINDOWS", True)
    native = FakeKernel32(release=False)
    monkeypatch.setattr(hw, "_load_kernel32", lambda: native)
    lease = hw.HardwareLease.try_acquire()
    with pytest.raises(OSError, match="failed"):
        lease.close()
    assert native.calls[-1][0] == "close"
    native = FakeKernel32(create=False)
    monkeypatch.setattr(hw, "_load_kernel32", lambda: native)
    with pytest.raises(OSError, match="failed"):
        hw.HardwareLease.try_acquire()
    assert [c[0] for c in native.calls] == ["create"]


def test_fake_windows_release_exception_still_closes(monkeypatch):
    hw = lease_module()
    native = FakeKernel32()

    def release(handle):
        native.record("release", (handle,), False)
        raise OSError("native release failed")

    native.ReleaseMutex = NativeFunction(release)
    monkeypatch.setattr(hw, "_WINDOWS", True)
    monkeypatch.setattr(hw, "_load_kernel32", lambda: native)
    lease = hw.HardwareLease.try_acquire()
    with pytest.raises(OSError, match="native release failed"):
        lease.close()
    assert native.calls[-1][0] == "close"


def test_fake_windows_native_signatures(monkeypatch):
    import ctypes
    from ctypes import wintypes

    hw = lease_module()
    native = FakeKernel32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error: native, raising=False)
    assert hw._load_kernel32() is native
    assert native.CreateMutexW.argtypes == [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    assert native.CreateMutexW.restype is wintypes.HANDLE
    assert native.WaitForSingleObject.argtypes == [wintypes.HANDLE, wintypes.DWORD]
    assert native.WaitForSingleObject.restype is wintypes.DWORD
    assert native.ReleaseMutex.argtypes == [wintypes.HANDLE]
    assert native.ReleaseMutex.restype is wintypes.BOOL
    assert native.CloseHandle.argtypes == [wintypes.HANDLE]
    assert native.CloseHandle.restype is wintypes.BOOL


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX os.fork")
def test_fork_cannot_borrow_parent_lease():
    hw = lease_module()
    with hw.hardware_operation() as owner:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                with hw.hardware_operation(owner):
                    os.write(write_fd, b"invalid")
            except RuntimeError:
                os.write(write_fd, b"rejected")
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            assert os.read(read_fd, 32) == b"rejected"
            assert os.waitpid(pid, 0)[1] == 0
            assert hw.HardwareLease.try_acquire() is None
        finally:
            os.close(read_fd)
