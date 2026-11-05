"""数据上传：配置加密、旧 token 迁移、自定义请求头和参数、重试和 WiFi。"""

from __future__ import annotations

import json
import zipfile
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
    UploadHeader,
    UploadParam,
    UploadProfile,
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
        "fileFieldName": "file",
        "headers": [
            {
                "id": "uh_auth",
                "name": "Authorization",
                "value": "Bearer secret-token",
                "secret": True,
            }
        ],
        "params": [],
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
    assert "Bearer secret-token" not in raw
    assert "wifi-secret" not in raw
    assert "tokenMode" not in raw
    loaded = load_profiles(store)
    assert loaded[0].headers[0].value == "Bearer secret-token"
    assert loaded[0].headers[0].secret is True
    assert loaded[0].wifi_password == "wifi-secret"
    public = public_profiles(loaded)[0]
    assert public["headers"][0]["value"] == ""
    assert public["headers"][0]["saved"] is True
    assert public["headers"][0]["secret"] is True
    assert "valueEnc" not in public["headers"][0]
    assert public["wifiPassword"] == ""
    assert public["wifiPasswordSaved"] is True
    assert "secret-token" not in json.dumps(public)

    public["name"] = "生产二"
    save_profiles(store, [public])
    again = load_profiles(store)[0]
    assert again.name == "生产二"
    assert again.headers[0].value == "Bearer secret-token"
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


def _zip_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as handle:
        return set(handle.namelist())


def test_prepare_single_file_is_not_zipped(tmp_path: Path) -> None:
    sample = tmp_path / "only.txt"
    sample.write_text("甲", encoding="utf-8")
    ready, failed = prepare_upload_files([sample, sample], tmp_path / "work")
    assert failed == []
    assert len(ready) == 1
    assert ready[0].kind == "file"
    assert ready[0].name == "only.txt"
    assert ready[0].path == sample
    assert ready[0].note == ""


def test_prepare_two_files_become_one_zip(tmp_path: Path) -> None:
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("甲", encoding="utf-8")
    second.write_text("乙", encoding="utf-8")
    ready, failed = prepare_upload_files([first, second], tmp_path / "work")
    assert failed == []
    assert len(ready) == 1
    assert ready[0].kind == "zip"
    assert ready[0].name == "one.txt_等2项.zip"
    assert ready[0].note == "已打包 2 项"
    assert _zip_names(ready[0].path) == {"one.txt", "two.txt"}


def test_prepare_nested_folder_one_zip_skips_internal_artifacts(tmp_path: Path) -> None:
    folder = tmp_path / "报表"
    folder.mkdir()
    (folder / "a.txt").write_text("甲", encoding="utf-8")
    nested = folder / "子目录"
    nested.mkdir()
    (nested / "b.txt").write_text("乙", encoding="utf-8")
    (folder / ".notes.pdf").write_bytes(b"dot-user")
    (folder / ".file_versions.json").write_text("{}", encoding="utf-8")
    (folder / ".file_versions.json.tmp").write_text("{}", encoding="utf-8")
    (folder / "storage_state.json").write_text("{}", encoding="utf-8")
    (folder / "extra.zip").write_bytes(b"PK")
    versions = folder / "_versions"
    versions.mkdir()
    (versions / "old.pdf").write_bytes(b"old")
    failures = folder / "_failures"
    failures.mkdir()
    (failures / "err.txt").write_text("x", encoding="utf-8")
    stable = folder / "_stable_delivery"
    stable.mkdir()
    (stable / "pack.zip").write_bytes(b"PK")

    ready, failed = prepare_upload_files([folder], tmp_path / "work")
    assert failed == []
    assert len(ready) == 1
    assert ready[0].kind == "zip"
    assert ready[0].name == "报表.zip"
    assert ready[0].note == "已打包 1 项"
    assert _zip_names(ready[0].path) == {"a.txt", "子目录/b.txt", ".notes.pdf"}


