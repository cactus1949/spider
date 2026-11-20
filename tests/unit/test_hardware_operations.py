"""Public hardware callers: real orchestration, fake Chrome/network/native ports."""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from task_runner import TaskRunner
from tests.unit.test_data_upload import FakeWifi, MemorySettings, _profile_payload
from tests.unit.test_station_execution import _StationContext, _StationPage

from spider_gd.core.crawler.shared_browser import SharedBrowser
from spider_gd.gateway.data_upload_flow import run_test_wifi, upload_selected_paths
from spider_gd.gateway.data_uploader import run_upload
from spider_gd.gateway.upload_profiles import UploadProfile
from spider_gd.infrastructure.hardware_lease import (
    HardwareBusyError,
    HardwareLease,
    hardware_operation,
    run_hardware_blocking,
)
from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.infrastructure.windows.cert_dialog import CertDialogResult, CertificateWatch


@pytest.fixture(autouse=True)
def _isolated_hardware_environment(tmp_path, monkeypatch):
    from spider_gd.config.delivery_env import DELIVERY_ENV_KEYS

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SPIDER_GD_HOME", str(tmp_path))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "stable")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    for key in (*DELIVERY_ENV_KEYS, "SPIDER_GD_DELIVERY_ENV_FILE", "SPIDER_BROWSER_CONFIG"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def store(tmp_path):
    state = TaskStateStore(tmp_path / "spider.db")
    yield state
    state.close()


def browser_port(monkeypatch):
    session = SharedBrowser()
    page = _StationPage()
    context = _StationContext(page)
    calls = []

    async def connect():
        calls.append("connect")
        session._browser = SimpleNamespace(is_connected=lambda: True)
        session._context = context
        session._bind_page(page)

    async def pages():
        return []

    monkeypatch.setattr(session, "ensure_connected", connect)
    monkeypatch.setattr(session, "list_pages", pages)
    return session, calls


@pytest.mark.asyncio
async def test_real_browser_busy_before_connect_and_borrow_through_cleanup(monkeypatch):
    session, calls = browser_port(monkeypatch)
    with hardware_operation() as owner:
        with pytest.raises(HardwareBusyError):
            async with session.execution("busy"):
                pytest.fail("busy browser entered")
        assert calls == []
        async with session.execution("owner", hardware_lease=owner):
            assert HardwareLease.try_acquire() is None
            with pytest.raises(RuntimeError, match="占用"):
                async with session.execution("same-browser", hardware_lease=owner):
                    pytest.fail("browser-local lock allowed concurrent execution")
        assert HardwareLease.try_acquire() is None
    async with session.execution("independent"):
        assert HardwareLease.try_acquire() is None
    with hardware_operation():
        pass


@pytest.mark.asyncio
async def test_task_runner_public_run_borrows_real_browser_lease(monkeypatch, tmp_path):
    session, calls = browser_port(monkeypatch)
    runner = TaskRunner({"name": "empty", "indicators": []}, download_dir=str(tmp_path))
    events = []
    with hardware_operation() as owner:
        with pytest.raises(HardwareBusyError):
            await runner.run(events.append, session=session)
        assert calls == []
        await runner.run(events.append, session=session, hardware_lease=owner)
        assert any(item["event"] == "task_done" for item in events)
        assert HardwareLease.try_acquire() is None


