from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from spider_gd.app import (
    _build_delivery_hook,
    _build_reliable_delivery_hook,
    _delivery_identity,
    build_delivery_test_plan,
    retry_pending_deliveries,
)
from spider_gd.config.settings import AppSettings, DeliverySettings
from spider_gd.infrastructure.storage.database import TaskStateStore


class _StateSettings:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def get_setting(self, key: str) -> str | None:
        return self.values.get(key)


def _mock_remote_file_ok(url: str) -> None:
    respx.head(url).mock(return_value=httpx.Response(200))


@pytest.mark.unit
def test_delivery_identity_is_hashed_and_does_not_expose_credentials():
    secrets = [
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        "SCTSECRET",
        "openid-123",
        "pushplus-secret",
        "https://files.example.com/upload?token=raw",
    ]
    settings_by_backend = [
        AppSettings(
            delivery=DeliverySettings(
                enabled=True,
                backend="wecom_webhook",
                wecom_webhook=secrets[0],
                remote_upload_url=secrets[4],
            )
        ),
        AppSettings(
            delivery=DeliverySettings(
                enabled=True,
                backend="serverchan",
                serverchan_send_key=secrets[1],
                serverchan_openid=secrets[2],
                remote_upload_url=secrets[4],
            )
        ),
        AppSettings(
            delivery=DeliverySettings(
                enabled=True,
                backend="pushplus",
                pushplus_token=secrets[3],
                remote_upload_url=secrets[4],
            )
        ),
    ]

    identities = [
        _delivery_identity(SimpleNamespace(settings=settings))
        for settings in settings_by_backend
    ]

    assert len(set(identities)) == len(identities)
    for identity in identities:
        assert len(identity) == 64
        assert all(ch in "0123456789abcdef" for ch in identity)
        for secret in secrets:
            assert secret not in identity
        assert "key=abc" not in identity
        assert "token=raw" not in identity


