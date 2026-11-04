"""数据上传：配置加密、token 传递、文件夹压缩、重试和 WiFi 跳过。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from spider_gd.gateway.data_upload_flow import maybe_upload_after_run
from spider_gd.gateway.data_uploader import (
    DEFAULT_ATTEMPTS,
    prepare_upload_files,
    probe_wifi_switch,
    run_upload,
    switch_to_upload_wifi,
    upload_prepared_files,
)
from spider_gd.gateway.secret_box import SecretBox
from spider_gd.gateway.upload_profiles import (
    SETTINGS_KEY,
    load_profiles,
    public_profiles,
    save_profiles,
)
from spider_gd.infrastructure.network.wifi_connector import WifiConnectOptions, WifiConnectResult
from spider_gd.infrastructure.storage.database import TaskStateStore


class MemorySettings:
    """最小设置存储，给配置读写测试用。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_setting(self, key: str) -> str:
        return self.values.get(key, "")

    def save_settings(self, settings: dict[str, str]) -> None:
        self.values.update(settings)


class FakeWifi:
    """不碰真实网卡的 WiFi 端口。"""

    def __init__(self, current: str) -> None:
        self.current = current
        self.calls: list[WifiConnectOptions] = []

    def current_ssid(self, interface_name: str = "") -> str:
        _ = interface_name
        return self.current

    def connect(self, options: WifiConnectOptions) -> WifiConnectResult:
        self.calls.append(options)
        self.current = options.ssid
        return WifiConnectResult(
            ok=True,
            target_ssid=options.ssid,
            current_ssid=options.ssid,
            changed=True,
            message=f"已切换到 {options.ssid}",
        )


def _profile_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "up_test",
        "name": "生产",
        "url": "https://example.test/upload",
        "token": "secret-token",
        "tokenMode": "header",
        "tokenHeaderName": "Authorization",
        "tokenValuePrefix": "Bearer ",
        "wifiSsid": "office",
        "wifiPassword": "wifi-secret",
    }
    payload.update(overrides)
    return payload


def test_profiles_roundtrip_encrypts_secrets(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path / "spider.db")
    save_profiles(store, [_profile_payload()])
    raw = store.get_setting(SETTINGS_KEY) or ""
    assert "secret-token" not in raw
    assert "wifi-secret" not in raw
    loaded = load_profiles(store)
    assert loaded[0].token == "secret-token"
    assert loaded[0].wifi_password == "wifi-secret"
    public = public_profiles(loaded)[0]
    assert public["token"] == ""
    assert public["tokenSaved"] is True
    assert public["wifiPassword"] == ""
    assert public["wifiPasswordSaved"] is True

    save_profiles(store, [_profile_payload(token="", wifiPassword="", name="生产二")])
    again = load_profiles(store)[0]
    assert again.name == "生产二"
    assert again.token == "secret-token"
    assert again.wifi_password == "wifi-secret"
    assert "secret-token" not in (store.get_setting(SETTINGS_KEY) or "")
    store.close()


def test_task_config_stores_upload_fields(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path / "spider.db")
    store.save_task_config(
        {
            "id": "task-1",
            "name": "日报",
            "indicators": ["spot"],
            "upload_profile_id": "up_test",
            "auto_upload": True,
        }
    )
    loaded = store.get_task_config("task-1")
    assert loaded is not None
    assert loaded["upload_profile_id"] == "up_test"
    assert loaded["auto_upload"] is True
    store.save_task_config({**loaded, "auto_upload": False, "upload_profile_id": ""})
    updated = store.get_task_config("task-1")
    assert updated is not None
    assert updated["auto_upload"] is False
    assert updated["upload_profile_id"] == ""
    store.close()


def test_prepare_upload_files_zips_directory(tmp_path: Path) -> None:
    folder = tmp_path / "报表"
    folder.mkdir()
    (folder / "a.txt").write_text("甲", encoding="utf-8")
    nested = folder / "子目录"
    nested.mkdir()
    (nested / "b.txt").write_text("乙", encoding="utf-8")
    loose = tmp_path / "c.txt"
    loose.write_text("丙", encoding="utf-8")
    ready, failed = prepare_upload_files([folder, loose, tmp_path / "missing.txt"], tmp_path / "work")
    assert failed[0]["status"] == "failed"
    zipped = next(item for item in ready if item.kind == "zip")
    plain = next(item for item in ready if item.kind == "file")
    assert plain.name == "c.txt"
    import zipfile

    with zipfile.ZipFile(zipped.path) as handle:
        names = set(handle.namelist())
    assert "a.txt" in names
    assert "子目录/b.txt" in names