@pytest.mark.asyncio
async def test_manual_upload_and_wifi_probe_busy_without_touching_network(tmp_path, store):
    sample = tmp_path / "sample.txt"
    sample.write_text("safe")
    profile = UploadProfile("up_test", "test", "https://example.test/upload", wifi_ssid="office")
    wifi = FakeWifi("original")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200))
    ) as client:
        with hardware_operation() as owner:
            with pytest.raises(HardwareBusyError):
                await upload_selected_paths(store, profile, [str(sample)], wifi=wifi, client=client)
            with pytest.raises(HardwareBusyError):
                run_test_wifi(MemorySettings(), {"profile": _profile_payload()}, wifi)
            assert wifi.calls == []
            result = await upload_selected_paths(
                store, profile, [str(sample)], wifi=wifi, client=client, hardware_lease=owner
            )
            assert result.ok
            assert wifi.current == "original"
            assert HardwareLease.try_acquire() is None
            result = await run_upload(profile, [sample], switch_wifi=False, client=client)
            assert result.ok


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["switch", "http", "error"])
async def test_independent_upload_cancellation_restores_before_release(
    monkeypatch, tmp_path, phase, store
):
    sample = tmp_path / "sample.txt"
    sample.write_text("safe")
    entered = threading.Event()
    barrier = threading.Event()
    restore_entered = threading.Event()
    restore_barrier = threading.Event()

    class Wifi(FakeWifi):
        def connect(self, options):
            result = super().connect(options)
            if options.ssid == "office" and phase == "switch":
                entered.set()
                assert barrier.wait(10)
            if options.ssid == "original":
                restore_entered.set()
                assert restore_barrier.wait(10)
            return result

    wifi = Wifi("original")
    profile = UploadProfile("up_test", "test", "https://example.test/upload", wifi_ssid="office")
    folders = []
    import tempfile

    real_mkdtemp = tempfile.mkdtemp

    def tempdir(*args, **kwargs):
        folder = real_mkdtemp(*args, dir=tmp_path, **kwargs)
        folders.append(folder)
        return folder

    monkeypatch.setattr(tempfile, "mkdtemp", tempdir)

    async def http_port(request):
        if phase != "switch":
            entered.set()
            if phase == "error":
                raise RuntimeError("HTTP port failed")
            await asyncio.Event().wait()
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(http_port)) as client:
        task = asyncio.create_task(
            upload_selected_paths(store, profile, [str(sample)], wifi=wifi, client=client)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            if phase != "error":
                task.cancel()
                await asyncio.sleep(0.02)
                assert not task.done()
                assert HardwareLease.try_acquire() is None
            barrier.set()
            assert await asyncio.to_thread(restore_entered.wait, 5)
            task.cancel()
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            assert HardwareLease.try_acquire() is None
        finally:
            barrier.set()
            restore_barrier.set()
            await asyncio.gather(task, return_exceptions=True)
    from pathlib import Path

    assert wifi.current == "original"
    assert all(not Path(folder).exists() for folder in folders)
    with hardware_operation():
        pass


@pytest.mark.asyncio
async def test_certificate_finish_does_not_release_while_native_probe_alive():
    entered = threading.Event()
    barrier = threading.Event()

    def probe():
        entered.set()
        assert barrier.wait(10)
        return CertDialogResult(False, "", absent=True)

    watch = CertificateWatch("test", probe=probe, timeout=0.01)

    async def operation():
        with hardware_operation():
            watch.start()
            await run_hardware_blocking(watch.finish, 0.01)

    task = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # Beyond the old final join(1), the real native probe is still alive.
        done, _ = await asyncio.wait({task}, timeout=1.15)
        assert not done
        assert HardwareLease.try_acquire() is None
    finally:
        barrier.set()
        await task
    with hardware_operation():
        pass


@pytest.fixture
def app_context(tmp_path, monkeypatch, store):
    import spider_gd.core.crawler.shared_browser as browsers
    import spider_gd.infrastructure.network.wifi_connector as network
    from spider_gd.app import assemble
    from spider_gd.config.settings import load_config
    from spider_gd.gateway.api.server import create_app
    from spider_gd.gateway.control_service import ControlService

    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "api.db")
    settings.storage.data_dir = str(tmp_path / "outputs")
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    ctx.state_store.save_task_config(
        {
            "id": "lease-task",
            "name": "lease-task",
            "indicators": ["lease-metric"],
            "backfill_days": 1,
        }
    )
    ctx.state_store.save_workflow_config(
        {
            "id": "lease-metric",
            "name": "lease-metric",
            "yaml_text": (
                "name: lease-metric\nneeds_login: false\nsteps:\n  - action: wait\n    ms: 1\n"
            ),
        }
    )
    session, browser_calls = browser_port(monkeypatch)
    monkeypatch.setattr(browsers, "_shared_browser", session)
    network_calls = []

    class Network(FakeWifi):
        def __init__(self):
            super().__init__("original")

        def current_ssid(self, interface_name=""):
            network_calls.append("ssid")
            return super().current_ssid(interface_name)

        def prepare(self, options):
            network_calls.append("prepare")
            return self.connect(options)

    monkeypatch.setattr(network, "WifiConnector", Network)
    import spider_gd.infrastructure.network as network_package

    monkeypatch.setattr(network_package, "WifiConnector", Network)
    import spider_gd.gateway.data_upload_flow as flow
    import spider_gd.gateway.data_uploader as uploader

    monkeypatch.setattr(uploader, "WifiConnector", Network)
    monkeypatch.setattr(flow, "WifiConnector", Network)
    app = create_app(ControlService(ctx))
    yield ctx, app, session, browser_calls, network_calls
    ctx.state_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("with_stations", [False, True])
