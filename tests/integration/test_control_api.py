"""集成：REST 控制接口（注入假采集 runner，不启动浏览器）。"""

from __future__ import annotations

import hashlib
import base64
import json
import sqlite3
import zipfile
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from spider_gd.app import assemble
from spider_gd.config.settings import load_config
from spider_gd.gateway.api.server import create_app
from spider_gd.gateway.control_service import ControlService
from spider_gd.infrastructure.network.dual_nic_closure import (
    DualNicClosureReport,
    DualNicRuleResult,
)
from spider_gd.infrastructure.network.wifi_connector import WifiConnectResult, WifiNetwork
from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.interfaces.storage import ExecutionLogRecord, TaskItemRecord, TaskRecord


@pytest.fixture
def client(tmp_path: Path):
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    ctx = assemble(settings)

    # 预置一条任务，便于查询接口
    ctx.state_store.create_task(
        TaskRecord("T1", "日常采集", "completed", str(tmp_path), "2026-06-22T09:00:00")
    )
    ctx.state_store.create_item(TaskItemRecord("T1", "日清算概览", "2026-06-21", "success"))

    async def _fake_runner(target_date: str, task_name: str):
        return {"task_id": "T2", "status": "completed", "summary": {"success": 1, "total": 1}}

    service = ControlService(ctx, crawl_runner=_fake_runner)
    app = create_app(service)
    try:
        yield TestClient(app)
    finally:
        ctx.state_store.close()