def test_prepare_file_and_folder_become_one_zip(tmp_path: Path) -> None:
    folder = tmp_path / "报表"
    folder.mkdir()
    (folder / "a.txt").write_text("甲", encoding="utf-8")
    nested = folder / "子目录"
    nested.mkdir()
    (nested / "b.txt").write_text("乙", encoding="utf-8")
    loose = tmp_path / "c.txt"
    loose.write_text("丙", encoding="utf-8")
    manifest = tmp_path / ".file_versions.json"
    manifest.write_text("{}", encoding="utf-8")
    chosen_zip = tmp_path / "附件.zip"
    chosen_zip.write_bytes(b"PK")

    ready, failed = prepare_upload_files(
        [folder, loose, manifest, chosen_zip, folder / "a.txt"],
        tmp_path / "work",
    )
    assert failed == []
    assert len(ready) == 1
    assert ready[0].kind == "zip"
    assert ready[0].name == "报表_等3项.zip"
    names = _zip_names(ready[0].path)
    assert names == {"报表/a.txt", "报表/子目录/b.txt", "c.txt", "附件.zip"}
    assert all(".file_versions.json" not in name for name in names)
    assert all("_versions" not in Path(name).parts for name in names)


def test_prepare_empty_and_missing_paths_fail(tmp_path: Path) -> None:
    ready, failed = prepare_upload_files([], tmp_path / "work")
    assert ready == []
    assert failed == []

    missing = tmp_path / "missing.txt"
    ready, failed = prepare_upload_files([missing], tmp_path / "work")
    assert ready == []
    assert failed[0]["status"] == "failed"
    assert "不存在" in failed[0]["message"]

    sample = tmp_path / "only.txt"
    sample.write_text("甲", encoding="utf-8")
    ready, failed = prepare_upload_files([sample, missing], tmp_path / "work")
    assert len(ready) == 1
    assert ready[0].kind == "file"
    assert ready[0].path == sample
    assert len(failed) == 1
    assert failed[0]["status"] == "failed"

    empty = tmp_path / "空目录"
    empty.mkdir()
    ready, failed = prepare_upload_files([empty], tmp_path / "work")
    assert ready == []
    assert failed[0]["status"] == "failed"
    assert "没有可上传的文件" in failed[0]["message"]

    artifact_only = tmp_path / "只有清单"
    artifact_only.mkdir()
    (artifact_only / ".file_versions.json").write_text("{}", encoding="utf-8")
    versions = artifact_only / "_versions"
    versions.mkdir()
    (versions / "old.pdf").write_bytes(b"old")
    ready, failed = prepare_upload_files([artifact_only], tmp_path / "work")
    assert ready == []
    assert "没有可上传的文件" in failed[0]["message"]


@pytest.mark.asyncio
@respx.mock
async def test_legacy_header_token_migrates_to_same_request(tmp_path: Path) -> None:
    sample = tmp_path / "a.txt"
    sample.write_text("data", encoding="utf-8")
    box = SecretBox(b"k" * 32)
    store = MemorySettings()
    store.values[SETTINGS_KEY] = json.dumps(
        [_legacy_row(box, tokenHeaderName="X-Token", tokenValuePrefix="Bearer ")]
    )
    loaded = load_profiles(store, box)[0]
    assert loaded.headers[0].name == "X-Token"
    assert loaded.headers[0].value == "Bearer secret-token"
    assert loaded.headers[0].secret is True
    assert loaded.params == []
    assert loaded.file_field_name == "file"
    assert loaded.wifi_password == "wifi-secret"

    route = respx.post("https://example.test/upload").mock(
        return_value=httpx.Response(200, json={"code": 200})
    )
    result = await upload_prepared_files(loaded, _only(sample), retry_delay_sec=0, max_attempts=1)
    assert result.ok is True
    assert route.calls.last.request.headers["X-Token"] == "Bearer secret-token"

    save_profiles(store, public_profiles([loaded]), box)
    raw = store.values[SETTINGS_KEY]
    assert "tokenMode" not in raw
    assert "tokenEnc" not in raw
    assert "tokenHeaderName" not in raw
    assert "secret-token" not in raw
    assert "Bearer secret-token" not in raw
    assert "wifi-secret" not in raw
    again = load_profiles(store, box)[0]
    assert again.headers[0].value == "Bearer secret-token"
    assert again.wifi_password == "wifi-secret"
    public = public_profiles([again])[0]
    assert public["headers"][0]["value"] == ""
    assert public["headers"][0]["saved"] is True
    assert "secret-token" not in json.dumps(public)
    assert "valueEnc" not in json.dumps(public)