async def test_public_task_busy_skips_network_cert_picker_browser_and_output(
    app_context, tmp_path, monkeypatch, scheduled, with_stations
):
    import spider_gd.core.station_picker as picker
    import spider_gd.infrastructure.network.usb_devices as usb
    from spider_gd.app import _task_runner_job

    ctx, app, _, browser_calls, network_calls = app_context
    cfg = ctx.state_store.get_task_config("lease-task")
    cfg["ukey_station_ids"] = ["fixture-station"] if with_stations else []
    ctx.state_store.save_task_config(cfg)
    ctx.state_store.save_settings({"wifiAutoConnectEnabled": "true", "wifiSsid": "office"})
    cert_calls = []

    def native(*args, **kwargs):
        cert_calls.append("native")
        raise AssertionError("busy task touched certificate/picker port")

    monkeypatch.setattr(usb, "list_usb_devices", native)
    monkeypatch.setattr(usb, "_list_certificates", native)
    monkeypatch.setattr(picker, "pick_stations", native)
    with hardware_operation():
        if scheduled:
            await _task_runner_job(ctx, "lease-task")({})
            logs = ctx.state_store.list_execution_logs()
            assert len(logs) == 1
            assert logs[0].status == "failed"
            assert "共享硬件" in logs[0].reason
        else:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://fixture"
            ) as client:
                response = await client.post(
                    "/api/tasks/lease-task/run/stream",
                    json={"download_dir": str(tmp_path / "outputs")},
                )
            assert response.status_code == 409
            assert "共享硬件" in response.json()["detail"]
        assert network_calls == []
        assert cert_calls == []
        assert browser_calls == []
        assert not (tmp_path / "outputs").exists()


@pytest.mark.asyncio
async def test_http_wifi_busy_is_409_and_validation_stays_400(app_context):
    _, app, _, _, network_calls = app_context
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        with hardware_operation():
            response = await client.post(
                "/api/upload-profiles/test-wifi", json={"profile": _profile_payload()}
            )
            assert response.status_code == 409
            assert "共享硬件" in response.json()["detail"]
            assert network_calls == []
        response = await client.post(
            "/api/upload-profiles/test-wifi", json={"profileId": "missing"}
        )
        assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["scheduled", "unconsumed", "http-disconnect"])
