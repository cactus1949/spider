"""WifiRack 移动工作台联动、动态按 SSID 唤醒、预唤醒流水线与公有数据 any_one 模式测试。"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from spider_gd.core.station_profile import StationProfile
from spider_gd.core.ukey_plan import (
    KnownCert,
    configured_station_mode,
    plan_task_execution,
    prioritize_stations_for_any_one,
)
from spider_gd.gateway.settings_update import filter_settings_update
from spider_gd.infrastructure.network.wifi_connector import WifiConnectResult, WifiNetwork
from spider_gd.infrastructure.network.workbench_client import (
    WorkbenchClient,
    WorkbenchConfig,
    WorkbenchWakeResult,
    workbench_config_from_store,
)
from spider_gd.infrastructure.storage.database import TaskStateStore


@pytest.mark.unit
def test_workbench_http_wake_polls_job_until_woke() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    poll_count = {"n": 0}
    now = {"t": 0.0}

    def fake_http(
        method: str, path: str, payload: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        calls.append((method, path, payload))
        if path == "/api/wake":
            return {"ok": True, "jobId": 7, "status": "queued", "done": False}
        if path == "/api/job?id=7":
            poll_count["n"] += 1
            if poll_count["n"] == 1:
                return {"ok": True, "jobId": 7, "status": "running", "done": False}
            return {
                "ok": True,
                "jobId": 7,
                "status": "woke",
                "done": True,
                "online": True,
                "targetSsid": "HUAWEI-SG03",
                "slotId": 4,
                "rssi": -39,
                "message": "动态搜索短按唤醒成功 (槽4)",
            }
        raise AssertionError(f"unexpected path: {path}")

    client = WorkbenchClient(
        WorkbenchConfig(enabled=True, transport="http", base_url="http://192.168.1.99"),
        http_requester=fake_http,
        clock=lambda: now["t"],
        sleeper=lambda sec: now.__setitem__("t", now["t"] + sec),
        poll_interval_sec=0.5,
    )
    res = client.wake_by_ssid("HUAWEI-SG03", name="韶关浈江", wifi_password="pw")
    assert res.ok is True
    assert res.status == "woke"
    assert res.slot_id == 4
    assert res.rssi == -39
    assert res.transport == "http"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/wake"
    assert calls[0][2]["ssid"] == "HUAWEI-SG03"
    assert calls[0][2]["wifiPass"] == "pw"


@pytest.mark.unit
def test_workbench_auto_transport_prefers_serial_and_falls_back_to_http() -> None:
    serial_attempts = {"n": 0}

    def failing_serial(frame: dict[str, Any], timeout: float) -> dict[str, Any]:
        serial_attempts["n"] += 1
        raise RuntimeError("COM5 busy")

    def fake_http(
        method: str, path: str, payload: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        assert path == "/api/status"
        return {
            "version": "4.3",
            "slots": [{"id": 2, "ssid": "WIFI-B", "present": True, "wifiOnline": True}],
            "visibleKnown": [{"ssid": "WIFI-C", "rssi": -41}],
        }

    client = WorkbenchClient(
        WorkbenchConfig(enabled=True, transport="auto", serial_port="COM5"),
        http_requester=fake_http,
        serial_commander=failing_serial,
    )
    online = client.list_online_ssids()
    assert serial_attempts["n"] == 1
    assert online == {"WIFI-B", "WIFI-C"}


@pytest.mark.unit
def test_workbench_sync_profiles_and_secret_mask() -> None:
    sent: list[dict[str, Any]] = []

    def fake_serial(frame: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append(frame)
        return {"ok": True, "count": len(frame.get("profiles") or [])}

    client = WorkbenchClient(
        WorkbenchConfig(enabled=True, transport="serial", serial_port="COM5"),
        serial_commander=fake_serial,
    )
    profiles = [
        StationProfile("s1", "阳江", "YJ-WIFI", "pw1", "pin1"),
        StationProfile("s2", "空WiFi", "", "", "pin2"),
        StationProfile("s3", "阳西", "YX-WIFI", "pw3", "pin3"),
    ]
    res = client.sync_profiles(profiles)
    assert res["ok"] is True
    assert res["count"] == 2
    assert sent[0]["cmd"] == "sync_profiles"
    assert sent[0]["profiles"] == [
        {"name": "阳江", "ssid": "YJ-WIFI", "wifiPass": "pw1"},
        {"name": "阳西", "ssid": "YX-WIFI", "wifiPass": "pw3"},
    ]

    filtered = filter_settings_update(
        {"workbenchToken": "******", "workbenchUrl": "http://10.0.0.5"}
    )
    assert "workbenchToken" not in filtered
    assert filtered["workbenchUrl"] == "http://10.0.0.5"


@pytest.mark.unit
def test_public_data_any_one_planning_priority_and_db_persistence(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path / "state.db")
    try:
        store.save_settings({
            "workbenchEnabled": "true",
            "workbenchTransport": "serial",
            "workbenchSerialPort": "COM8",
        })
        cfg = workbench_config_from_store(store)
        assert cfg.enabled is True
        assert cfg.transport == "serial"
        assert cfg.serial_port == "COM8"

        store.save_task_config({
            "id": "pub-1",
            "name": "公有价格采集",
            "indicators": ["日前出清"],
            "station_mode": "any_one",
            "ukey_station_ids": ["a", "b"],
        })
        saved = store.get_task_config("pub-1")
        assert saved is not None
        assert saved["station_mode"] == "any_one"
    finally:
        store.close()

    assert configured_station_mode({"station_mode": "any_one"}) == "any_one"
    assert configured_station_mode({}) == "all"

    ordered = prioritize_stations_for_any_one(
        [
            {"name": "站1", "wifi_ssid": "WIFI-1"},
            {"name": "站2", "wifi_ssid": "WIFI-2"},
            {"name": "站3", "wifi_ssid": "WIFI-3"},
        ],
        current_ssid="WIFI-3",
        online_ssids={"WIFI-2"},
    )
    assert [row["name"] for row in ordered] == ["站3", "站2", "站1"]

    stations = [
        StationProfile("a", "阳江", "WIFI-1", device_id="SN-1", org_name="阳江"),
        StationProfile("b", "阳西", "WIFI-2", device_id="SN-2", org_name="阳西"),
    ]
    certs = [KnownCert("aa", "阳江公司", True), KnownCert("bb", "阳西公司", True)]
    plan = plan_task_execution(
        stations,
        selected_ids=[],
        present_device_ids=["SN-1", "SN-2"],
        certs=certs,
        mode="manual",
        station_mode="any_one",
        opener=None,
    )
    assert plan.started is True
    assert [row["name"] for row in plan.runnable] == ["阳江", "阳西"]


@pytest.mark.unit
def test_task_runner_wakes_missing_ssid_via_workbench_with_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from task_runner import TaskRunner

    visible_state = {"online": False}
    connect_calls: list[tuple[str, str]] = []
    wake_calls: list[tuple[str, str]] = []

    class _FakeConnector:
        def current_ssid(self, interface_name: str = "") -> str:
            return ""

        def list_networks(self, interface_name: str = "") -> list[WifiNetwork]:
            if visible_state["online"]:
                return [WifiNetwork(ssid="HUAWEI-YJ01")]
            return []

        def prepare(self, options) -> WifiConnectResult:
            connect_calls.append((options.ssid, options.interface_name))
            return WifiConnectResult(ok=True, target_ssid=options.ssid, current_ssid=options.ssid)

    class _FakeWorkbench:
        enabled = True

        def wake_by_ssid(
            self, ssid: str, *, name: str = "", wifi_password: str = "", cancelled=None
        ):
            wake_calls.append((ssid, name))
            visible_state["online"] = True
            return WorkbenchWakeResult(
                ok=True, status="woke", ssid=ssid, slot_id=3, message="短按唤醒成功 (槽3)"
            )

    monkeypatch.setattr(
        "spider_gd.infrastructure.network.wifi_connector.WifiConnector",
        _FakeConnector,
    )

    events: list[dict[str, Any]] = []
    runner = TaskRunner(
        {"name": "测试", "_unattended": True},
        workbench_client=_FakeWorkbench(),
        wifi_interface_name="WLAN 2",
    )
    runner._on_event = events.append
    outcome = asyncio.run(runner._connect_station_wifi("阳江", "HUAWEI-YJ01", "pw123"))

    assert outcome == "ok"
    assert wake_calls == [("HUAWEI-YJ01", "阳江")]
    assert connect_calls == [("HUAWEI-YJ01", "WLAN 2")]
    assert any("工作台" in str(ev.get("message", "")) for ev in events)


@pytest.mark.unit
def test_task_runner_prewakes_next_station_and_supports_any_one_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from task_runner import TaskRunner

    # 1) 验证流水线预唤醒去重与触发
    prewoken: list[tuple[str, str]] = []

    class _PrewakeWorkbench:
        enabled = True

        def prewake_by_ssid(self, ssid: str, *, name: str = "", wifi_password: str = ""):
            prewoken.append((ssid, name))
            return WorkbenchWakeResult(ok=True, status="queued", ssid=ssid, message="预唤醒已提交")

    runner = TaskRunner(
        {"name": "轮询任务", "station_mode": "all"},
        workbench_client=_PrewakeWorkbench(),
    )

    async def _run_prewake_check() -> None:
        events: list[dict[str, Any]] = []

        def emit_fn(event_name: str, **kw: Any) -> None:
            events.append({"event": event_name, **kw})

        next_st = {"name": "站2", "wifi_ssid": "WIFI-2", "wifi_password": "p2"}
        runner._schedule_prewake_station(next_st, emit_fn)
        runner._schedule_prewake_station(next_st, emit_fn)
        await asyncio.gather(*runner._prewake_tasks)
        assert prewoken == [("WIFI-2", "站2")]
        assert any("预唤醒下一站" in str(ev.get("message", "")) for ev in events)

    asyncio.run(_run_prewake_check())

    # 2) 验证公有数据 any_one 模式：第 1 站切网跳过，第 2 站成功即停，第 3 站不跑
    class _DummyPage:
        context = None

    class _DummySession:
        _cancel_requested = False
        _browser = None
        page = _DummyPage()
        _active_runner = None

        @contextlib.asynccontextmanager
        async def execution(self, _name: str, on_event=None, *, hardware_lease=None):
            yield (on_event or (lambda _ev: None))

        async def wait_continue(self):
            return True

    ran_stations: list[str] = []
    pub_runner = TaskRunner(
        {
            "name": "公有价格",
            "station_mode": "any_one",
            "indicators": ["日前价格"],
            "_unattended": True,
        },
        workflow_loader=lambda name: {"name": name, "steps": []},
        station_queue=[
            {"name": "站1", "wifi_ssid": "WIFI-1", "cert_password": "p1"},
            {"name": "站2", "wifi_ssid": "WIFI-2", "cert_password": "p2"},
            {"name": "站3", "wifi_ssid": "WIFI-3", "cert_password": "p3"},
        ],
    )

    async def fake_use_station(station: dict, index: int, total: int) -> str:
        ran_stations.append(station["name"])
        if station["name"] == "站1":
            pub_runner._station_results.append(
                {"name": "站1", "status": "skipped", "reason": "离线"}
            )
            return "skip"
        return "ok"

    async def fake_run_with_retries(item, step_idx, total_steps, _emit, keys) -> str:
        pub_runner._summary["success"] += 1
        return "ok"

    async def fake_noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "spider_gd.core.crawler.shared_browser.get_shared_browser",
        lambda: _DummySession(),
    )
    monkeypatch.setattr(pub_runner, "_use_station", fake_use_station)
    monkeypatch.setattr(pub_runner, "_run_with_retries", fake_run_with_retries)
    monkeypatch.setattr(pub_runner, "_prepare_browser", fake_noop)
    monkeypatch.setattr(pub_runner, "_drop_station_session", fake_noop)
    monkeypatch.setattr(pub_runner, "_dismiss_workbench_popups", fake_noop)
    monkeypatch.setattr(pub_runner, "_finish_indicator_pages", fake_noop)

    emitted: list[dict[str, Any]] = []
    asyncio.run(pub_runner.run(on_event=emitted.append))

    assert ran_stations == ["站1", "站2"]
    assert any(ev.get("event") == "task_done" and ev.get("success") == 1 for ev in emitted)
    report_events = [ev for ev in emitted if ev.get("event") == "station_report"]
    assert report_events
    statuses = {row["name"]: row["status"] for row in report_events[-1]["stations"]}
    assert statuses == {"站1": "skipped", "站2": "ran", "站3": "skipped"}


@pytest.mark.unit
async def test_workbench_mcp_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from spider_gd.gateway.mcp.server import create_workbench_mcp_server

    class _FakeStore:
        def get_setting(self, key: str) -> str:
            if key == "stationProfiles":
                return '[{"id":"a","name":"阳江","wifiSsid":"YJ-WIFI","wifiPassword":"pw"}]'
            return ""

    class _FakeCtx:
        state_store = _FakeStore()

    class _FakeSvc:
        _ctx = _FakeCtx()

        def list_tasks(self, limit: int = 20):
            return []

        def get_task(self, task_id: str):
            return {}

        async def health(self):
            return {"overall": "healthy"}

    class _FakeClient:
        enabled = True

        def get_status(self):
            return {"version": "4.3", "slots": []}

        def wake_by_ssid(
            self, ssid: str, *, name: str = "", wifi_password: str = "", wait: bool = True
        ):
            return WorkbenchWakeResult(
                ok=True, status="woke", ssid=ssid, slot_id=2, message="已唤醒"
            )

        def discover_slots(self, *, wait: bool = False):
            return {"ok": True, "jobId": 9, "status": "queued"}

    monkeypatch.setattr(
        "spider_gd.app.build_workbench_client",
        lambda ctx, require_enabled=False: _FakeClient(),
    )

    mcp = create_workbench_mcp_server(_FakeSvc())
    tools = {t.name for t in await mcp.list_tools()}
    assert {"workbench_status", "workbench_wake_device", "workbench_discover_slots"} <= tools

    _, wake_data = await mcp.call_tool("workbench_wake_device", {"station_name": "阳江"})
    assert wake_data["ok"] is True
    assert wake_data["ssid"] == "YJ-WIFI"
    assert wake_data["slot_id"] == 2


@pytest.mark.unit
def test_multi_station_all_mode_does_not_skip_second_station_indicators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：多场站 all 模式下，第 1 站写完 _skip_slots 后，第 2 站仍须执行同一指标。"""
    from task_runner import TaskRunner

    class _DummyPage:
        context = None

    class _DummySession:
        _cancel_requested = False
        _browser = None
        page = _DummyPage()
        _active_runner = None

        @contextlib.asynccontextmanager
        async def execution(self, _name: str, on_event=None, *, hardware_lease=None):
            yield (on_event or (lambda _ev: None))

        async def wait_continue(self):
            return True

    executed: list[tuple[str, str]] = []
    current_station = {"name": ""}

    runner = TaskRunner(
        {
            "name": "多场站专有数据",
            "station_mode": "all",
            "indicators": ["日前电量"],
            "_unattended": True,
        },
        workflow_loader=lambda name: {"name": name, "steps": []},
        station_queue=[
            {"name": "阳江", "wifi_ssid": "WIFI-1", "cert_password": "p1"},
            {"name": "阳西", "wifi_ssid": "WIFI-2", "cert_password": "p2"},
        ],
    )

    async def fake_use_station(station: dict, index: int, total: int) -> str:
        current_station["name"] = station["name"]
        return "ok"

    async def fake_run_with_retries(item, step_idx, total_steps, _emit, keys) -> str:
        executed.append((current_station["name"], item["indicator"]))
        runner._summary["success"] += 1
        runner._mark_slots(keys, "success")
        return "ok"

    async def fake_noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "spider_gd.core.crawler.shared_browser.get_shared_browser",
        lambda: _DummySession(),
    )
    monkeypatch.setattr(runner, "_use_station", fake_use_station)
    monkeypatch.setattr(runner, "_run_with_retries", fake_run_with_retries)
    monkeypatch.setattr(runner, "_prepare_browser", fake_noop)
    monkeypatch.setattr(runner, "_drop_station_session", fake_noop)
    monkeypatch.setattr(runner, "_dismiss_workbench_popups", fake_noop)
    monkeypatch.setattr(runner, "_finish_indicator_pages", fake_noop)

    emitted: list[dict[str, Any]] = []
    asyncio.run(runner.run(on_event=emitted.append))

    assert executed == [("阳江", "日前电量"), ("阳西", "日前电量")]
    assert any(ev.get("event") == "task_done" and ev.get("success") == 2 for ev in emitted)