@pytest.mark.asyncio
@pytest.mark.parametrize("param_in", ["query", "form"])
@respx.mock
async def test_legacy_param_token_migrates_to_same_request(tmp_path: Path, param_in: str) -> None:
    sample = tmp_path / "a.txt"
    sample.write_text("data", encoding="utf-8")
    box = SecretBox(b"k" * 32)
    store = MemorySettings()
    store.values[SETTINGS_KEY] = json.dumps(
        [
            _legacy_row(
                box,
                tokenMode="param",
                tokenParamName="access_token",
                tokenParamIn=param_in,
                wifiPasswordEnc="",
            )
        ]
    )
    loaded = load_profiles(store, box)[0]
    assert loaded.headers == []
    assert loaded.params[0].name == "access_token"
    assert loaded.params[0].value == "secret-token"
    assert loaded.params[0].location == param_in
    assert loaded.params[0].secret is True

    respx.post(url__startswith="https://example.test/upload").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    result = await upload_prepared_files(loaded, _only(sample), retry_delay_sec=0, max_attempts=1)
    assert result.ok is True
    sent = respx.calls.last.request
    if param_in == "query":
        assert sent.url.params["access_token"] == "secret-token"
        assert b"secret-token" not in sent_body(sent)
    else:
        body = sent_body(sent)
        assert b'name="access_token"' in body
        assert b"secret-token" in body
        assert "access_token" not in sent.url.params
    assert b'name="file"' in sent_body(sent)

    save_profiles(store, public_profiles([loaded]), box)
    raw = store.values[SETTINGS_KEY]
    assert "tokenMode" not in raw
    assert "secret-token" not in raw
    again = load_profiles(store, box)[0]
    assert again.params[0].value == "secret-token"
    assert again.params[0].location == param_in


def test_legacy_bearer_prefix_is_not_doubled() -> None:
    box = SecretBox(b"k" * 32)
    store = MemorySettings()
    legacy = _legacy_row(box, tokenEnc=box.encrypt("Bearer secret-token"))
    store.values[SETTINGS_KEY] = json.dumps([legacy])
    loaded = load_profiles(store, box)[0]
    assert loaded.headers[0].value == "Bearer secret-token"


def test_legacy_empty_token_adds_no_row() -> None:
    box = SecretBox(b"k" * 32)
    store = MemorySettings()
    store.values[SETTINGS_KEY] = json.dumps([_legacy_row(box, tokenEnc="")])
    loaded = load_profiles(store, box)[0]
    assert loaded.headers == []
    assert loaded.params == []
    assert loaded.wifi_password == "wifi-secret"