async def test_task_done_holds_lease_through_real_borrowed_final_upload(
    app_context, tmp_path, monkeypatch, mode
):
    scheduled = mode == "scheduled"
    import spider_gd.gateway.data_upload_flow as flow
    import spider_gd.gateway.data_uploader as uploader
    from spider_gd.app import _task_runner_job
    from spider_gd.gateway.upload_profiles import save_profiles

    ctx, app, session, _, _ = app_context
    save_profiles(ctx.state_store, [_profile_payload()])
    cfg = ctx.state_store.get_task_config("lease-task")
    cfg.update(upload_profile_id="up_test", auto_upload=True)
    ctx.state_store.save_task_config(cfg)
    monkeypatch.setattr(flow, "runtime_release", lambda: {"channel": "stable"})
    connect = session.ensure_connected

    async def connect_and_make_output():
        await connect()
        sample = tmp_path / "outputs" / "result.txt"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text("safe")
        # Scheduled output root is date-layered; create under its actual snapshot root.
        if scheduled:
            from datetime import datetime

            dated = (
                sample.parent
                / datetime.now().strftime("%Y-%m")
                / datetime.now().strftime("%Y-%m-%d")
                / "result.txt"
            )
            dated.write_text("safe")

    monkeypatch.setattr(session, "ensure_connected", connect_and_make_output)
    entered = asyncio.Event()
    barrier = asyncio.Event()

    async def http_port(request):
        entered.set()
        await barrier.wait()
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(http_port)) as upload_client:
        monkeypatch.setattr(uploader.httpx, "AsyncClient", lambda **kwargs: upload_client)
        if scheduled:
            task = asyncio.create_task(_task_runner_job(ctx, "lease-task")({}))
        elif mode == "http-disconnect":
            import json

            terminal = asyncio.Event()
            requested = False

            async def receive():
                nonlocal requested
                if not requested:
                    requested = True
                    return {"type": "http.request", "body": b"{}", "more_body": False}
                await terminal.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body":
                    for line in message.get("body", b"").decode().splitlines():
                        if line.startswith("data: "):
                            event = json.loads(line[6:])
                            if event.get("event") == "task_done":
                                terminal.set()

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/tasks/lease-task/run/stream",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"content-type", b"application/json")],
                "server": ("fixture", 80),
                "client": ("fixture", 123),
            }
            task = asyncio.create_task(app(scope, receive, send))
        else:
            # Registered public route, deliberately never consume its SSE response.
            route = next(
                r
                for r in app.routes
                if r.path == "/api/tasks/{name}/run/stream" and "POST" in r.methods
            )
            from spider_gd.gateway.api.server import WorkflowRunRequest

            response = await route.endpoint(
                "lease-task", WorkflowRunRequest(download_dir=str(tmp_path / "outputs"))
            )
            assert response.status_code == 200
            task = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            logs = ctx.state_store.list_execution_logs()
            assert len(logs) == 1
            assert "任务完成" in ctx.state_store.get_execution_log(logs[0].exec_id).detail_log
            assert HardwareLease.try_acquire() is None
            if mode == "unconsumed":
                # Closing an unconsumed generator must not own or release the lease.
                await response.body_iterator.aclose()
                assert HardwareLease.try_acquire() is None
            elif mode == "http-disconnect":
                await asyncio.wait_for(terminal.wait(), 5)
                await asyncio.wait_for(task, 5)
                assert HardwareLease.try_acquire() is None
        finally:
            barrier.set()
            if task is not None:
                await task
            import spider_gd.gateway.api.server as server

            await asyncio.gather(*server._browser_background_tasks)
    with hardware_operation():
        pass
    assert ctx.state_store.list_execution_logs()[0].status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_public_desktop_delivery_cancel_waits_for_real_sender(
    app_context, tmp_path, monkeypatch, scheduled
):
    import zipfile

    import spider_gd.gateway.api.server as server
    from spider_gd.app import _task_runner_job
    from spider_gd.gateway.api.server import WorkflowRunRequest
    from spider_gd.gateway.delivery.desktop_wechat import DesktopWechatResult, DesktopWechatSender

    ctx, app, session, _, _ = app_context
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "stable")
    cfg = ctx.state_store.get_task_config("lease-task")
    cfg["path_template"] = "delivery-fixture"
    ctx.state_store.save_task_config(cfg)
    ctx.state_store.save_settings(
        {
            "downloadDir": str(tmp_path / "outputs"),
            "deliveryEnabled": "true",
            "deliveryBackend": "desktop_wechat",
            "deliveryDesktopWechatTarget": "文件传输助手",
            "deliveryDesktopWechatBackend": "powershell",
            "deliveryDesktopWechatSelectMode": "current",
            "deliveryDesktopWechatProfilePath": str(tmp_path / "wechat_targets.json"),
            "deliverySendMessage": "true",
            "deliverySendFile": "true",
        }
    )
    connect = session.ensure_connected

    async def connect_and_make_output():
        await connect()
        sample = tmp_path / "outputs" / "delivery-fixture" / "result.txt"
        sample.write_text("safe")

    monkeypatch.setattr(session, "ensure_connected", connect_and_make_output)
    entered = threading.Event()
    barrier = threading.Event()
    exited = threading.Event()
    calls = []

    def send(_sender, options):
        calls.append(options)
        entered.set()
        try:
            assert barrier.wait(10)
            return DesktopWechatResult(ok=True, dry_run=False, platform="fixture")
        finally:
            exited.set()

    # Keep settings resolution, both delivery hooks, and scheduled/API ownership real.
    monkeypatch.setattr(DesktopWechatSender, "send", send)
    existing_tasks = asyncio.all_tasks()
    response = None
    if scheduled:
        owner = asyncio.create_task(_task_runner_job(ctx, "lease-task")({}))
    else:
        previous_owners = set(server._browser_background_tasks)
        route = next(
            r
            for r in app.routes
            if r.path == "/api/tasks/{name}/run/stream" and "POST" in r.methods
        )
        response = await route.endpoint("lease-task", WorkflowRunRequest())
        assert response.status_code == 200
        owners = server._browser_background_tasks - previous_owners
        assert len(owners) == 1
        owner = next(iter(owners))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert len(calls) == 1
        options = calls[0]
        assert options.target == "文件传输助手"
        assert options.backend == "powershell"
        assert options.select_mode == "current"
        assert options.profile_path == str(tmp_path / "wechat_targets.json")
        assert options.dry_run is False
        assert "lease-task" in options.message
        with zipfile.ZipFile(options.file_path) as archive:
            assert archive.namelist() == ["result.txt"]
            assert archive.read("result.txt") == b"safe"
        assert len(ctx.state_store.list_delivery_outbox()) == 1
        for attempt in range(3):
            for task in asyncio.all_tasks() - existing_tasks:
                task.cancel(f"desktop-cancel-{attempt}")
            done, _ = await asyncio.wait({owner}, timeout=0.03)
            assert not done, "desktop sender outlived scheduled/API hardware ownership"
            assert not exited.is_set()
            assert HardwareLease.try_acquire() is None
        barrier.set()
        with pytest.raises(asyncio.CancelledError, match="desktop-cancel-0"):
            await owner
        assert exited.is_set()
        with hardware_operation():
            pass
    finally:
        barrier.set()
        await asyncio.gather(owner, return_exceptions=True)
        await asyncio.to_thread(exited.wait, 5)
        if response is not None:
            await response.body_iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["trace", "finish"])