@pytest.mark.integration
def test_health_endpoint(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["overall"] in {"healthy", "degraded", "critical"}


@pytest.mark.integration
def test_ukey_report_download_packs_devices_without_secrets(
    client: TestClient, monkeypatch, tmp_path: Path,
):
    import spider_gd.infrastructure.network.usb_devices as usb

    monkeypatch.setattr(usb, "probe_ukeys", lambda: {
        "summary": "发现 2 把飞天 UKey。序列号：SN-A、SN-B。",
        "distinguishable": True,
        "error": "",
        "devices": [{"id": "USB\\1", "name": "钥匙", "serial": "SN-A", "ukeyCandidate": True}],
        "ukeys": [{"id": "USB\\1", "name": "钥匙", "serial": "SN-A"}],
        "certificates": [{
            "organization": "阳江",
            "subject": "CN=甲, O=阳江",
            "issuer": "CA",
            "notAfter": "2027-01-01",
            "thumbprint": "AA",
        }],
    })
    monkeypatch.setattr(usb, "ukey_raw_text", lambda: {
        "usb": "InstanceId USB\\VID_096E&PID_0317\\SN-A",
        "certificates": "Subject=CN=甲, O=阳江",
    })
    profiles = (
        '[{"id":"a","name":"阳江","wifiSsid":"CSG",'
        '"wifiPassword":"secret","certPassword":"pin"}]'
    )
    saved = client.put("/api/settings", json={"settings": {"stationProfiles": profiles}})
    assert saved.status_code == 200

    resp = client.post(
        "/api/stations/ukey-report/download",
        json={"download_dir": str(tmp_path / "downloads")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"].startswith("ukey_")
    assert body["filename"].endswith(".zip")
    with zipfile.ZipFile(body["path"]) as archive:
        names = set(archive.namelist())
        assert "app_log_window.txt" not in names
        assert "network_diagnostics.txt" not in names
        assert "environment.json" not in names
        assert {"summary.txt", "report.json", "raw_usb.txt", "raw_certificates.txt", "bindings.json"} <= names
        assert "SN-A" in archive.read("report.json").decode("utf-8")
        assert "VID_096E" in archive.read("raw_usb.txt").decode("utf-8")
        assert "飞天 UKey" in archive.read("summary.txt").decode("utf-8")
        bindings = archive.read("bindings.json").decode("utf-8")
        assert "阳江" in bindings
        assert "secret" not in bindings and "pin" not in bindings
        packed = "\n".join(archive.read(name).decode("utf-8", errors="replace") for name in names)
        assert "secret" not in packed and "pin" not in packed


def test_download_setting_is_used_by_managed_browser(client: TestClient, monkeypatch, tmp_path):
    import spider_gd.core.crawler.shared_browser as module

    session = module.SharedBrowser()
    monkeypatch.setattr(module, "_shared_browser", session)
    destination = tmp_path / "user-downloads"
    response = client.put("/api/settings", json={"settings": {"downloadDir": str(destination)}})
    assert response.status_code == 200
    assert session._download_root == destination
    assert not destination.exists(), "保存设置不应提前创建下载目录"


@pytest.mark.integration
def test_crawl_start_endpoint(client: TestClient):
    resp = client.post("/api/crawl/start", json={"target_date": "2026-06-21"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["task_id"] == "T2"


@pytest.mark.integration
def test_tasks_endpoints(client: TestClient):
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert any(t["id"] == "T1" for t in tasks)

    detail = client.get("/api/tasks/T1")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "completed"
    assert body["items"][0]["indicator"] == "日清算概览"


@pytest.mark.integration
def test_apply_route_rules_returns_dual_nic_closure_result(client: TestClient, mocker):
    captured: dict = {}

    class FakeDualNicClosure:
        def __init__(self, *, cache_dir):
            captured["cache_dir"] = str(cache_dir)

        def apply_rules(self, rules):
            captured["rules"] = rules
            return DualNicClosureReport(
                status="ok",
                applied=1,
                message="双网卡闭环验证通过",
                details=["OK 解析 pm.gd.csg.cn via UKey-WiFi: 36.137.0.13"],
                results=[
                    DualNicRuleResult(
                        domain="pm.gd.csg.cn",
                        interface_name="UKey-WiFi",
                        status="ok",
                        resolved_ips=["36.137.0.13"],
                        stale_ips=["36.137.0.12"],
                        gateway="10.88.0.1",
                        private_ok=True,
                        public_ok=True,
                        routes_ok=True,
                    )
                ],
            )

    mocker.patch("spider_gd.infrastructure.network.DualNicClosure", FakeDualNicClosure)

    resp = client.put("/api/network/route-rules", json={
        "rules": [
            {"domain_pattern": "pm.gd.csg.cn", "interface_name": "UKey-WiFi", "enabled": True}
        ]
    })
    assert resp.status_code == 200

    resp = client.post("/api/network/route-rules/apply")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["applied"] == 1
    assert body["results"][0]["resolved_ips"] == ["36.137.0.13"]
    assert body["results"][0]["stale_ips"] == ["36.137.0.12"]
    assert body["results"][0]["private_ok"] is True
    assert body["results"][0]["public_ok"] is True
    assert captured["rules"][0]["domain_pattern"] == "pm.gd.csg.cn"


@pytest.mark.integration
def test_station_profiles_are_masked_and_can_be_picked_by_name(client: TestClient):
    resp = client.put(
        "/api/settings",
        json={
            "settings": {
                "stationProfiles": json.dumps(
                    [
                        {
                            "id": "a",
                            "name": "阳江",
                            "wifiSsid": "YJ",
                            "wifiPassword": "wifi-secret",
                            "certPassword": "pin-secret",
                        },
                        {
                            "id": "b",
                            "name": "阳西",
                            "wifiSsid": "YX",
                            "wifiPassword": "wifi-2",
                            "certPassword": "pin-2",
                        },
                    ],
                    ensure_ascii=False,
                )
            }
        },
    )
    assert resp.status_code == 200

    saved = client.get("/api/settings").json()["settings"]["stationProfiles"]
    assert "wifi-secret" not in saved
    assert "pin-secret" not in saved

    resp = client.put(
        "/api/settings",
        json={
            "settings": {
                "stationProfiles": json.dumps(
                    [
                        {
                            "id": "a",
                            "name": "阳江",
                            "wifiSsid": "YJ-2",
                            "wifiPassword": "******",
                            "certPassword": "******",
                        }
                    ],
                    ensure_ascii=False,
                )
            }
        },
    )
    assert resp.status_code == 200
    kept = client.get("/api/settings").json()["settings"]["stationProfiles"]
    assert "YJ-2" in kept
    assert "wifi-secret" not in kept

    resp = client.post("/api/stations/pick", json={"choice": "阳江"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["station"]["wifiSsid"] == "YJ-2"
    assert "pin-secret" not in json.dumps(body, ensure_ascii=False)

    resp = client.post(
        "/api/stations/pick",
        json={"forceWindow": True, "useSupplied": True, "stations": [], "choice": "阳西海上风电"},
    )
    assert resp.status_code == 200
    assert resp.json()["demo"] is True
    assert resp.json()["station"]["wifiSsid"] == "YX-UKEY"


@pytest.mark.integration
def test_wifi_settings_keep_existing_password_when_blank(client: TestClient, mocker):
    captured: dict[str, object] = {}

    class FakeWifiConnector:
        def prepare(self, options):
            captured["password"] = options.password
            return WifiConnectResult(
                ok=True,
                target_ssid=options.ssid,
                current_ssid=options.ssid,
                message="已连接目标 WiFi",
            )

    mocker.patch("spider_gd.infrastructure.network.WifiConnector", FakeWifiConnector)
    resp = client.put(
        "/api/settings",
        json={
            "settings": {
                "wifiAutoConnectEnabled": "true",
                "wifiSsid": "CSG-UKEY",
                "wifiPassword": "secret-pass",
            }
        },
    )
    assert resp.status_code == 200

    resp = client.put("/api/settings", json={"settings": {"wifiPassword": ""}})
    assert resp.status_code == 200

    body = client.get("/api/settings").json()
    assert body["settings"]["wifiPassword"] == "******"
    resp = client.post("/api/network/wifi/test")
    assert resp.status_code == 200
    assert captured["password"] == "secret-pass"


@pytest.mark.integration
def test_wifi_test_endpoint_uses_saved_settings_without_returning_password(
    client: TestClient,
    mocker,
):
    captured: dict[str, object] = {}

    class FakeWifiConnector:
        def prepare(self, options):
            captured["ssid"] = options.ssid
            captured["password"] = options.password
            captured["interface_name"] = options.interface_name
            return WifiConnectResult(
                ok=True,
                target_ssid=options.ssid,
                current_ssid=options.ssid,
                interface_name=options.interface_name,
                platform="windows",
                message="已连接目标 WiFi: CSG-UKEY",
                details=["已连接目标 WiFi: CSG-UKEY"],
            )

    mocker.patch("spider_gd.infrastructure.network.WifiConnector", FakeWifiConnector)
    resp = client.put(
        "/api/settings",
        json={
            "settings": {
                "wifiAutoConnectEnabled": "true",
                "wifiSsid": "CSG-UKEY",
                "wifiPassword": "secret-pass",
                "wifiInterfaceName": "Wi-Fi",
                "wifiConnectTimeoutSec": "5",
            }
        },
    )
    assert resp.status_code == 200

    resp = client.post("/api/network/wifi/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "secret-pass" not in json.dumps(body, ensure_ascii=False)
    assert captured == {
        "ssid": "CSG-UKEY",
        "password": "secret-pass",
        "interface_name": "Wi-Fi",
    }


@pytest.mark.integration
def test_wifi_networks_endpoint_lists_available_ssids(client: TestClient, mocker):
    captured: dict[str, object] = {}

    class FakeWifiConnector:
        def list_networks(self, interface_name=""):
            captured["list_interface"] = interface_name
            return [
                WifiNetwork(
                    ssid="CSG-UKEY",
                    signal="86%",
                    security="WPA2-Personal",
                    current=True,
                ),
                WifiNetwork(ssid="Office WiFi", signal="60%", security="WPA3-Personal"),
            ]

        def current_ssid(self, interface_name=""):
            captured["current_interface"] = interface_name
            return "CSG-UKEY"

    mocker.patch("spider_gd.infrastructure.network.WifiConnector", FakeWifiConnector)

    resp = client.get("/api/network/wifi/networks?interface_name=Wi-Fi")

    assert resp.status_code == 200
    body = resp.json()
    assert body["current_ssid"] == "CSG-UKEY"
    assert body["networks"][0] == {
        "ssid": "CSG-UKEY",
        "signal": "86%",
        "security": "WPA2-Personal",
        "current": True,
    }
    assert body["networks"][1]["ssid"] == "Office WiFi"
    assert "password" not in json.dumps(body, ensure_ascii=False).lower()
    assert captured == {"list_interface": "Wi-Fi", "current_interface": "Wi-Fi"}


@pytest.mark.integration
def test_header_wifi_uses_connected_network_even_with_saved_interface(client: TestClient, mocker):
    class FakeWifiConnector:
        def current_ssid(self, interface_name=""):
            return "Office WiFi" if not interface_name else ""

    mocker.patch("spider_gd.infrastructure.network.WifiConnector", FakeWifiConnector)
    resp = client.put(
        "/api/settings", json={"settings": {"wifiInterfaceName": "UKey-WiFi"}}
    )
    assert resp.status_code == 200

    result = client.get("/api/network/wifi/current")

    assert result.status_code == 200
    assert result.json() == {"ssid": "Office WiFi", "error": ""}


@pytest.mark.integration
def test_delivery_test_endpoint_uses_saved_settings(client: TestClient, tmp_path: Path):
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    resp = client.put(
        "/api/settings",
        json={
            "settings": {
                "deliveryEnabled": "true",
                "deliveryWecomWebhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
                "deliveryFileDelivery": "remote_link",
                "deliveryRemoteUploadUrl": "https://files.example.com",
            }
        },
    )
    assert resp.status_code == 200

    resp = client.post("/api/delivery/test", json={"filePath": str(sample), "send": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert "远程上传后发送下载链接" in body["actions"]


@pytest.mark.integration
def test_execution_log_download_builds_one_zip(tmp_path: Path):
    from datetime import datetime

    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.file_dir = str(tmp_path / "logs")
    settings.log.persist_to_file = False
    log_dir = Path(settings.log.file_dir)
    log_dir.mkdir(parents=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (log_dir / "app.log").write_text(
        f"{now}.000 | INFO | run:save:1 | task | 本次执行标记\n"
        "2020-01-01 00:00:00.000 | INFO | run:save:1 | task | 很久以前\n",
        encoding="utf-8",
    )
    download_dir = tmp_path / "downloads"
    failure = download_dir / "_failures" / "login_error_test.log"
    failure.parent.mkdir(parents=True)
    failure.write_text("登录停在半登录\n", encoding="utf-8")
    page = download_dir / "_failures" / "page_step.html"
    page.write_text("<html>登录页</html>", encoding="utf-8")
    ctx = assemble(settings)
    try:
        ctx.state_store.save_settings({"certPassword": "SECRET", "workshopUploadToken": "TOKEN"})
        ctx.state_store.save_workflow_config({
            "id": "daily",
            "name": "诊断任务",
            "type": "indicator",
            "yaml_text": "name: 诊断任务\npassword: hunter2\nsteps: []\n",
        })
        ctx.state_store.create_execution_log(
            ExecutionLogRecord(
                exec_id="diag_001",
                name="诊断任务",
                type="scheduled_manual",
                target_id="daily",
                status="failed",
                reason="站点不可达",
                detail_log="步骤 1/2 失败: 站点不可达",
                created_at=now,
                updated_at=now,
            )
        )
        app = create_app(ControlService(ctx))
        client = TestClient(app)
        missing = client.post(
            "/api/execution-logs/diag_001/upload",
            json={"download_dir": str(download_dir)},
        )
        assert missing.status_code == 404

        resp = client.post(
            "/api/execution-logs/diag_001/download",
            json={"download_dir": str(download_dir)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["filename"].endswith(".zip")
        with zipfile.ZipFile(body["path"]) as archive:
            names = set(archive.namelist())
            assert "execution_log.txt" in names
            assert "app_log_window.txt" in names
            assert "network_diagnostics.txt" not in names
            assert "summary.json" in names
            assert "workflows/daily.yml" in names
            assert "runtime/_failures/login_error_test.log" in names
            assert "runtime/_failures/page_step.html" in names
            assert "站点不可达" in archive.read("execution_log.txt").decode("utf-8")
            window = archive.read("app_log_window.txt").decode("utf-8")
            assert "本次执行标记" in window
            assert "很久以前" not in window
            workflow = archive.read("workflows/daily.yml").decode("utf-8")
            assert "hunter2" not in workflow
            summary = json.loads(archive.read("summary.json").decode("utf-8"))
            assert "settings" not in summary
            assert summary["reason"] == "站点不可达"
            packed = "\n".join(
                archive.read(name).decode("utf-8", errors="replace") for name in names
            )
            assert "SECRET" not in packed and "TOKEN" not in packed
    finally:
        ctx.state_store.close()


@pytest.mark.integration
def test_delivery_test_endpoint_reports_missing_env_file(client: TestClient, tmp_path: Path):
    resp = client.put(
        "/api/settings",
        json={
            "settings": {
                "deliveryEnabled": "true",
                "deliveryEnvFile": str(tmp_path / "missing.env"),
            }
        },
    )
    assert resp.status_code == 200

    resp = client.post("/api/delivery/test", json={"send": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["env_file_error"] is True
    assert "env 文件不存在" in body["errors"][0]


@pytest.mark.integration
def test_delivery_test_endpoint_uses_env_file_when_saved_webhook_is_blank(
    client: TestClient,
    tmp_path: Path,
):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    resp = client.put(
        "/api/settings",
        json={
            "settings": {
                "deliveryEnabled": "true",
                "deliveryEnvFile": str(env_file),
                "deliveryBackend": "wecom_webhook",
                "deliveryWecomWebhook": "",
            }
        },
    )
    assert resp.status_code == 200

    resp = client.post("/api/delivery/test", json={"send": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["wecom_webhook_configured"] is True


@pytest.mark.integration
@respx.mock
def test_delivery_test_endpoint_file_send_uses_outbox_on_failure(tmp_path: Path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    settings.delivery.enabled = True
    settings.delivery.wecom_webhook = webhook
    settings.delivery.file_delivery = "wecom_file"
    settings.delivery.retry_attempts = 1
    settings.delivery.retry_delay_sec = 0
    ctx = assemble(settings)
    try:
        sample = tmp_path / "result.zip"
        sample.write_bytes(b"123456")
        respx.post(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
        ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
        send_count = {"n": 0}

        def send_handler(_request: httpx.Request) -> httpx.Response:
            send_count["n"] += 1
            if send_count["n"] == 2:
                return httpx.Response(500, json={"errcode": 1})
            return httpx.Response(200, json={"errcode": 0})

        respx.post(webhook).mock(side_effect=send_handler)
        app = create_app(ControlService(ctx))
        c = TestClient(app)

        resp = c.post("/api/delivery/test", json={"filePath": str(sample), "send": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["sent"] is False
        assert body["task_id"].startswith("DELIVERY_UI_TEST_")
        assert body["outbox"]["status"] == "pending"
        assert body["outbox"]["file_sent"] is True
        assert body["outbox"]["zip_size"] == 6
        assert body["outbox"]["zip_sha256"] == hashlib.sha256(b"123456").hexdigest()
        assert "任务产物通知发送失败" in body["outbox"]["last_error"]
        pending = ctx.state_store.list_delivery_outbox()
        assert pending[0]["task_id"].startswith("DELIVERY_UI_TEST_")
        assert pending[0]["file_sent"] == 1
        assert "任务产物通知发送失败" in pending[0]["last_error"]
    finally:
        ctx.state_store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_test_endpoint_returns_outbox_receipt_on_success(tmp_path: Path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    settings.delivery.enabled = True
    settings.delivery.wecom_webhook = webhook
    settings.delivery.file_delivery = "wecom_file"
    settings.delivery.retry_attempts = 1
    settings.delivery.retry_delay_sec = 0
    ctx = assemble(settings)
    try:
        sample = tmp_path / "result.zip"
        sample.write_bytes(b"123456")
        respx.post(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
        ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
        respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
        app = create_app(ControlService(ctx))
        c = TestClient(app)

        resp = c.post("/api/delivery/test", json={"filePath": str(sample), "send": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["sent"] is True
        assert body["task_id"].startswith("DELIVERY_UI_TEST_")
        assert body["outbox"]["status"] == "sent"
        assert body["outbox"]["delivery_note"] == "企业微信群机器人；企业微信文件直发；完成消息"
        assert body["outbox"]["zip_size"] == 6
        assert body["outbox"]["zip_sha256"] == hashlib.sha256(b"123456").hexdigest()
        assert body["outbox"]["delivered_at"]
    finally:
        ctx.state_store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_test_endpoint_sample_zip_generates_receipt(tmp_path: Path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    settings.delivery.enabled = True
    settings.delivery.wecom_webhook = webhook
    settings.delivery.file_delivery = "wecom_file"
    settings.delivery.retry_attempts = 1
    settings.delivery.retry_delay_sec = 0
    ctx = assemble(settings)
    try:
        respx.post(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
        ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
        respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
        app = create_app(ControlService(ctx))
        c = TestClient(app)

        resp = c.post("/api/delivery/test", json={"send": True, "sampleZip": True, "verify": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["sent"] is True
        assert body["task_id"].startswith("DELIVERY_UI_TEST_")
        assert body["outbox"]["status"] == "sent"
        assert body["outbox"]["delivery_note"] == "企业微信群机器人；企业微信文件直发；完成消息"
        assert "delivery_sample_" in body["outbox"]["zip_path"]
        assert body["outbox"]["zip_size"] > 0
        assert len(body["outbox"]["zip_sha256"]) == 64
        assert body["outbox"]["delivered_at"]
        assert body["verification"]["ok"] is True
        assert any(
            check["name"] == "local_zip_sha256" and check["ok"]
            for check in body["verification"]["checks"]
        )
    finally:
        ctx.state_store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_test_endpoint_force_remote_link_downloads_remote_zip(tmp_path: Path):
    from email.parser import BytesParser
    from email.policy import default

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    remote_url = "https://files.example.com/upload/publicData/ui_acceptance.zip"
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    settings.delivery.enabled = True
    settings.delivery.wecom_webhook = webhook
    settings.delivery.file_delivery = "wecom_file"
    settings.delivery.remote_upload_url = "https://files.example.com"
    settings.delivery.retry_attempts = 1
    settings.delivery.retry_delay_sec = 0
    ctx = assemble(settings)
    try:
        uploaded = {"content": b""}

        def upload_handler(request: httpx.Request) -> httpx.Response:
            content_type = request.headers["content-type"].encode("utf-8")
            msg = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + content_type + b"\r\n\r\n" + request.content
            )
            for part in msg.iter_parts():
                if part.get_filename():
                    uploaded["content"] = part.get_payload(decode=True) or b""
                    break
            return httpx.Response(200, json={"code": 200, "data": {"url": remote_url}})

        respx.post("https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData").mock(
            side_effect=upload_handler
        )
        respx.head(remote_url).mock(return_value=httpx.Response(200))
        respx.get(remote_url).mock(
            side_effect=lambda _request: httpx.Response(200, content=uploaded["content"])
        )
        respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
        app = create_app(ControlService(ctx))
        c = TestClient(app)

        resp = c.post(
            "/api/delivery/test",
            json={
                "send": True,
                "sampleZip": True,
                "verify": True,
                "forceRemoteLink": True,
                "requireRemoteUrl": True,
                "downloadRemote": True,
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["sent"] is True
        assert body["force_remote_link"] is True
        assert body["require_remote_url"] is True
        assert body["download_remote"] is True
        assert body["file_delivery"] == "remote_link"
        assert body["temporary_overrides"] == {"deliveryFileDelivery": "remote_link"}
        assert body["outbox"]["remote_url"] == remote_url
        assert body["outbox"]["forwarding"]["can_forward_link"] is True
        assert body["verification"]["ok"] is True
        assert any(
            check["name"] == "remote_zip_sha256" and check["ok"]
            for check in body["verification"]["checks"]
        )
        assert ctx.delivery_overrides is None
        assert ctx.settings.delivery.file_delivery == "wecom_file"
    finally:
        ctx.state_store.close()


@pytest.mark.integration
def test_delivery_outbox_endpoint_lists_pending(tmp_path: Path):
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    try:
        zip_path = tmp_path / "result.zip"
        zip_path.write_bytes(b"123456")
        ctx.state_store.enqueue_delivery_outbox(
            task_id="T_OUTBOX",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="send failed",
        )
        app = create_app(ControlService(ctx))
        c = TestClient(app)

        resp = c.get("/api/delivery/outbox")

        assert resp.status_code == 200
        body = resp.json()
        assert body["items"][0]["task_id"] == "T_OUTBOX"
        assert body["items"][0]["summary"]["success"] == 1
        assert body["items"][0]["forwarding"]["ready"] is False
        assert body["items"][0]["forwarding"]["can_forward_link"] is False
        assert body["summary"]["pending"] == 1

        remote_url = "https://files.example.com/result.zip"
        ctx.state_store.mark_delivery_outbox_remote_url("T_OUTBOX", remote_url)
        ctx.state_store.mark_delivery_outbox_delivered("T_OUTBOX")
        resp = c.get("/api/delivery/outbox?status=sent")
        assert resp.status_code == 200
        sent_item = resp.json()["items"][0]
        assert sent_item["task_id"] == "T_OUTBOX"
        assert sent_item["remote_url"] == remote_url
        assert sent_item["forwarding"]["ready"] is True
        assert sent_item["forwarding"]["can_forward_link"] is True
        assert sent_item["forwarding"]["remote_url"] == remote_url
        assert "zip_sha256" in sent_item["forwarding"]["instruction"]

        resp = c.post("/api/delivery/outbox/cleanup", json={"status": "sent", "limit": 10})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1

        ctx.state_store.enqueue_delivery_outbox(
            task_id="T_FAILED",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="send failed",
        )
        ctx.state_store.mark_delivery_outbox_failed("T_FAILED", "file missing", status="failed")
        resp = c.post("/api/delivery/outbox/requeue", json={"status": "failed", "limit": 10})
        assert resp.status_code == 200
        assert resp.json()["requeued"] == 1
        assert ctx.state_store.get_delivery_outbox_summary()["pending"] == 1
    finally:
        ctx.state_store.close()


@pytest.mark.integration
def test_delivery_verify_endpoint_checks_outbox_zip_hash(tmp_path: Path):
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    try:
        zip_path = tmp_path / "result.zip"
        payload = b"123456"
        zip_path.write_bytes(payload)
        ctx.state_store.enqueue_delivery_outbox(
            task_id="T_VERIFY_API",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="waiting",
        )
        ctx.state_store.mark_delivery_outbox_delivered(
            "T_VERIFY_API",
            "企业微信群机器人；完成消息",
        )
        app = create_app(ControlService(ctx))
        c = TestClient(app)

        resp = c.post("/api/delivery/verify", json={"taskId": "T_VERIFY_API"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["receipt"]["zip_size"] == len(payload)
        assert body["receipt"]["zip_sha256"] == hashlib.sha256(payload).hexdigest()
        assert any(check["name"] == "local_zip_sha256" and check["ok"] for check in body["checks"])
    finally:
        ctx.state_store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_verify_endpoint_can_download_remote_hash(tmp_path: Path):
    remote_url = "https://files.example.com/result.zip"
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    settings.delivery.retry_attempts = 1
    settings.delivery.retry_delay_sec = 0
    ctx = assemble(settings)
    try:
        payload = b"123456"
        zip_path = tmp_path / "result.zip"
        zip_path.write_bytes(payload)
        receipt_path = tmp_path / "receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "outbox": {
                        "task_id": "T_VERIFY_REMOTE_API",
                        "status": "sent",
                        "zip_path": str(zip_path),
                        "zip_size": len(payload),
                        "zip_sha256": hashlib.sha256(payload).hexdigest(),
                        "remote_url": remote_url,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        respx.head(remote_url).mock(return_value=httpx.Response(200))
        respx.get(remote_url).mock(return_value=httpx.Response(200, content=payload))
        app = create_app(ControlService(ctx))
        c = TestClient(app)

        resp = c.post(
            "/api/delivery/verify",
            json={"receiptPath": str(receipt_path), "downloadRemote": True},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert any(
            check["name"] == "remote_zip_sha256"
            and check["ok"]
            and check["actual"] == hashlib.sha256(payload).hexdigest()
            for check in body["checks"]
        )
    finally:
        ctx.state_store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_retry_endpoint_can_requeue_failed_records(tmp_path: Path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    settings.delivery.enabled = True
    settings.delivery.wecom_webhook = webhook
    settings.delivery.send_file = False
    settings.delivery.retry_attempts = 1
    settings.delivery.retry_delay_sec = 0
    ctx = assemble(settings)
    try:
        zip_path = tmp_path / "result.zip"
        zip_path.write_bytes(b"123456")
        ctx.state_store.enqueue_delivery_outbox(
            task_id="T_FAILED_RETRY",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="send failed",
        )
        ctx.state_store.mark_delivery_outbox_failed(
            "T_FAILED_RETRY",
            "network down",
            status="failed",
        )
        route = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
        app = create_app(ControlService(ctx))
        c = TestClient(app)

        resp = c.post("/api/delivery/retry", json={"limit": 10, "includeFailed": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["requeued_failed"] == 1
        assert body["sent"] == 1
        assert route.called
        assert ctx.state_store.get_delivery_outbox_summary()["sent"] == 1
    finally:
        ctx.state_store.close()


@pytest.mark.integration
def test_web_ui_disabled_by_default(client: TestClient):
    # 默认 create_app 不挂载 dashboard
    assert client.get("/").status_code == 404


@pytest.mark.integration
def test_web_ui_served_when_enabled(tmp_path: Path):
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    try:
        app = create_app(ControlService(ctx), enable_web_ui=True)
        c = TestClient(app)
        resp = c.get("/")
        assert resp.status_code == 200
        assert "广东电力交易数据采集系统" in resp.text
    finally:
        ctx.state_store.close()


@pytest.mark.integration
def test_step_stream_workflow_not_found(client: TestClient):
    """步骤播放：工作流不存在返回 404。"""
    resp = client.get("/api/workflows/nonexistent/step/1/run/stream")
    assert resp.status_code == 404


@pytest.mark.integration
def test_step_stream_browser_not_running(tmp_path: Path):
    """步骤播放：浏览器未启动时返回 SSE error 事件。"""
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    try:
        ctx.state_store.save_workflow_config({
            "id": "test_step",
            "name": "测试步骤",
            "type": "indicator",
            "needs_login": False,
            "start_from": "dashboard",
            "yaml_text": (
                "name: test\n"
                "type: indicator\n"
                "steps:\n"
                "- action: click\n"
                "  selector: text=something\n"
                "- action: wait\n"
                "  ms: 1000\n"
                "- action: export\n"
                "  selector: button:has-text('导出')\n"
                "  save_as: test_{date}.xlsx\n"
            ),
        })
        app = create_app(ControlService(ctx))
        c = TestClient(app)
        resp = c.get("/api/workflows/test_step/step/1/run/stream")
        assert resp.status_code == 200
        content = resp.text
        assert "共享浏览器未启动" in content
    finally:
        ctx.state_store.close()


@pytest.mark.integration
def test_indicator_step_playback_reuses_login_without_injecting_login_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    """已登录工作台播放指标首步时，不能重新导航到登录首页。"""
    import spider_gd.core.crawler.shared_browser as shared_browser
    import workflow_runner
    import workflow_types

    yaml_text = (
        "name: 场站指标\ntype: indicator\nneeds_login: true\n"
        "start_from: dashboard\nsteps:\n"
        "- action: navigate\n  url: https://pm.gd.csg.cn/portal/#/powerEnterprise\n"
        "- action: click\n  selector: text=现货交易\n"
    )
    assert client.put("/api/workflows/field_login_reuse", json={
        "name": "场站指标", "yaml_text": yaml_text,
    }).status_code == 200

    captured = {}

    class FakeRunner:
        def __init__(self, cfg, **_kwargs):
            captured["cfg"] = cfg

        async def run(self, on_event=None, **_kwargs):
            on_event({"event": "workflow_done"})

    class FakeSession:
        is_running = True

    monkeypatch.setattr(workflow_runner, "WorkflowRunner", FakeRunner)
    monkeypatch.setattr(shared_browser, "get_shared_browser", lambda: FakeSession())
    monkeypatch.setattr(workflow_types, "find_login_workflow", lambda _path: {
        "name": "01登录", "steps": [
            {"action": "login", "url": "https://pm.gd.csg.cn/portal/#/home"},
            {"action": "click", "selector": "button:has-text('登 录')"},
        ],
    })

    response = client.post("/api/workflows/field_login_reuse/step/1/run/stream", json={
        "mode": "from_here",
    })
    assert response.status_code == 200
    assert [step["action"] for step in captured["cfg"]["steps"]] == ["navigate", "click"]
    assert captured["cfg"]["needs_login"] is True

    response = client.get("/api/workflows/field_login_reuse/run/stream")
    assert response.status_code == 200
    assert [step["action"] for step in captured["cfg"]["steps"]] == ["navigate", "click"]
    assert captured["cfg"]["needs_login"] is True


@pytest.mark.integration
def test_step_stream_out_of_range(tmp_path: Path):
    """步骤播放：步骤索引超出范围返回 400。"""
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    try:
        ctx.state_store.save_workflow_config({
            "id": "test_range",
            "name": "测试范围",
            "type": "indicator",
            "needs_login": False,
            "start_from": "dashboard",
            "yaml_text": "name: test\ntype: indicator\nsteps:\n- action: click\n  selector: x\n",
        })
        app = create_app(ControlService(ctx))
        c = TestClient(app)
        resp = c.get("/api/workflows/test_range/step/99/run/stream")
        assert resp.status_code == 400
        assert "超出范围" in resp.json()["detail"]
    finally:
        ctx.state_store.close()
@pytest.mark.integration
def test_workflow_groups_keep_ids_and_archive(client: TestClient):
    parent = client.post('/api/workflow-groups', json={'name': '市场', 'parent_id': None}).json()['id']
    child = client.post('/api/workflow-groups', json={'name': '日前', 'parent_id': parent}).json()['id']
    assert client.put(f'/api/workflow-groups/{parent}', json={'name': '市场', 'parent_id': child}).status_code == 400

    yaml_text = 'name: 电量\ntype: indicator\nsteps: []\n'
    assert client.put('/api/workflows/volumes', json={'name': '电量', 'yaml_text': yaml_text, 'group_id': child}).status_code == 200
    assert client.patch('/api/workflows/volumes/meta', json={'label': '总电量', 'group_id': child}).status_code == 200
    listed = client.get('/api/workflows').json()
    assert listed[0]['name'] == 'volumes'
    assert listed[0]['label'] == '总电量'
    assert listed[0]['group_id'] == child
    assert client.delete(f'/api/workflow-groups/{child}').status_code == 409

    archive = client.get('/api/workflows/export-all')
    assert archive.status_code == 200
    result = client.post('/api/workflows/import-archive', json={'data': base64.b64encode(archive.content).decode()})
    assert result.status_code == 200
    assert result.json()['errors'] == []
    assert client.get('/api/workflows').json()[0]['group_id'] == child

    assert client.put('/api/task-configs/task-ref', json={'name': '引用任务', 'indicators': ['volumes'], 'enabled': False}).status_code == 200
    blocked = client.delete('/api/workflows/volumes')
    assert blocked.status_code == 409
    assert '引用任务' in blocked.json()['detail']


@pytest.mark.integration
def test_legacy_workflow_configs_migrate_to_root(tmp_path: Path):
    db_path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(db_path)
    conn.execute('''CREATE TABLE workflow_configs (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
        needs_login INTEGER NOT NULL, start_from TEXT NOT NULL,
        yaml_text TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
    conn.execute("INSERT INTO workflow_configs VALUES ('old', '旧指标', 'indicator', 1, 'dashboard', 'name: 旧指标', 0, 0)")
    conn.commit()
    conn.close()
    store = TaskStateStore(db_path)
    try:
        assert store.list_workflow_configs()[0]['group_id'] is None
        assert store.list_workflow_groups() == []
    finally:
        store.close()


@pytest.mark.integration
def test_task_path_preview_and_persistence(client: TestClient, tmp_path):
    resp = client.post(
        "/api/task-configs/preview-path",
        json={
            "template": "{YYYY-MM}/{YYYY-MM-DD}",
            "root": str(tmp_path / "data"),
            "exec_date": "2026-10-30",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] == ""
    assert body["relative"] == "2026-10/2026-10-30"
    assert body["path"].endswith("2026-10/2026-10-30")

    wifi = client.post(
        "/api/task-configs/preview-path",
        json={
            "template": "{SSID}/{YYYY-MM-DD}",
            "root": str(tmp_path / "data"),
            "exec_date": "2026-10-30",
            "ssid": "CSG-UKEY",
        },
    )
    assert wifi.json()["relative"] == "CSG-UKEY/2026-10-30"

    bad = client.post(
        "/api/task-configs/preview-path",
        json={"template": "../escape", "root": str(tmp_path), "exec_date": "2026-10-30"},
    )
    assert bad.status_code == 200
    assert "不能包含 .." in bad.json()["error"]

    saved = client.put(
        "/api/task-configs/t_path",
        json={
            "name": "路径测试",
            "indicators": [],
            "path_template": "{SSID}/{YYYY-MM}",
            "path_folders": [{"name": "阳江", "indicators": ["A"]}],
        },
    )
    assert saved.status_code == 200
    loaded = client.get("/api/task-configs/t_path").json()
    assert loaded["path_template"] == "{SSID}/{YYYY-MM}"
    assert loaded["path_folders"] == [
        {"id": "", "name": "阳江", "indicators": ["A"], "children": []}
    ]


@pytest.mark.integration
def test_task_download_directory_is_explicit_only(client: TestClient, tmp_path):
    old = client.put("/api/task-configs/legacy-root", json={
        "name": "旧任务", "indicators": [], "download_dir": str(tmp_path / "old-global"),
    })
    assert old.status_code == 200
    loaded = client.get("/api/task-configs/legacy-root").json()
    assert loaded["download_dir_custom"] is False

    custom = client.put("/api/task-configs/custom-root", json={
        "name": "独立目录任务", "indicators": [],
        "download_dir": str(tmp_path / "task-root"), "download_dir_custom": True,
    })
    assert custom.status_code == 200
    loaded = client.get("/api/task-configs/custom-root").json()
    assert loaded["download_dir_custom"] is True
    assert loaded["download_dir"] == str(tmp_path / "task-root")


@pytest.mark.integration
def test_export_all_task_configs_downloads_zip(client: TestClient):
    assert client.put("/api/task-configs/export-check", json={
        "name": "导出检查", "indicators": [],
    }).status_code == 200
    response = client.get("/api/task-configs/export-all")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"


@pytest.mark.integration
def test_task_structure_preview_and_persistence(client: TestClient, tmp_path):
    resp = client.post(
        "/api/task-configs/preview-structure",
        json={
            "structure": "- {wifi}\n  - 披露文件\n    - [A]\n  - 阳江\n    - [B]",
            "indicators": ["A", "B"],
            "root": str(tmp_path / "data"),
            "ssid": "CSG-UKEY",
            "exec_date": "2026-10-30",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] == ""
    assert body["base"].endswith("CSG-UKEY")
    by_indicator = {item["indicator"]: item for item in body["assignments"]}
    assert by_indicator["A"]["folder"] == "CSG-UKEY/披露文件"
    assert by_indicator["A"]["path"].endswith("CSG-UKEY/披露文件")
    assert by_indicator["B"]["folder"] == "CSG-UKEY/阳江"
    assert body["unassigned"] == []
    assert body["conflicts"] == []

    bad = client.post(
        "/api/task-configs/preview-structure",
        json={"structure": "- [未知]", "indicators": ["A"], "root": str(tmp_path)},
    )
    assert bad.status_code == 200
    # 失效标签不再把整个结构判为错误，单独列出来供弹窗标红。
    assert bad.json()["error"] == ""
    assert bad.json()["invalid"] == ["未知"]

    structure = "- {wifi}\n  - [A]"
    saved = client.put(
        "/api/task-configs/t_structure",
        json={"name": "结构测试", "indicators": ["A"], "path_structure": structure},
    )
    assert saved.status_code == 200
    loaded = client.get("/api/task-configs/t_structure").json()
    assert loaded["path_structure"] == structure


@pytest.mark.integration
def test_workflow_list_exposes_all_save_as_outputs(client: TestClient):
    yaml_text = """name: 多文件指标
type: indicator
steps:
  - action: export
    selector: button
    save_as: 文件A_{date}.xlsx
  - action: wait
    ms: 100
  - action: export
    selector: button
    save_as: 文件B_{date}.xlsx
  - action: extract_calendar
    selector: .calendar
    save_as: 交易日历{month_iso}.csv
"""
    resp = client.post(
        "/api/workflows/import",
        json={
            "files": [
                {"name": "多文件指标", "yaml_text": yaml_text},
            ]
        },
    )
    assert resp.status_code == 200
    workflows = client.get("/api/workflows").json()
    item = next(entry for entry in workflows if entry["name"] == "多文件指标")
    assert [output["label"] for output in item["outputs"]] == [
        "文件A_{date}.xlsx",
        "文件B_{date}.xlsx",
        "交易日历{month_iso}.csv",
    ]
    assert [output["key"] for output in item["outputs"]] == [
        "文件A_{date}.xlsx",
        "文件B_{date}.xlsx",
        "交易日历{month_iso}.csv",
    ]
    assert [output["position"] for output in item["outputs"]] == ["s0", "s2", "s3"]


@pytest.mark.integration
def test_workflow_list_marks_outputs_inside_day_loop(client: TestClient):
    yaml_text = """name: 现货循环
type: indicator
steps:
  - action: navigate
    url: https://example
  - action: export
    save_as: 前置.xlsx
  - action: pick_date
    value: yesterday
  - action: export
    save_as: 出清.xlsx
    name: 日前
  - action: export
    save_as: 出清.xlsx
    name: 实时
"""
    resp = client.post("/api/workflows/import", json={"files": [{"name": "现货循环", "yaml_text": yaml_text}]})
    assert resp.status_code == 200
    item = next(entry for entry in client.get("/api/workflows").json() if entry["name"] == "现货循环")
    assert [(output["key"], output["in_loop"]) for output in item["outputs"]] == [
        ("前置.xlsx", False), ("日前", True), ("实时", True),
    ]


@pytest.mark.integration
def test_task_output_windows_persist(client: TestClient):
    windows = {"A::日前": {"start": -3, "end": -1}}
    saved = client.put(
        "/api/task-configs/t_output_windows",
        json={"name": "导出范围", "indicators": ["A"], "output_windows": windows},
    )
    assert saved.status_code == 200
    assert client.get("/api/task-configs/t_output_windows").json()["output_windows"] == windows


SPOT_PROFILE_YAML = """name: 现货循环
type: indicator
steps:
  - action: navigate
    url: https://example
  - action: pick_date
    value: yesterday
  - action: export
    save_as: 出清.xlsx
    name: 日前
  - action: export
    save_as: 出清.xlsx
    name: 实时
"""


@pytest.mark.integration
def test_date_profile_writes_export_windows_onto_steps(client: TestClient):
    resp = client.post("/api/workflows/date-profile", json={
        "yaml_text": SPOT_PROFILE_YAML, "date_kind": "day", "start": -1, "end": -1,
        "output_windows": {"日前": [-3, -1], "实时": None},
    })
    assert resp.status_code == 200
    body = resp.json()
    loop = next(step for step in body["steps"] if step["action"] == "repeat")
    day_ahead = next(step for step in loop["steps"] if step.get("name") == "日前")
    assert (day_ahead["start"], day_ahead["end"]) == (-3, -1)
    assert [(o["key"], o["in_loop"], o["window"]) for o in body["outputs"]] == [
        ("日前", True, [-3, -1]), ("实时", True, None),
    ]


@pytest.mark.integration
def test_date_profile_read_does_not_rewrite_yaml(client: TestClient):
    resp = client.post("/api/workflows/date-profile/read", json={"yaml_text": SPOT_PROFILE_YAML})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "day"
    assert [(o["key"], o["in_loop"], o["window"]) for o in body["outputs"]] == [
        ("日前", True, None), ("实时", True, None),
    ]
    assert "yaml_text" not in body


@pytest.mark.integration
def test_execution_logs_batch_delete(tmp_path: Path):
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "spider.db")
    settings.storage.data_dir = str(tmp_path / "data")
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    try:
        for exec_id in ("log_a", "log_b", "log_c"):
            ctx.state_store.create_execution_log(ExecutionLogRecord(
                exec_id=exec_id, name=exec_id, type="workflow", target_id="x", status="success",
                created_at="2026-06-30 10:00:00", updated_at="2026-06-30 10:00:00",
            ))
        c = TestClient(create_app(ControlService(ctx)))
        resp = c.post("/api/execution-logs/batch-delete", json={"exec_ids": ["log_a", "log_c", "missing"]})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "deleted": 2}
        left = [item["exec_id"] for item in c.get("/api/execution-logs").json()["items"]]
        assert left == ["log_b"]
    finally:
        ctx.state_store.close()