@pytest.mark.asyncio
@respx.mock
async def test_token_header_and_param_modes(tmp_path: Path) -> None:
    sample = tmp_path / "a.txt"
    sample.write_text("data", encoding="utf-8")
    box = SecretBox(b"k" * 32)
    header_profile = load_profiles_from(box, tokenMode="header", tokenHeaderName="X-Token", tokenValuePrefix="Bearer ")
    route = respx.post("https://example.test/upload").mock(return_value=httpx.Response(200, json={"code": 200}))
    result = await upload_prepared_files(header_profile, _only(sample), retry_delay_sec=0, max_attempts=1)
    assert result.ok is True
    assert route.calls.last.request.headers["X-Token"] == "Bearer secret-token"

    query_profile = load_profiles_from(box, tokenMode="param", tokenParamName="access_token", tokenParamIn="query")
    respx.post(url__startswith="https://example.test/upload").mock(return_value=httpx.Response(200, json={"ok": True}))
    result = await upload_prepared_files(query_profile, _only(sample), retry_delay_sec=0, max_attempts=1)
    assert result.ok is True
    assert result.attempts == 1
    sent = respx.calls.last.request
    assert sent.url.params["access_token"] == "secret-token"
    assert "X-Token" not in sent.headers

    form_profile = load_profiles_from(box, tokenMode="param", tokenParamName="token", tokenParamIn="form")
    respx.post("https://example.test/upload").mock(return_value=httpx.Response(200, json={"ok": True}))
    result = await upload_prepared_files(form_profile, _only(sample), retry_delay_sec=0, max_attempts=1)
    assert result.ok is True
    body = sent_body(respx.calls.last.request)
    assert b'name="token"' in body
    assert b"secret-token" in body
    assert b'name="file"' in body


@pytest.mark.asyncio
@respx.mock
async def test_multipart_accepts_multiple_files_and_retries(tmp_path: Path) -> None:
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < DEFAULT_ATTEMPTS:
            return httpx.Response(500, json={"code": 500, "message": "busy"})
        return httpx.Response(200, json={"code": 200})

    respx.post("https://example.test/upload").mock(side_effect=handler)
    profile = load_profiles_from(SecretBox(b"k" * 32), token="")
    result = await upload_prepared_files(
        profile,
        [*_only(first), *_only(second)],
        retry_delay_sec=0,
    )
    assert calls["count"] == DEFAULT_ATTEMPTS
    assert result.ok is True
    assert {item["status"] for item in result.files} == {"success"}
    body = sent_body(respx.calls.last.request)
    assert body.count(b'name="file"') == 2
    assert b"one.txt" in body
    assert b"two.txt" in body


@pytest.mark.asyncio
@respx.mock
async def test_retry_stops_after_three_extra_attempts(tmp_path: Path) -> None:
    sample = tmp_path / "a.txt"
    sample.write_bytes(b"x")
    route = respx.post("https://example.test/upload").mock(
        return_value=httpx.Response(502, json={"message": "down"})
    )
    profile = load_profiles_from(SecretBox(b"k" * 32), token="")
    result = await upload_prepared_files(profile, _only(sample), retry_delay_sec=0)
    assert route.call_count == DEFAULT_ATTEMPTS
    assert result.ok is False
    assert result.files[0]["status"] == "failed"
    assert "down" in result.message


def test_wifi_skips_when_already_connected() -> None:
    from spider_gd.gateway.upload_profiles import UploadProfile

    port = FakeWifi("office")
    profile = UploadProfile("up_1", "甲", "https://example.test/upload", wifi_ssid="office", wifi_password="secret")
    hop = switch_to_upload_wifi(profile, port)
    assert hop.already_connected is True
    assert hop.message == "已连接目标 WiFi"
    assert port.calls == []
    report = probe_wifi_switch(profile, port)
    assert report["alreadyConnected"] is True
    assert report["message"] == "已连接目标 WiFi"
    assert port.calls == []