async def test_workflow_public_run_cancellation_settles_native_watch(monkeypatch, tmp_path, phase):
    from workflow_runner import WorkflowRunner

    import spider_gd.infrastructure.windows.cert_dialog as cert

    session, _ = browser_port(monkeypatch)
    await session.ensure_connected()
    entered = threading.Event()
    barrier = threading.Event()
    trace_entered = asyncio.Event()

    def native(*args):
        entered.set()
        assert barrier.wait(10)
        return CertDialogResult(False, "", absent=True)

    async def cookies(*args):
        trace_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cert, "confirm_certificate", native)
    session.page.context.cookies = cookies
    runner = WorkflowRunner(
        {
            "name": "watch",
            "type": "login",
            "needs_login": False,
            "trace_browser_actions": phase == "trace",
            "_station": {"org_name": "fixture"},
            "steps": [{"action": "click", "selector": "button:has-text('立即登录')"}],
        },
        download_dir=str(tmp_path),
    )
    task = asyncio.create_task(runner.run(session=session))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if phase == "trace":
            await asyncio.wait_for(trace_entered.wait(), 5)
        else:
            await asyncio.sleep(0.05)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            assert HardwareLease.try_acquire() is None
    finally:
        barrier.set()
        await asyncio.gather(task, return_exceptions=True)
    with hardware_operation():
        pass