def test_save_legacy_payload_writes_new_schema() -> None:
    box = SecretBox(b"k" * 32)
    store = MemorySettings()
    save_profiles(
        store,
        [
            {
                "name": "旧",
                "url": "https://example.test/upload",
                "token": "secret-token",
                "tokenMode": "header",
                "tokenHeaderName": "Authorization",
                "tokenValuePrefix": "Bearer ",
            }
        ],
        box,
    )
    raw = store.values[SETTINGS_KEY]
    assert "tokenMode" not in raw
    assert "tokenEnc" not in raw
    assert "secret-token" not in raw
    loaded = load_profiles(store, box)[0]
    assert loaded.headers[0].name == "Authorization"
    assert loaded.headers[0].value == "Bearer secret-token"
    assert loaded.headers[0].secret is True
    assert loaded.file_field_name == "file"


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
    result = await run_upload(
        profile,
        [first, second],
        switch_wifi=False,
        retry_delay_sec=0,
        work_dir=tmp_path / "work",
    )
    assert calls["count"] == DEFAULT_ATTEMPTS
    assert result.ok is True
    assert [item["status"] for item in result.files] == ["success"]
    assert result.files[0]["name"] == "one.txt_等2项.zip"
    assert "已打包 2 项" in result.files[0]["message"]
    body = sent_body(respx.calls.last.request)
    assert body.count(b'name="file"') == 1
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
    profile = UploadProfile(
        "up_1",
        "甲",
        "https://example.test/upload",
        headers=[UploadHeader("Authorization", "Bearer secret-token", True, "uh_1")],
        wifi_ssid="office",
        wifi_password="wifi-secret",
    )
    port = FakeWifi("office")
    events: list[dict] = []
    result = await run_upload(
        profile,
        [folder],
        wifi=port,
        retry_delay_sec=0,
        max_attempts=1,
        work_dir=tmp_path / "zip",
        on_progress=events.append,
    )
    assert result.ok is True
    assert port.calls == []
    assert result.wifi_message == "已连接目标 WiFi"
    assert len(result.files) == 1
    assert result.files[0]["name"] == "目录.zip"
    assert "已打包 1 项" in result.files[0]["message"]
    done = [item for item in events if item["phase"] == "done"][-1]
    assert done["total"] == 1
    assert done["done"] == 1
    assert len(done["files"]) == 1
    body = sent_body(respx.calls.last.request)
    assert body.count(b'name="file"') == 1
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
    body = sent_body(respx.calls.last.request)
    assert b'filename="out.txt"' in body
    assert b"new" in body
    detail = store.get_execution_log("exec-1")
    assert detail is not None
    assert "准备重试 1/3" in detail.detail_log
    assert "上传成功：out.txt" in detail.detail_log
    store.close()


@pytest.mark.asyncio
@respx.mock
async def test_maybe_upload_after_run_packs_changed_files_into_one_zip(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path / "spider.db")
    save_profiles(store, [_profile_payload(wifiSsid="", wifiPassword="")])
    download = tmp_path / "downloads"
    download.mkdir()
    (download / "one.txt").write_text("甲", encoding="utf-8")
    (download / "two.txt").write_text("乙", encoding="utf-8")
    (download / ".file_versions.json").write_text("{}", encoding="utf-8")
    versions = download / "_versions"
    versions.mkdir()
    (versions / "old.txt").write_text("old", encoding="utf-8")
    from spider_gd.interfaces.storage import ExecutionLogRecord

    store.create_execution_log(
        ExecutionLogRecord(
            exec_id="exec-zip",
            name="日报",
            type="scheduled_manual",
            target_id="task-1",
            status="success",
            created_at="2026-01-01 00:00:00",
            updated_at="2026-01-01 00:00:00",
        )
    )
    respx.post("https://example.test/upload").mock(
        return_value=httpx.Response(200, json={"code": 200})
    )
    events: list[dict] = []
    offer = await maybe_upload_after_run(
        store,
        profile_id="up_test",
        auto_upload=True,
        exec_id="exec-zip",
        download_dir=str(download),
        before={},
        wifi=FakeWifi(""),
        retry_delay_sec=0,
        on_event=events.append,
    )
    assert offer is not None
    assert offer["ok"] is True
    body = sent_body(respx.calls.last.request)
    assert body.count(b'name="file"') == 1
    assert "one.txt_等2项.zip".encode() in body
    assert b"one.txt" in body
    assert b"two.txt" in body
    assert b".file_versions.json" not in body
    assert b"old.txt" not in body
    done = [item for item in events if item.get("phase") == "done"][-1]
    assert done["total"] == 1
    assert done["done"] == 1
    assert done["files"][0]["status"] == "success"
    assert "已打包 2 项" in done["files"][0]["message"]
    detail = store.get_execution_log("exec-zip")
    assert detail is not None
    assert "选中 2 项" in detail.detail_log
    assert "one.txt_等2项.zip" in detail.detail_log
    store.close()