def test_wifi_switches_and_restores() -> None:
    from spider_gd.gateway.upload_profiles import UploadProfile

    port = FakeWifi("home")
    profile = UploadProfile("up_1", "甲", "https://example.test/upload", wifi_ssid="office", wifi_password="secret")
    report = probe_wifi_switch(profile, port)
    assert report["ok"] is True
    assert report["alreadyConnected"] is False
    assert [item.ssid for item in port.calls] == ["office", "home"]
    assert port.calls[0].password == "secret"
    assert port.calls[1].password == ""
    assert port.current == "home"


@pytest.mark.asyncio
@respx.mock
async def test_run_upload_zips_folder_and_skips_connected_wifi(tmp_path: Path) -> None:
    folder = tmp_path / "目录"
    folder.mkdir()
    (folder / "inside.txt").write_text("内容", encoding="utf-8")
    respx.post("https://example.test/upload").mock(return_value=httpx.Response(200, json={"code": 0}))
    from spider_gd.gateway.upload_profiles import UploadProfile

    profile = UploadProfile(
        "up_1",
        "甲",
        "https://example.test/upload",
        token="secret-token",
        wifi_ssid="office",
        wifi_password="wifi-secret",
    )
    port = FakeWifi("office")
    result = await run_upload(
        profile,
        [folder],
        wifi=port,
        retry_delay_sec=0,
        max_attempts=1,
        work_dir=tmp_path / "zip",
    )
    assert result.ok is True
    assert port.calls == []
    assert result.wifi_message == "已连接目标 WiFi"
    assert result.files[0]["name"] == "目录.zip"
    body = sent_body(respx.calls.last.request)
    assert b"inside.txt" in body


@pytest.mark.asyncio
@respx.mock
async def test_maybe_upload_after_run_manual_offer_and_auto_retry(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path / "spider.db")
    save_profiles(store, [_profile_payload(wifiSsid="", wifiPassword="")])
    download = tmp_path / "downloads"
    download.mkdir()
    target = download / "out.txt"
    target.write_text("new", encoding="utf-8")
    before: dict[str, tuple[int, int]] = {}

    from spider_gd.interfaces.storage import ExecutionLogRecord

    store.create_execution_log(
        ExecutionLogRecord(
            exec_id="exec-1",
            name="日报",
            type="scheduled_manual",
            target_id="task-1",
            status="success",
            created_at="2026-01-01 00:00:00",
            updated_at="2026-01-01 00:00:00",
        )
    )
    offer = await maybe_upload_after_run(
        store,
        profile_id="up_test",
        auto_upload=False,
        exec_id="exec-1",
        download_dir=str(download),
        before=before,
        retry_delay_sec=0,
    )
    assert offer is not None
    assert offer["auto"] is False
    assert str(target) in offer["paths"]
    detail = store.get_execution_log("exec-1")
    assert detail is not None
    assert "手动上传" in detail.detail_log
    assert "secret-token" not in detail.detail_log

    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"message": "later"})
        return httpx.Response(200, json={"code": 200})

    respx.post("https://example.test/upload").mock(side_effect=handler)
    auto = await maybe_upload_after_run(
        store,
        profile_id="up_test",
        auto_upload=True,
        exec_id="exec-1",
        download_dir=str(download),
        before=before,
        wifi=FakeWifi(""),
        retry_delay_sec=0,
    )
    assert auto is not None and auto["ok"] is True
    assert calls["count"] == 2
    detail = store.get_execution_log("exec-1")
    assert detail is not None
    assert "准备重试 1/3" in detail.detail_log
    assert "上传成功" in detail.detail_log
    store.close()


def load_profiles_from(box: SecretBox, **overrides: object):
    store = MemorySettings()
    save_profiles(store, [_profile_payload(**overrides)], box)
    return load_profiles(store, box)[0]


def _only(path: Path):
    from spider_gd.gateway.data_uploader import PreparedFile

    return [PreparedFile(str(path), path.name, path, "file")]


def sent_body(request: httpx.Request) -> bytes:
    return request.content