@pytest.mark.unit
def test_build_delivery_hook_returns_none_when_disabled():
    ctx = SimpleNamespace(settings=AppSettings())

    assert _build_delivery_hook(ctx) is None


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_sends_wecom_message_and_file(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            title="采集结果",
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    upload = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T1", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert upload.called
    assert send.call_count == 2
    assert send.calls[0].request.content == b'{"msgtype":"file","file":{"media_id":"MEDIA"}}'
    assert b'"msgtype":"markdown"' in send.calls[1].request.content
    assert b"SHA256" in send.calls[1].request.content
    assert hashlib.sha256(b"123456").hexdigest().encode() in send.calls[1].request.content


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_reads_configured_env_file_for_unattended_send(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=False,
            env_file=str(env_file),
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    payloads: list[dict[str, object]] = []

    def send_handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    respx.post(webhook).mock(side_effect=send_handler)
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    await hook("T_ENV_FILE", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert payloads
    assert payloads[0]["msgtype"] == "markdown"
    assert "T_ENV_FILE" in payloads[0]["markdown"]["content"]


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_reads_process_delivery_env_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SPIDER_GD_DELIVERY_ENV_FILE", str(env_file))
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=False,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    await hook("T_PROCESS_ENV_FILE", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert send.called


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_reads_state_delivery_env_file(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=False,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    ctx = SimpleNamespace(
        settings=settings,
        state_store=_StateSettings({"deliveryEnvFile": str(env_file)}),
    )
    hook = _build_delivery_hook(ctx)
    assert hook is not None

    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    await hook("T_STATE_ENV_FILE", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert send.called


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_uses_env_file_when_saved_secret_field_is_blank(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=False,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    ctx = SimpleNamespace(
        settings=settings,
        state_store=_StateSettings(
            {
                "deliveryEnabled": "true",
                "deliveryEnvFile": str(env_file),
                "deliveryBackend": "wecom_webhook",
                "deliveryWecomWebhook": "",
            }
        ),
    )
    hook = _build_delivery_hook(ctx)
    assert hook is not None

    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    await hook("T_BLANK_SECRET", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert send.called


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_does_not_send_message_when_wecom_file_fails(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="wecom_file",
            title="采集结果",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    send = respx.post(webhook).mock(return_value=httpx.Response(500, json={"errcode": 1}))

    with pytest.raises(RuntimeError, match="任务产物文件发送失败"):
        await hook("T1", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert send.call_count == 1
    assert send.calls[0].request.content == b'{"msgtype":"file","file":{"media_id":"MEDIA"}}'


@pytest.mark.unit
@respx.mock
async def test_wecom_file_failure_falls_back_to_remote_link_when_configured(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="wecom_file",
            remote_upload_url="https://files.example.com",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    wecom_upload = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    remote_upload = respx.post(
        "https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://files.example.com/upload/publicData/result.zip"}},
        )
    )
    _mock_remote_file_ok("https://files.example.com/upload/publicData/result.zip")

    def send_handler(request: httpx.Request) -> httpx.Response:
        if b'"msgtype":"file"' in request.content:
            return httpx.Response(500, json={"errcode": 1})
        return httpx.Response(200, json={"errcode": 0})

    send = respx.post(webhook).mock(side_effect=send_handler)

    await hook("T_FALLBACK", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    assert wecom_upload.called
    assert remote_upload.called
    assert send.call_count == 2
    assert send.calls[0].request.content == b'{"msgtype":"file","file":{"media_id":"MEDIA"}}'
    assert b"https://files.example.com/upload/publicData/result.zip" in send.calls[1].request.content
    assert "附件直发失败" in send.calls[1].request.content.decode()


@pytest.mark.unit
@respx.mock
async def test_reliable_wecom_file_failure_records_remote_fallback_note(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="wecom_file",
            remote_upload_url="https://files.example.com",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    settings.storage.data_dir = str(tmp_path / "data")
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    remote_upload = respx.post(
        "https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://files.example.com/upload/publicData/result.zip"}},
        )
    )
    _mock_remote_file_ok("https://files.example.com/upload/publicData/result.zip")

    def send_handler(request: httpx.Request) -> httpx.Response:
        if b'"msgtype":"file"' in request.content:
            return httpx.Response(500, json={"errcode": 1})
        return httpx.Response(200, json={"errcode": 0})

    respx.post(webhook).mock(side_effect=send_handler)

    await hook("T_FALLBACK_NOTE", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert remote_upload.called
    assert sent[0]["remote_url"] == "https://files.example.com/upload/publicData/result.zip"
    assert sent[0]["file_sent"] == 0
    assert sent[0]["delivery_note"] == "企业微信群机器人；远程下载链接；完成消息"


@pytest.mark.unit
@respx.mock
async def test_retry_skips_wecom_file_when_file_already_sent(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="wecom_file",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    upload = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    send_count = {"n": 0}

    def send_handler(_request: httpx.Request) -> httpx.Response:
        send_count["n"] += 1
        if send_count["n"] == 2:
            return httpx.Response(500, json={"errcode": 1})
        return httpx.Response(200, json={"errcode": 0})

    send = respx.post(webhook).mock(side_effect=send_handler)

    with pytest.raises(RuntimeError, match="任务产物通知发送失败"):
        await hook("T_FILE_SENT", str(tmp_path), str(zip_path), {
            "total": 1,
            "success": 1,
            "failed": 0,
        })

    pending = store.list_delivery_outbox()[0]
    assert pending["file_sent"] == 1

    result = await retry_pending_deliveries(SimpleNamespace(settings=settings, state_store=store))

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert result["sent"] == 1
    assert sent[0]["task_id"] == "T_FILE_SENT"
    assert upload.call_count == 1
    assert send.call_count == 3
    assert send.calls[0].request.content == b'{"msgtype":"file","file":{"media_id":"MEDIA"}}'
    assert b'"msgtype":"markdown"' in send.calls[1].request.content
    assert b'"msgtype":"markdown"' in send.calls[2].request.content


@pytest.mark.unit
@respx.mock
async def test_reliable_hook_reuses_file_sent_progress_on_duplicate_enqueue(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="wecom_file",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    upload = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    send_count = {"n": 0}

    def send_handler(_request: httpx.Request) -> httpx.Response:
        send_count["n"] += 1
        if send_count["n"] == 2:
            return httpx.Response(500, json={"errcode": 1})
        return httpx.Response(200, json={"errcode": 0})

    send = respx.post(webhook).mock(side_effect=send_handler)

    with pytest.raises(RuntimeError, match="任务产物通知发送失败"):
        await hook("T_DUP", str(tmp_path), str(zip_path), {
            "total": 1,
            "success": 1,
            "failed": 0,
        })

    await hook("T_DUP", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert sent[0]["task_id"] == "T_DUP"
    assert upload.call_count == 1
    assert send.call_count == 3
    assert send.calls[0].request.content == b'{"msgtype":"file","file":{"media_id":"MEDIA"}}'
    assert b'"msgtype":"markdown"' in send.calls[1].request.content
    assert b'"msgtype":"markdown"' in send.calls[2].request.content


@pytest.mark.unit
@respx.mock
async def test_reliable_hook_resets_file_sent_progress_when_zip_changes(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="wecom_file",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    first_zip = tmp_path / "first.zip"
    second_zip = tmp_path / "second.zip"
    first_zip.write_bytes(b"123456")
    second_zip.write_bytes(b"abcdef")
    upload = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    send_count = {"n": 0}

    def send_handler(_request: httpx.Request) -> httpx.Response:
        send_count["n"] += 1
        if send_count["n"] == 2:
            return httpx.Response(500, json={"errcode": 1})
        return httpx.Response(200, json={"errcode": 0})

    send = respx.post(webhook).mock(side_effect=send_handler)

    with pytest.raises(RuntimeError, match="任务产物通知发送失败"):
        await hook("T_DUP_NEW_ZIP", str(tmp_path), str(first_zip), {
            "total": 1,
            "success": 1,
            "failed": 0,
        })

    await hook("T_DUP_NEW_ZIP", str(tmp_path), str(second_zip), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert sent[0]["task_id"] == "T_DUP_NEW_ZIP"
    assert upload.call_count == 2
    assert send.call_count == 4
    assert send.calls[0].request.content == b'{"msgtype":"file","file":{"media_id":"MEDIA"}}'
    assert b'"msgtype":"markdown"' in send.calls[1].request.content
    assert send.calls[2].request.content == b'{"msgtype":"file","file":{"media_id":"MEDIA"}}'
    assert b'"msgtype":"markdown"' in send.calls[3].request.content


@pytest.mark.unit
@respx.mock
async def test_wecom_file_over_20mb_falls_back_to_remote_link(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="wecom_file",
            remote_upload_url="https://files.example.com",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    zip_path = tmp_path / "large.zip"
    with zip_path.open("wb") as fp:
        fp.truncate(20 * 1024 * 1024 + 1)
    remote_upload = respx.post(
        "https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://files.example.com/upload/publicData/large.zip"}},
        )
    )
    _mock_remote_file_ok("https://files.example.com/upload/publicData/large.zip")
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T_LARGE", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    assert remote_upload.called
    assert send.call_count == 1
    assert b"https://files.example.com/upload/publicData/large.zip" in send.calls[0].request.content


@pytest.mark.unit
def test_delivery_plan_requires_remote_upload_for_large_forced_wecom_file(tmp_path):
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            file_delivery="wecom_file",
        )
    )
    zip_path = tmp_path / "large.zip"
    with zip_path.open("wb") as fp:
        fp.truncate(20 * 1024 * 1024 + 1)

    plan = build_delivery_test_plan(
        SimpleNamespace(settings=settings, state_store=_StateSettings()),
        file_path=zip_path,
    )

    assert plan["ok"] is False
    assert "远程链接模式需要配置 remote_upload_url 或 workshopUploadUrl" in plan["errors"]
    assert any("企业微信文件超过 20MB" in action for action in plan["actions"])
    assert "上传后验证下载链接可访问" in plan["actions"]


@pytest.mark.unit
def test_delivery_plan_reports_wecom_file_remote_fallback(tmp_path):
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            file_delivery="wecom_file",
            remote_upload_url="https://files.example.com",
        )
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    plan = build_delivery_test_plan(
        SimpleNamespace(settings=settings, state_store=_StateSettings()),
        file_path=zip_path,
    )

    assert plan["ok"] is True
    assert plan["requires_remote_upload"] is False
    assert plan["remote_fallback_available"] is True
    assert "企业微信文件直发" in plan["actions"]
    assert any(
        "文件直发失败时自动降级为远程下载链接" in action
        for action in plan["actions"]
    )
    assert "warnings" not in plan


@pytest.mark.unit
def test_delivery_plan_warns_when_wecom_file_has_no_remote_fallback(tmp_path):
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            file_delivery="wecom_file",
        )
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    plan = build_delivery_test_plan(
        SimpleNamespace(settings=settings, state_store=_StateSettings()),
        file_path=zip_path,
    )

    assert plan["ok"] is True
    assert plan["remote_fallback_available"] is False
    assert "远程上传兜底" in plan["warnings"][0]


@pytest.mark.unit
def test_delivery_plan_reports_missing_configured_env_file(tmp_path):
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            env_file=str(tmp_path / "missing.env"),
        )
    )

    plan = build_delivery_test_plan(
        SimpleNamespace(settings=settings, state_store=_StateSettings())
    )

    assert plan["ok"] is False
    assert plan["env_file_error"] is True
    assert plan["env_file_configured"] is True
    assert plan["env_file_loaded"] is False
    assert plan["env_file"] == str(tmp_path / "missing.env")
    assert "env 文件不存在" in plan["errors"][0]


@pytest.mark.unit
def test_delivery_plan_reports_loaded_state_env_file_without_secret(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    settings = AppSettings(delivery=DeliverySettings(enabled=False))

    plan = build_delivery_test_plan(
        SimpleNamespace(
            settings=settings,
            state_store=_StateSettings({"deliveryEnvFile": str(env_file)}),
        )
    )

    assert plan["ok"] is True
    assert plan["env_file_configured"] is True
    assert plan["env_file_loaded"] is True
    assert plan["env_file"] == str(env_file)
    assert plan["wecom_webhook_configured"] is True
    assert webhook not in json.dumps(plan, ensure_ascii=False)


@pytest.mark.unit
def test_delivery_plan_rejects_file_test_when_send_file_disabled(tmp_path):
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            send_message=True,
            send_file=False,
        )
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    plan = build_delivery_test_plan(
        SimpleNamespace(settings=settings, state_store=_StateSettings()),
        file_path=zip_path,
    )

    assert plan["ok"] is False
    assert "已指定测试文件，但 delivery.send_file=false，不会发送文件或链接" in plan["errors"]


@pytest.mark.unit
def test_delivery_plan_warns_when_remote_link_verification_disabled(tmp_path):
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            file_delivery="remote_link",
            remote_upload_url="https://files.example.com",
            remote_upload_verify_url=False,
        )
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    plan = build_delivery_test_plan(
        SimpleNamespace(settings=settings, state_store=_StateSettings()),
        file_path=zip_path,
    )

    assert plan["ok"] is True
    assert plan["remote_upload_verify_url"] is False
    assert "远程下载链接验证已关闭" in plan["warnings"][0]


@pytest.mark.unit
def test_delivery_plan_supports_desktop_wechat_direct_file(tmp_path):
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="desktop_wechat",
            desktop_wechat_target="文件传输助手",
            desktop_wechat_backend="powershell",
            desktop_wechat_select_mode="current",
        )
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    plan = build_delivery_test_plan(
        SimpleNamespace(settings=settings, state_store=_StateSettings()),
        file_path=zip_path,
    )

    assert plan["ok"] is True
    assert plan["desktop_wechat_configured"] is True
    assert plan["requires_remote_upload"] is False
    assert "本机微信直接发送文件" in plan["actions"]
    assert any("本机微信 PoC" in warning for warning in plan["warnings"])


@pytest.mark.unit
async def test_delivery_hook_sends_desktop_wechat_message_and_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []

    async def fake_send(settings, *, message, file_path=None, dry_run=False):
        calls.append({
            "target": settings.desktop_wechat_target,
            "message": message,
            "file_path": str(file_path or ""),
            "dry_run": dry_run,
        })
        return SimpleNamespace(ok=True)

    monkeypatch.setattr("spider_gd.app._send_desktop_wechat_delivery", fake_send)
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="desktop_wechat",
            desktop_wechat_target="文件传输助手",
            send_message=True,
            send_file=True,
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    await hook("T_DESKTOP", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert calls == [{
        "target": "文件传输助手",
        "message": calls[0]["message"],
        "file_path": str(zip_path),
        "dry_run": False,
    }]
    assert "T_DESKTOP" in str(calls[0]["message"])


@pytest.mark.unit
async def test_delivery_hook_skips_desktop_wechat_when_file_already_sent_and_no_message(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[object] = []

    async def fake_send(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(ok=True)

    monkeypatch.setattr("spider_gd.app._send_desktop_wechat_delivery", fake_send)
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="desktop_wechat",
            desktop_wechat_target="文件传输助手",
            send_message=False,
            send_file=True,
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    await hook(
        "T_DESKTOP_SENT",
        str(tmp_path),
        str(zip_path),
        {"total": 1, "success": 1, "failed": 0, "file_sent": "true"},
    )

    assert calls == []


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_uploads_remote_link_instead_of_wecom_file(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="remote_link",
            remote_upload_url="https://files.example.com",
            remote_upload_token="tok",
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    remote_upload = respx.post(
        "https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://files.example.com/upload/publicData/result.zip"}},
        )
    )
    _mock_remote_file_ok("https://files.example.com/upload/publicData/result.zip")
    wecom_send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T1", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert remote_upload.called
    assert remote_upload.calls.last.request.headers["Authorization"] == "Bearer tok"
    assert wecom_send.call_count == 1
    assert b"https://files.example.com/upload/publicData/result.zip" in wecom_send.calls[0].request.content


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_blocks_bad_remote_link_before_sending_message(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="remote_link",
            remote_upload_url="https://files.example.com",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    respx.post("https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData").mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://files.example.com/upload/publicData/result.zip"}},
        )
    )
    respx.head("https://files.example.com/upload/publicData/result.zip").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://files.example.com/upload/publicData/result.zip").mock(
        return_value=httpx.Response(404)
    )
    wecom_send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    with pytest.raises(RuntimeError, match="下载链接验证失败"):
        await hook("T_BAD_LINK", str(tmp_path), str(zip_path), {
            "total": 1,
            "success": 1,
            "failed": 0,
        })

    pending = store.list_delivery_outbox()
    store.close()
    assert wecom_send.call_count == 0
    assert pending[0]["task_id"] == "T_BAD_LINK"
    assert "下载链接验证失败" in pending[0]["last_error"]


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_sends_encoded_guessed_remote_link(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="remote_link",
            remote_upload_url="https://files.example.com",
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    zip_path = tmp_path / "日常 采集_EXEC1.zip"
    zip_path.write_bytes(b"123456")
    respx.post("https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData").mock(
        return_value=httpx.Response(200, json={"code": 200})
    )
    _mock_remote_file_ok(
        "https://files.example.com/upload/publicData/%E6%97%A5%E5%B8%B8%20"
        "%E9%87%87%E9%9B%86_EXEC1.zip"
    )
    wecom_send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T_CN", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert wecom_send.called
    assert (
        b"https://files.example.com/upload/publicData/%E6%97%A5%E5%B8%B8%20"
        b"%E9%87%87%E9%9B%86_EXEC1.zip"
    ) in wecom_send.calls.last.request.content


@pytest.mark.unit
@respx.mock
async def test_retry_reuses_persisted_remote_url_after_message_failure(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="remote_link",
            remote_upload_url="https://files.example.com",
            remote_upload_token="tok",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    remote_upload = respx.post(
        "https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://files.example.com/upload/publicData/result.zip"}},
        )
    )
    _mock_remote_file_ok("https://files.example.com/upload/publicData/result.zip")

    send_count = {"n": 0}

    def send_handler(_request: httpx.Request) -> httpx.Response:
        send_count["n"] += 1
        if send_count["n"] == 1:
            return httpx.Response(500, json={"errcode": 1})
        return httpx.Response(200, json={"errcode": 0})

    wecom_send = respx.post(webhook).mock(side_effect=send_handler)

    with pytest.raises(RuntimeError):
        await hook("T_REUSE", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    pending = store.list_delivery_outbox()[0]
    assert pending["remote_url"] == "https://files.example.com/upload/publicData/result.zip"

    result = await retry_pending_deliveries(SimpleNamespace(settings=settings, state_store=store))

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert result["sent"] == 1
    assert sent[0]["task_id"] == "T_REUSE"
    assert sent[0]["delivery_note"] == "企业微信群机器人；远程下载链接；完成消息"
    assert sent[0]["zip_size"] == len(b"123456")
    assert sent[0]["zip_sha256"] == hashlib.sha256(b"123456").hexdigest()
    assert remote_upload.call_count == 1
    assert wecom_send.call_count == 2


@pytest.mark.unit
@respx.mock
async def test_retry_sends_persisted_remote_url_when_local_zip_missing(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="remote_link",
            remote_upload_url="https://files.example.com",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    missing_zip = tmp_path / "deleted.zip"
    store.enqueue_delivery_outbox(
        task_id="T_LINK_ONLY",
        task_dir=str(tmp_path),
        zip_path=str(missing_zip),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="message failed after upload",
        remote_url="https://files.example.com/upload/publicData/deleted.zip",
    )
    _mock_remote_file_ok("https://files.example.com/upload/publicData/deleted.zip")
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    result = await retry_pending_deliveries(SimpleNamespace(settings=settings, state_store=store))

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert result["sent"] == 1
    assert result["skipped"] == 0
    assert sent[0]["task_id"] == "T_LINK_ONLY"
    assert sent[0]["delivery_note"] == "企业微信群机器人；远程下载链接；完成消息"
    assert send.called
    assert b"https://files.example.com/upload/publicData/deleted.zip" in send.calls.last.request.content


@pytest.mark.unit
@respx.mock
async def test_retry_blocks_bad_persisted_remote_url_without_sending_message(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="wecom_webhook",
            wecom_webhook=webhook,
            file_delivery="remote_link",
            remote_upload_url="https://files.example.com",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    missing_zip = tmp_path / "deleted.zip"
    store.enqueue_delivery_outbox(
        task_id="T_BAD_STORED_LINK",
        task_dir=str(tmp_path),
        zip_path=str(missing_zip),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="message failed after upload",
        remote_url="https://files.example.com/upload/publicData/deleted.zip",
    )
    respx.head("https://files.example.com/upload/publicData/deleted.zip").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://files.example.com/upload/publicData/deleted.zip").mock(
        return_value=httpx.Response(404)
    )
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    result = await retry_pending_deliveries(SimpleNamespace(settings=settings, state_store=store))

    pending = store.list_delivery_outbox(status="pending")
    store.close()
    assert result["ok"] is False
    assert result["failed"] == 1
    assert send.call_count == 0
    assert pending[0]["task_id"] == "T_BAD_STORED_LINK"
    assert "下载链接验证失败" in pending[0]["last_error"]


@pytest.mark.unit
async def test_retry_blocks_missing_local_zip_without_remote_url(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=True,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    missing_zip = tmp_path / "missing.zip"
    store.enqueue_delivery_outbox(
        task_id="T_MISSING",
        task_dir=str(tmp_path),
        zip_path=str(missing_zip),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="first try failed",
    )

    result = await retry_pending_deliveries(SimpleNamespace(settings=settings, state_store=store))

    failed = store.list_delivery_outbox(status="failed")
    store.close()
    assert result["ok"] is False
    assert result["blocked"] == 1
    assert result["failed"] == 0
    assert failed[0]["task_id"] == "T_MISSING"
    assert "待补发文件不存在" in failed[0]["last_error"]


@pytest.mark.unit
@respx.mock
async def test_retry_message_only_does_not_block_when_local_zip_missing(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            send_message=True,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    missing_zip = tmp_path / "missing.zip"
    store.enqueue_delivery_outbox(
        task_id="T_MESSAGE_ONLY",
        task_dir=str(tmp_path),
        zip_path=str(missing_zip),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="first try failed",
    )
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    result = await retry_pending_deliveries(SimpleNamespace(settings=settings, state_store=store))

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert result["ok"] is True
    assert result["sent"] == 1
    assert result["blocked"] == 0
    assert sent[0]["task_id"] == "T_MESSAGE_ONLY"
    assert send.called


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_serverchan_uploads_remote_link_for_personal_wechat(tmp_path):
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            backend="serverchan",
            serverchan_send_key="SCT123",
            file_delivery="auto",
            remote_upload_url="https://files.example.com",
            send_file=True,
        )
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    remote_upload = respx.post(
        "https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://files.example.com/upload/publicData/result.zip"}},
        )
    )
    _mock_remote_file_ok("https://files.example.com/upload/publicData/result.zip")
    serverchan = respx.post("https://sctapi.ftqq.com/SCT123.send").mock(
        return_value=httpx.Response(200, json={"code": 0})
    )

    await hook("T1", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert remote_upload.called
    assert serverchan.called
    form = serverchan.calls.last.request.content.decode()
    assert "https%3A%2F%2Ffiles.example.com%2Fupload%2FpublicData%2Fresult.zip" in form


@pytest.mark.unit
@respx.mock
async def test_delivery_hook_reads_settings_store_overrides(tmp_path):
    settings = AppSettings(delivery=DeliverySettings(enabled=False))
    state_store = _StateSettings(
        {
            "deliveryEnabled": "true",
            "deliveryWecomWebhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
            "deliveryFileDelivery": "remote_link",
            "deliveryRemoteUploadUrl": "https://files.example.com",
            "deliveryRemoteUploadToken": "tok",
        }
    )
    hook = _build_delivery_hook(SimpleNamespace(settings=settings, state_store=state_store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    remote_upload = respx.post(
        "https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://files.example.com/upload/publicData/result.zip"}},
        )
    )
    _mock_remote_file_ok("https://files.example.com/upload/publicData/result.zip")
    wecom_send = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T1", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    assert remote_upload.called
    assert remote_upload.calls.last.request.headers["Authorization"] == "Bearer tok"
    assert wecom_send.called


@pytest.mark.unit
async def test_reliable_delivery_hook_noops_when_disabled_at_send_time(tmp_path):
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(delivery=DeliverySettings(enabled=False))
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")

    try:
        await hook("T_DISABLED", str(tmp_path), str(zip_path), {
            "total": 1,
            "success": 1,
            "failed": 0,
        })

        assert store.get_delivery_outbox_summary()["total"] == 0
    finally:
        store.close()


@pytest.mark.unit
@respx.mock
async def test_reliable_delivery_hook_reads_settings_at_send_time(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(delivery=DeliverySettings(enabled=False))
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    store.save_settings({
        "deliveryEnabled": "true",
        "deliveryWecomWebhook": webhook,
        "deliverySendFile": "false",
        "deliveryRetryAttempts": "1",
        "deliveryRetryDelaySec": "0",
    })
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    try:
        await hook("T_ENABLED_LATE", str(tmp_path), str(zip_path), {
            "total": 1,
            "success": 1,
            "failed": 0,
        })

        sent = store.list_delivery_outbox(status="sent")
        assert send.called
        assert sent[0]["task_id"] == "T_ENABLED_LATE"
    finally:
        store.close()


@pytest.mark.unit
@respx.mock
async def test_reliable_delivery_hook_enqueues_failed_delivery(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    settings.storage.data_dir = str(tmp_path / "data")
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    respx.post(webhook).mock(return_value=httpx.Response(500, json={"errcode": 1}))

    with pytest.raises(RuntimeError):
        await hook("T1", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    pending = store.list_delivery_outbox()
    store.close()
    assert len(pending) == 1
    assert pending[0]["task_id"] == "T1"
    assert pending[0]["status"] == "pending"
    assert pending[0]["summary"]["success"] == 1


@pytest.mark.unit
@respx.mock
async def test_reliable_delivery_hook_persists_before_successful_send(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    settings.storage.data_dir = str(tmp_path / "data")
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T_SENT", str(tmp_path), str(zip_path), {"total": 1, "success": 1, "failed": 0})

    sent = store.list_delivery_outbox(status="sent")
    summary = store.get_delivery_outbox_summary()
    store.close()
    assert sent[0]["task_id"] == "T_SENT"
    assert sent[0]["last_error"] == ""
    assert sent[0]["delivery_note"] == "企业微信群机器人；完成消息"
    assert summary["sent"] == 1
    receipt_path = tmp_path / "data" / "_stable_delivery" / "receipts" / "T_SENT.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["command"] == "auto-delivery"
    assert receipt["outbox"]["task_id"] == "T_SENT"
    assert receipt["outbox"]["status"] == "sent"
    assert receipt["outbox"]["zip_size"] == 6
    assert receipt["outbox"]["zip_sha256"] == hashlib.sha256(b"123456").hexdigest()


@pytest.mark.unit
@respx.mock
async def test_reliable_delivery_hook_skips_already_sent_same_zip(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T_SENT_ONCE", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })
    await hook("T_SENT_ONCE", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert sent[0]["task_id"] == "T_SENT_ONCE"
    assert send.call_count == 1


@pytest.mark.unit
@respx.mock
async def test_reliable_delivery_hook_resends_when_delivery_target_changes(tmp_path):
    first_webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    second_webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=def"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=first_webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    ctx = SimpleNamespace(settings=settings, state_store=store)
    hook = _build_reliable_delivery_hook(ctx)
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    first_send = respx.post(first_webhook).mock(
        return_value=httpx.Response(200, json={"errcode": 0})
    )
    second_send = respx.post(second_webhook).mock(
        return_value=httpx.Response(200, json={"errcode": 0})
    )

    await hook("T_TARGET_CHANGE", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })
    first_identity = store.get_delivery_outbox("T_TARGET_CHANGE")["delivery_identity"]
    settings.delivery.wecom_webhook = second_webhook
    await hook("T_TARGET_CHANGE", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert sent[0]["task_id"] == "T_TARGET_CHANGE"
    assert first_send.call_count == 1
    assert second_send.call_count == 1
    assert sent[0]["delivery_identity"]
    assert sent[0]["delivery_identity"] != first_identity
    assert first_webhook not in sent[0]["delivery_identity"]
    assert second_webhook not in sent[0]["delivery_identity"]


@pytest.mark.unit
@respx.mock
async def test_reliable_delivery_hook_resends_when_existing_identity_missing(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store.enqueue_delivery_outbox(
        task_id="T_MISSING_IDENTITY",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="legacy sent",
    )
    store.mark_delivery_outbox_delivered("T_MISSING_IDENTITY")
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T_MISSING_IDENTITY", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert send.call_count == 1
    assert sent[0]["delivery_identity"]


@pytest.mark.unit
@respx.mock
async def test_reliable_delivery_hook_resends_when_same_path_content_changes(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    hook = _build_reliable_delivery_hook(SimpleNamespace(settings=settings, state_store=store))
    assert hook is not None

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"old")
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    await hook("T_SAME_PATH_NEW_CONTENT", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })
    zip_path.write_bytes(b"new content")
    await hook("T_SAME_PATH_NEW_CONTENT", str(tmp_path), str(zip_path), {
        "total": 1,
        "success": 1,
        "failed": 0,
    })

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert sent[0]["task_id"] == "T_SAME_PATH_NEW_CONTENT"
    assert send.call_count == 2


@pytest.mark.unit
async def test_retry_pending_deliveries_reports_disabled_config(tmp_path):
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(delivery=DeliverySettings(enabled=False))
    try:
        result = await retry_pending_deliveries(
            SimpleNamespace(settings=settings, state_store=store)
        )

        assert result["ok"] is False
        assert result["retried"] == 0
        assert result["errors"] == ["delivery.enabled 未开启"]
    finally:
        store.close()


@pytest.mark.unit
async def test_retry_pending_deliveries_reports_missing_env_file(tmp_path):
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            env_file=str(tmp_path / "missing.env"),
        )
    )
    try:
        result = await retry_pending_deliveries(
            SimpleNamespace(settings=settings, state_store=store)
        )

        assert result["ok"] is False
        assert result["retried"] == 0
        assert result["env_file_error"] is True
        assert "env 文件不存在" in result["errors"][0]
    finally:
        store.close()


@pytest.mark.unit
@respx.mock
async def test_retry_pending_deliveries_marks_sent(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    settings.storage.data_dir = str(tmp_path / "data")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store.enqueue_delivery_outbox(
        task_id="T1",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="first try failed",
    )
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    result = await retry_pending_deliveries(SimpleNamespace(settings=settings, state_store=store))

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert result["sent"] == 1
    assert result["failed"] == 0
    assert sent[0]["task_id"] == "T1"
    assert result["receipt_paths"] == [
        str(tmp_path / "data" / "_stable_delivery" / "receipts" / "T1.json")
    ]
    receipt = json.loads(Path(result["receipt_paths"][0]).read_text(encoding="utf-8"))
    assert receipt["command"] == "delivery-retry"
    assert receipt["outbox"]["status"] == "sent"
    assert receipt["outbox"]["zip_sha256"] == hashlib.sha256(b"123456").hexdigest()


@pytest.mark.unit
@respx.mock
async def test_retry_auto_requeues_recoverable_failed_delivery(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    settings.storage.data_dir = str(tmp_path / "data")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store.enqueue_delivery_outbox(
        task_id="T_RECOVER_FAILED",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="network down",
    )
    store.mark_delivery_outbox_failed("T_RECOVER_FAILED", "network down", status="failed")
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    result = await retry_pending_deliveries(
        SimpleNamespace(settings=settings, state_store=store),
        include_recoverable_failed=True,
    )

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert result["requeued_failed"] == 1
    assert result["retried"] == 1
    assert result["sent"] == 1
    assert sent[0]["task_id"] == "T_RECOVER_FAILED"


@pytest.mark.unit
@respx.mock
async def test_retry_does_not_auto_requeue_unrecoverable_failed_delivery(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=True,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    missing_zip = tmp_path / "missing.zip"
    store.enqueue_delivery_outbox(
        task_id="T_UNRECOVERABLE_FAILED",
        task_dir=str(tmp_path),
        zip_path=str(missing_zip),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="file missing",
    )
    store.mark_delivery_outbox_failed(
        "T_UNRECOVERABLE_FAILED",
        "file missing",
        status="failed",
    )
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    result = await retry_pending_deliveries(
        SimpleNamespace(settings=settings, state_store=store),
        include_recoverable_failed=True,
    )

    failed = store.list_delivery_outbox(status="failed")
    store.close()
    assert result["requeued_failed"] == 0
    assert result["retried"] == 0
    assert failed[0]["task_id"] == "T_UNRECOVERABLE_FAILED"
    assert not send.called


@pytest.mark.unit
@respx.mock
async def test_retry_resets_file_progress_when_delivery_target_changes(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=def"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            file_delivery="wecom_file",
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store.enqueue_delivery_outbox(
        task_id="T_TARGET_RETRY",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="old target message failed",
        delivery_identity="old-target",
    )
    assert store.mark_delivery_outbox_file_sent("T_TARGET_RETRY")
    upload = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=def&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    send = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    result = await retry_pending_deliveries(SimpleNamespace(settings=settings, state_store=store))

    sent = store.list_delivery_outbox(status="sent")
    store.close()
    assert result["sent"] == 1
    assert upload.called
    assert send.call_count == 2
    assert sent[0]["task_id"] == "T_TARGET_RETRY"
    assert sent[0]["delivery_identity"] != "old-target"


@pytest.mark.unit
@respx.mock
async def test_retry_pending_deliveries_serializes_concurrent_runs(tmp_path):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(
        delivery=DeliverySettings(
            enabled=True,
            wecom_webhook=webhook,
            send_file=False,
            retry_attempts=1,
            retry_delay_sec=0,
        )
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store.enqueue_delivery_outbox(
        task_id="T_LOCK",
        task_dir=str(tmp_path),
        zip_path=str(zip_path),
        summary={"total": 1, "success": 1, "failed": 0},
        error_msg="first try failed",
    )
    route = respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    ctx = SimpleNamespace(settings=settings, state_store=store)

    first, second = await asyncio.gather(
        retry_pending_deliveries(ctx),
        retry_pending_deliveries(ctx),
    )

    summary = store.get_delivery_outbox_summary()
    store.close()
    assert first["sent"] + second["sent"] == 1
    assert route.call_count == 1
    assert summary["sent"] == 1


@pytest.mark.unit
async def test_deliver_task_runner_downloads_zips_only_changed_files(tmp_path):
    from spider_gd.app import _snapshot_download_files, deliver_task_runner_downloads

    store = TaskStateStore(tmp_path / "spider.db")
    settings = AppSettings(delivery=DeliverySettings(enabled=False))
    old_file = tmp_path / "old.xlsx"
    old_file.write_bytes(b"old")
    (tmp_path / "storage_state.json").write_text("{}", encoding="utf-8")
    before = _snapshot_download_files(tmp_path)
    new_file = tmp_path / "new.xlsx"
    new_file.write_bytes(b"new")

    zip_path = await deliver_task_runner_downloads(
        SimpleNamespace(settings=settings, state_store=store),
        exec_id="EXEC1",
        task_config_id="TASK1",
        task_name="日常采集",
        download_dir=tmp_path,
        before=before,
    )

    store.close()
    assert zip_path is not None
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["new.xlsx"]