@pytest.mark.asyncio
@respx.mock
async def test_mixed_headers_and_params_build_one_request(tmp_path: Path) -> None:
    sample = tmp_path / "a.txt"
    sample.write_text("data", encoding="utf-8")
    profile = UploadProfile(
        "up_1",
        "甲",
        "https://example.test/upload?keep=1&access_token=old",
        headers=[
            UploadHeader("X-Trace", "trace-1", False, "uh_trace"),
            UploadHeader("X-Token", "secret-token", True, "uh_token"),
        ],
        params=[
            UploadParam("access_token", "new-token", "query", True, "upm_q"),
            UploadParam("kind", "daily", "form", False, "upm_f"),
            UploadParam("access_token", "form-token", "form", False, "upm_same"),
        ],
    )
    respx.post(url__startswith="https://example.test/upload").mock(
        return_value=httpx.Response(200, json={"code": 200})
    )
    result = await upload_prepared_files(profile, _only(sample), retry_delay_sec=0, max_attempts=1)
    assert result.ok is True
    sent = respx.calls.last.request
    assert sent.headers["X-Trace"] == "trace-1"
    assert sent.headers["X-Token"] == "secret-token"
    assert sent.url.params["keep"] == "1"
    assert sent.url.params["access_token"] == "new-token"
    body = sent_body(sent)
    assert b'name="kind"' in body
    assert b"daily" in body
    assert b'name="access_token"' in body
    assert b"form-token" in body
    assert b'name="file"' in body


@pytest.mark.asyncio
@respx.mock
async def test_file_field_name_follows_profile(tmp_path: Path) -> None:
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    profile = UploadProfile(
        "up_1",
        "甲",
        "https://example.test/upload",
        file_field_name="attachment",
    )
    respx.post("https://example.test/upload").mock(
        return_value=httpx.Response(200, json={"code": 200})
    )
    result = await run_upload(
        profile,
        [first, second],
        switch_wifi=False,
        retry_delay_sec=0,
        max_attempts=1,
        work_dir=tmp_path / "work",
    )
    assert result.ok is True
    assert len(result.files) == 1
    assert result.files[0]["name"].endswith(".zip")
    body = sent_body(respx.calls.last.request)
    assert body.count(b'name="attachment"') == 1
    assert b'name="file"' not in body


def test_duplicate_names_and_file_field_conflict_are_rejected() -> None:
    box = SecretBox(b"k" * 32)
    store = MemorySettings()
    with pytest.raises(ValueError, match="请求头名称重复"):
        save_profiles(
            store,
            [
                _profile_payload(
                    headers=[
                        {"name": "X-Token", "value": "a", "secret": False},
                        {"name": "x-token", "value": "b", "secret": False},
                    ]
                )
            ],
            box,
        )
    with pytest.raises(ValueError, match="表单参数名称重复"):
        save_profiles(
            store,
            [
                _profile_payload(
                    headers=[],
                    params=[
                        {"name": "kind", "value": "a", "in": "form", "secret": False},
                        {"name": "kind", "value": "b", "in": "form", "secret": False},
                    ],
                )
            ],
            box,
        )
    with pytest.raises(ValueError, match="文件字段名冲突"):
        save_profiles(
            store,
            [
                _profile_payload(
                    headers=[],
                    fileFieldName="file",
                    params=[{"name": "file", "value": "x", "in": "form", "secret": False}],
                )
            ],
            box,
        )
    with pytest.raises(ValueError, match="由上传请求自动生成"):
        save_profiles(
            store,
            [
                _profile_payload(
                    headers=[{"name": "Content-Type", "value": "text/plain", "secret": False}]
                )
            ],
            box,
        )