@pytest.mark.unit
def test_workbench_api_respects_hardware_lease_and_wakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from spider_gd.gateway.api.server import create_app
    from spider_gd.infrastructure.hardware_lease import HardwareBusyError

    store = TaskStateStore(tmp_path / "api_wb.db")
    station_json = (
        '[{"id":"st1","name":"阳江","wifiSsid":"HUAWEI-YJ01","wifiPassword":"pw1"}]'
    )
    store.save_settings({
        "stationProfiles": station_json,
        "workbenchEnabled": "true",
    })

    class _FakeSettings:
        storage = SimpleNamespace(data_dir=str(tmp_path))
        platform = SimpleNamespace(domain="pm.gd.csg.cn")

    class _FakeCtx:
        state_store = store
        settings = _FakeSettings()
        scheduler = None
        alert = None

    class _FakeService:
        _ctx = _FakeCtx()

        async def health(self):
            return {"overall": "healthy"}

    class _FakeClient:
        enabled = True

        def get_status(self):
            return {"version": "4.3", "busVolts": 5.15, "slots": []}

        def wake_by_ssid(self, ssid: str, **kwargs):
            return WorkbenchWakeResult(
                ok=True, status="woke", ssid=ssid, slot_id=2, message="ok"
            )

    monkeypatch.setattr(
        "spider_gd.app.build_workbench_client",
        lambda ctx, require_enabled=False: _FakeClient(),
    )

    try:
        app = create_app(_FakeService())
        client = TestClient(app)

        resp = client.get("/api/workbench/status")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp_wake = client.post("/api/workbench/wake", json={"station_id": "st1"})
        assert resp_wake.status_code == 200
        body = resp_wake.json()
        assert body["ok"] is True
        assert body["ssid"] == "HUAWEI-YJ01"
        assert body["slot_id"] == 2

        @contextlib.contextmanager
        def _busy_op():
            raise HardwareBusyError("硬件正忙")
            yield

        monkeypatch.setattr(
            "spider_gd.infrastructure.hardware_lease.hardware_operation",
            _busy_op,
        )
        resp_busy = client.post("/api/workbench/wake", json={"ssid": "HUAWEI-YJ01"})
        assert resp_busy.status_code == 409
    finally:
        store.close()