@pytest.mark.asyncio
async def test_task_runner_public_wifi_worker_settles_on_cancel(monkeypatch, tmp_path):
    import spider_gd.infrastructure.network.wifi_connector as network

    session, _ = browser_port(monkeypatch)
    entered = threading.Event()
    barrier = threading.Event()

    class Network:
        def list_networks(self):
            return [SimpleNamespace(ssid="fixture")]

        def prepare(self, options):
            entered.set()
            assert barrier.wait(10)
            return SimpleNamespace(ok=True)

    monkeypatch.setattr(network, "WifiConnector", Network)
    runner = TaskRunner(
        {"name": "wifi", "indicators": ["metric"]},
        download_dir=str(tmp_path),
        workflow_loader=lambda name: {
            "name": "metric",
            "needs_login": False,
            "steps": [{"action": "wait", "ms": 1}],
        },
        station_queue=[{"name": "station", "wifi_ssid": "fixture"}],
    )
    task = asyncio.create_task(runner.run(session=session))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            assert HardwareLease.try_acquire() is None
    finally:
        barrier.set()
        await asyncio.gather(task, return_exceptions=True)
    with hardware_operation():
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_public_task_cert_preflight_cancel_waits_for_probe(
    app_context, monkeypatch, scheduled
):
    import spider_gd.infrastructure.network.usb_devices as usb
    import spider_gd.infrastructure.windows.cert_availability as cert
    from spider_gd.app import _task_runner_job

    ctx, app, _, browser_calls, _ = app_context
    cfg = ctx.state_store.get_task_config("lease-task")
    cfg["ukey_station_ids"] = ["fixture"]
    ctx.state_store.save_task_config(cfg)
    entered = threading.Event()
    barrier = threading.Event()

    def probe():
        entered.set()
        assert barrier.wait(10)
        return set()

    monkeypatch.setattr(usb, "list_usb_devices", lambda: [])
    monkeypatch.setattr(usb, "_list_certificates", lambda: [])
    monkeypatch.setattr(cert, "silent_thumbprints", probe)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        task = asyncio.create_task(
            _task_runner_job(ctx, "lease-task")({})
            if scheduled
            else client.post("/api/tasks/lease-task/run/stream", json={})
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.02)
                assert not task.done()
                assert HardwareLease.try_acquire() is None
            assert browser_calls == []
        finally:
            barrier.set()
            await asyncio.gather(task, return_exceptions=True)
    with hardware_operation():
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "phase"), [
    ("/api/network/wifi/test", "prepare"),
    ("/api/network/route-rules/apply", "prepare"),
    ("/api/network/route-rules/apply", "apply"),
])
@pytest.mark.parametrize("worker_error", [False, True])
async def test_rest_network_repeated_cancel_keeps_lease_until_worker_exits(
    app_context, monkeypatch, path, phase, worker_error
):
    from spider_gd.infrastructure.network.dual_nic_closure import DualNicClosureReport
    from spider_gd.infrastructure.network.wifi_connector import WifiConnectResult

    ctx, app, _, _, _ = app_context
    ctx.state_store.save_settings({"wifiAutoConnectEnabled": "true", "wifiSsid": "fixture"})
    entered = threading.Event()
    barrier = threading.Event()
    exited = threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    workers = []
    calls = []

    def assert_worker_owned():
        assert threading.get_ident() != loop_thread, "REST native work blocked the event loop"
        contender = HardwareLease.try_acquire()
        if contender is not None:
            contender.close()
        assert contender is None, "REST native work ran outside the hardware guard"
        workers.append(threading.get_ident())

    def blocking_port():
        entered.set()
        try:
            assert barrier.wait(15), "test did not release the native-port barrier"
            if worker_error:
                raise ValueError("fixture native failure")
        finally:
            exited.set()

    class WifiPort:
        def prepare(self, options):
            assert_worker_owned()
            calls.append("prepare")
            if phase == "prepare":
                blocking_port()
            return WifiConnectResult(ok=True, target_ssid=options.ssid, message="ready")

    class ClosurePort:
        def __init__(self, *, cache_dir):
            assert_worker_owned()
            calls.append("closure")

        def apply_rules(self, rules):
            assert_worker_owned()
            calls.append("apply")
            if phase == "apply":
                blocking_port()
            return DualNicClosureReport(status="ok", applied=0, results=[], details=[])

    monkeypatch.setattr("spider_gd.infrastructure.network.WifiConnector", WifiPort)
    monkeypatch.setattr("spider_gd.infrastructure.network.DualNicClosure", ClosurePort)
    route = next(r for r in app.routes if r.path == path and "POST" in r.methods)
    owner = asyncio.create_task(route.endpoint())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for attempt in range(3):
            owner.cancel(f"rest-cancel-{attempt}")
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not owner.done(), "REST owner released before the worker settled"
            assert not exited.is_set()
            assert HardwareLease.try_acquire() is None
        barrier.set()
        with pytest.raises(asyncio.CancelledError, match="rest-cancel-0"):
            await asyncio.wait_for(owner, 5)
        assert exited.is_set()
        assert calls == (
            ["prepare"] if path.endswith("wifi/test") or (worker_error and phase == "prepare")
            else ["prepare", "closure", "apply"]
        )
        assert len(set(workers)) == 1
        with hardware_operation():
            pass
    finally:
        barrier.set()
        await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_independent_desktop_repeated_cancel_keeps_lease_until_sender_exits(
    tmp_path, monkeypatch
):
    from spider_gd.app import send_delivery_test_message
    from spider_gd.config.settings import AppSettings, DeliverySettings
    from spider_gd.gateway.delivery.desktop_wechat import DesktopWechatResult, DesktopWechatSender

    entered = threading.Event()
    barrier = threading.Event()
    exited = threading.Event()
    calls = []

    def send(_sender, options):
        contender = HardwareLease.try_acquire()
        if contender is not None:
            contender.close()
        assert contender is None, "independent desktop sender ran without a lease"
        calls.append(options)
        entered.set()
        try:
            assert barrier.wait(15)
            return DesktopWechatResult(ok=True, dry_run=False, platform="fixture")
        finally:
            exited.set()

    monkeypatch.setattr(DesktopWechatSender, "send", send)
    settings = AppSettings(delivery=DeliverySettings(
        enabled=True, backend="desktop_wechat", desktop_wechat_target="fixture-target",
    ))
    owner = asyncio.create_task(send_delivery_test_message(SimpleNamespace(settings=settings)))
    loop = asyncio.get_running_loop()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for attempt in range(3):
            owner.cancel(f"independent-cancel-{attempt}")
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not owner.done()
            assert not exited.is_set()
            assert HardwareLease.try_acquire() is None
        barrier.set()
        with pytest.raises(asyncio.CancelledError, match="independent-cancel-0"):
            await asyncio.wait_for(owner, 5)
        assert exited.is_set()
        assert len(calls) == 1
        with hardware_operation():
            pass
    finally:
        barrier.set()
        await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_http_wifi_cancellation_waits_for_switch_and_restore(app_context, monkeypatch):
    import spider_gd.gateway.data_upload_flow as flow

    _, app, _, _, _ = app_context
    entered = threading.Event()
    barrier = threading.Event()
    wifi = FakeWifi("original")
    connect = wifi.connect

    def switching(options):
        result = connect(options)
        if options.ssid == "office":
            entered.set()
            assert barrier.wait(10)
        return result

    wifi.connect = switching
    monkeypatch.setattr(flow, "WifiConnector", lambda: wifi)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        task = asyncio.create_task(
            client.post("/api/upload-profiles/test-wifi", json={"profile": _profile_payload()})
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.02)
                assert not task.done()
                assert HardwareLease.try_acquire() is None
        finally:
            barrier.set()
            await asyncio.gather(task, return_exceptions=True)
    assert wifi.current == "original"
    with hardware_operation():
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_public_task_final_log_failure_still_releases(app_context, monkeypatch, scheduled):
    import spider_gd.gateway.api.server as server
    from spider_gd.app import _task_runner_job
    from spider_gd.gateway.api.server import WorkflowRunRequest

    ctx, app, _, _, _ = app_context
    update = ctx.state_store.update_execution_log

    def failing_log(exec_id, **kwargs):
        if kwargs.get("status") == "success":
            assert HardwareLease.try_acquire() is None
            raise RuntimeError("fixture final storage failure")
        return update(exec_id, **kwargs)

    monkeypatch.setattr(ctx.state_store, "update_execution_log", failing_log)
    if scheduled:
        with pytest.raises(RuntimeError, match="fixture final storage failure"):
            await _task_runner_job(ctx, "lease-task")({})
    else:
        route = next(
            r
            for r in app.routes
            if r.path == "/api/tasks/{name}/run/stream" and "POST" in r.methods
        )
        response = await route.endpoint("lease-task", WorkflowRunRequest())
        results = await asyncio.gather(*server._browser_background_tasks, return_exceptions=True)
        assert any(isinstance(result, RuntimeError) for result in results)
        await response.body_iterator.aclose()
    with hardware_operation():
        pass