def test_public_api_hides_secrets_and_keeps_plaintext(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path / "spider.db")
    save_profiles(
        store,
        [
            _profile_payload(
                wifiSsid="",
                wifiPassword="",
                headers=[
                    {
                        "id": "uh_auth",
                        "name": "Authorization",
                        "value": "Bearer secret-token",
                        "secret": True,
                    },
                    {
                        "id": "uh_trace",
                        "name": "X-Trace",
                        "value": "visible-trace",
                        "secret": False,
                    },
                ],
                params=[
                    {
                        "id": "upm_q",
                        "name": "access_token",
                        "value": "query-secret",
                        "in": "query",
                        "secret": True,
                    },
                    {
                        "id": "upm_f",
                        "name": "kind",
                        "value": "daily",
                        "in": "form",
                        "secret": False,
                    },
                ],
            )
        ],
    )
    raw = store.get_setting(SETTINGS_KEY) or ""
    assert "secret-token" not in raw
    assert "query-secret" not in raw
    assert "visible-trace" in raw
    assert "daily" in raw
    loaded = load_profiles(store)[0]
    public = public_profiles([loaded])[0]
    by_name = {item["name"]: item for item in public["headers"]}
    assert by_name["Authorization"]["value"] == ""
    assert by_name["Authorization"]["saved"] is True
    assert by_name["X-Trace"]["value"] == "visible-trace"
    assert by_name["X-Trace"]["secret"] is False
    params = {item["name"]: item for item in public["params"]}
    assert params["access_token"]["value"] == ""
    assert params["access_token"]["saved"] is True
    assert params["kind"]["value"] == "daily"
    dumped = json.dumps(public)
    assert "query-secret" not in dumped
    assert "secret-token" not in dumped
    assert "valueEnc" not in dumped

    save_profiles(store, [public])
    again = load_profiles(store)[0]
    assert again.headers[0].value == "Bearer secret-token"
    assert again.params[0].value == "query-secret"
    assert "query-secret" not in (store.get_setting(SETTINGS_KEY) or "")
    store.close()


def test_blank_secret_survives_rename_and_clearing_secret_drops_it() -> None:
    box = SecretBox(b"k" * 32)
    store = MemorySettings()
    save_profiles(store, [_profile_payload(wifiSsid="", wifiPassword="")], box)
    public = public_profiles(load_profiles(store, box))[0]
    public["headers"][0]["name"] = "X-Token"
    save_profiles(store, [public], box)
    renamed = load_profiles(store, box)[0]
    assert renamed.headers[0].name == "X-Token"
    assert renamed.headers[0].value == "Bearer secret-token"

    public = public_profiles([renamed])[0]
    public["headers"][0]["secret"] = False
    public["headers"][0]["value"] = ""
    save_profiles(store, [public], box)
    cleared = load_profiles(store, box)[0]
    assert cleared.headers[0].secret is False
    assert cleared.headers[0].value == ""
    raw = store.values[SETTINGS_KEY]
    assert "secret-token" not in raw
    assert "valueEnc" not in raw


def _legacy_row(box: SecretBox, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "up_legacy",
        "name": "旧配置",
        "url": "https://example.test/upload",
        "tokenEnc": box.encrypt("secret-token"),
        "tokenMode": "header",
        "tokenHeaderName": "Authorization",
        "tokenValuePrefix": "Bearer ",
        "tokenParamName": "token",
        "tokenParamIn": "query",
        "wifiSsid": "office",
        "wifiPasswordEnc": box.encrypt("wifi-secret"),
        "wifiInterface": "",
    }
    row.update(overrides)
    return row


def load_profiles_from(box: SecretBox, **overrides: object):
    store = MemorySettings()
    save_profiles(store, [_profile_payload(**overrides)], box)
    return load_profiles(store, box)[0]


def _only(path: Path):
    from spider_gd.gateway.data_uploader import PreparedFile

    return [PreparedFile(str(path), path.name, path, "file")]


def sent_body(request: httpx.Request) -> bytes:
    return request.content
