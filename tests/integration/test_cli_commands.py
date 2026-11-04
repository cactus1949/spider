"""集成：CLI 子命令真实接入（不依赖外网/浏览器的分支）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
import respx

from spider_gd.__main__ import main
from spider_gd.infrastructure.storage.database import TaskStateStore

_CFG = "configs/mode_d_offline.yaml"


@pytest.mark.integration
def test_crawl_dry_run_returns_zero():
    assert main(["--config", _CFG, "crawl", "--dry-run"]) == 0


@pytest.mark.integration
def test_schedule_list_returns_zero(capsys):
    assert main(["--config", _CFG, "schedule", "--list"]) == 0
    out = capsys.readouterr().out
    assert "cleanup_exec_logs" in out


@pytest.mark.integration
def test_health_outputs_json(capsys):
    assert main(["--config", _CFG, "health"]) == 0
    out = capsys.readouterr().out
    assert "overall" in out


@pytest.mark.integration
def test_alert_test_returns_zero():
    # 离线模式用 NullAlertGateway，发送被抑制但命令成功
    assert main(["--config", _CFG, "alert", "--test", "hi"]) == 0


@pytest.mark.integration
def test_agent_disabled_in_offline():
    # 离线模式未启用 agent
    assert main(["--config", _CFG, "agent", "--simulate", "查询状态"]) == 1


@pytest.mark.integration
def test_delivery_test_requires_enabled_config(tmp_path: Path, capsys):
    cfg = tmp_path / "delivery_disabled.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: false
""",
        encoding="utf-8",
    )
    assert main(["--config", str(cfg), "delivery-test"]) == 1
    out = capsys.readouterr().out
    assert "delivery.enabled 未开启" in out


@pytest.mark.integration
def test_delivery_test_dry_run_uses_configured_strategy(tmp_path: Path, capsys):
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
  file_delivery: remote_link
  remote_upload_url: https://files.example.com
""",
        encoding="utf-8",
    )

    assert main(["--config", str(cfg), "delivery-test", "--file", str(sample)]) == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out
    assert "远程上传后发送下载链接" in out


@pytest.mark.integration
@respx.mock
def test_delivery_test_send_file_uses_reliable_outbox(tmp_path: Path, capsys):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    db_path = tmp_path / "spider.db"
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery_send.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: {webhook}
  file_delivery: wecom_file
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
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

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-test",
            "--file",
            str(sample),
            "--send",
        ])
        == 1
    )
    out = capsys.readouterr().out
    assert '"sent": false' in out
    assert '"task_id": "DELIVERY_TEST_' in out
    assert '"outbox": {' in out
    assert '"status": "pending"' in out
    assert '"file_sent": true' in out
    assert '"zip_size": 6' in out
    assert hashlib.sha256(b"123456").hexdigest() in out
    assert "任务产物通知发送失败" in out
    store = TaskStateStore(db_path)
    try:
        pending = store.list_delivery_outbox()
        assert pending[0]["task_id"].startswith("DELIVERY_TEST_")
        assert pending[0]["file_sent"] == 1
        assert "任务产物通知发送失败" in pending[0]["last_error"]
    finally:
        store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_test_send_file_respects_explicit_task_id(tmp_path: Path, capsys):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    db_path = tmp_path / "spider.db"
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery_send_explicit_id.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: {webhook}
  file_delivery: wecom_file
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-test",
            "--file",
            str(sample),
            "--task-id",
            "MANUAL_TEST_ID",
            "--send",
        ])
        == 0
    )
    out = capsys.readouterr().out
    assert '"task_id": "MANUAL_TEST_ID"' in out
    assert '"status": "sent"' in out
    assert '"zip_size": 6' in out
    assert hashlib.sha256(b"123456").hexdigest() in out
    assert '"delivery_note": "企业微信群机器人；企业微信文件直发；完成消息"' in out
    store = TaskStateStore(db_path)
    try:
        sent = store.list_delivery_outbox(status="sent")
        assert sent[0]["task_id"] == "MANUAL_TEST_ID"
    finally:
        store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_test_send_sample_zip_creates_file_and_receipt(tmp_path: Path, capsys):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    db_path = tmp_path / "spider.db"
    data_dir = tmp_path / "data"
    cfg = tmp_path / "delivery_send_sample.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{data_dir}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: {webhook}
  file_delivery: wecom_file
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert main(["--config", str(cfg), "delivery-test", "--sample-zip", "--send", "--verify"]) == 0
    out = capsys.readouterr().out
    assert '"sent": true' in out
    assert '"task_id": "DELIVERY_TEST_' in out
    assert '"status": "sent"' in out
    assert '"verification": {' in out
    assert '"local_zip_sha256"' in out
    assert '"delivery_note": "企业微信群机器人；企业微信文件直发；完成消息"' in out
    assert '"zip_size":' in out
    assert '"zip_sha256":' in out
    assert "delivery_sample_" in out
    body = json.loads(out)
    receipt_path = Path(body["receipt_path"])
    assert receipt_path.parent == data_dir / "_stable_delivery" / "receipts"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["command"] == "delivery-test"
    assert receipt["sample_zip"] is True
    assert receipt["outbox"]["task_id"].startswith("DELIVERY_TEST_")

    store = TaskStateStore(db_path)
    try:
        sent = store.list_delivery_outbox(status="sent")
        assert sent[0]["task_id"].startswith("DELIVERY_TEST_")
        zip_path = Path(sent[0]["zip_path"])
        assert zip_path.name.startswith("delivery_sample_")
        assert zip_path.parent == data_dir / "_stable_delivery" / "samples"
        assert sent[0]["zip_size"] > 0
        assert len(sent[0]["zip_sha256"]) == 64
    finally:
        store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_acceptance_sample_zip_verifies_remote_link(tmp_path: Path, capsys):
    from email.parser import BytesParser
    from email.policy import default

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    remote_url = "https://files.example.com/upload/publicData/delivery_acceptance.zip"
    db_path = tmp_path / "spider.db"
    cfg = tmp_path / "delivery_acceptance.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
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
    respx.get(remote_url).mock(side_effect=lambda _request: httpx.Response(200, content=uploaded["content"]))
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-acceptance",
            "--wecom-webhook",
            webhook,
            "--remote-upload-url",
            "https://files.example.com",
            "--remote-upload-token",
            "upload-secret-1234",
            "--require-remote-url",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["acceptance"] is True
    assert body["sample_zip"] is True
    assert body["sent"] is True
    assert body["auto_force_remote_link"] is True
    assert body["task_id"].startswith("DELIVERY_ACCEPTANCE_")
    assert body["outbox"]["status"] == "sent"
    assert body["outbox"]["remote_url"] == remote_url
    assert body["outbox"]["forwarding"]["ready"] is True
    assert body["outbox"]["forwarding"]["can_forward_link"] is True
    assert body["outbox"]["forwarding"]["remote_url"] == remote_url
    assert "zip_sha256" in body["outbox"]["forwarding"]["instruction"]
    assert body["receipt_path"].endswith(f'{body["task_id"]}.json')
    receipt = json.loads(Path(body["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["command"] == "delivery-acceptance"
    assert receipt["acceptance"] is True
    assert receipt["acceptance_ok"] is True
    assert receipt["sent"] is True
    assert receipt["outbox"]["remote_url"] == remote_url
    assert receipt["outbox"]["forwarding"]["can_forward_link"] is True
    assert receipt["outbox"]["forwarding"]["zip_sha256"] == body["outbox"]["zip_sha256"]
    assert receipt["verification"]["ok"] is True
    assert receipt["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert receipt["overrides"]["deliveryRemoteUploadToken"] == "uplo...***"
    assert body["verification"]["ok"] is True
    assert any(
        check["name"] == "remote_zip_sha256"
        and check["ok"]
        and check["actual"] == body["outbox"]["zip_sha256"]
        for check in body["verification"]["checks"]
    )
    assert any(
        check["name"] == "remote_zip_sha256"
        and check["ok"]
        and check["actual"] == body["outbox"]["zip_sha256"]
        for check in receipt["verification"]["checks"]
    )
    assert body["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert body["overrides"]["deliveryFileDelivery"] == "remote_link"
    assert body["overrides"]["deliveryRemoteUploadToken"] == "uplo...***"
    assert webhook not in out
    assert "upload-secret-1234" not in out
    assert webhook not in json.dumps(receipt, ensure_ascii=False)
    assert "upload-secret-1234" not in json.dumps(receipt, ensure_ascii=False)


@pytest.mark.integration
@respx.mock
def test_delivery_acceptance_can_read_secrets_from_environment(tmp_path: Path, capsys, monkeypatch):
    from email.parser import BytesParser
    from email.policy import default

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    upload_token = "upload-secret-from-env"
    remote_url = "https://files.example.com/upload/publicData/delivery_env.zip"
    cfg = tmp_path / "delivery_acceptance_env.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SPIDER_GD_WECOM_WEBHOOK", webhook)
    monkeypatch.setenv("SPIDER_GD_REMOTE_UPLOAD_URL", "https://files.example.com")
    monkeypatch.setenv("SPIDER_GD_REMOTE_UPLOAD_TOKEN", upload_token)
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
    respx.get(remote_url).mock(side_effect=lambda _request: httpx.Response(200, content=uploaded["content"]))
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-acceptance",
            "--require-remote-url",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    receipt = json.loads(Path(body["receipt_path"]).read_text(encoding="utf-8"))
    assert body["ok"] is True
    assert body["outbox"]["remote_url"] == remote_url
    assert body["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert body["overrides"]["deliveryRemoteUploadToken"] == "uplo...***"
    assert receipt["acceptance_ok"] is True
    assert receipt["verification"]["ok"] is True
    assert webhook not in out
    assert upload_token not in out
    receipt_text = json.dumps(receipt, ensure_ascii=False)
    assert webhook not in receipt_text
    assert upload_token not in receipt_text


@pytest.mark.integration
@respx.mock
def test_delivery_acceptance_env_file_overrides_process_environment(tmp_path: Path, capsys, monkeypatch):
    from email.parser import BytesParser
    from email.policy import default

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    stale_webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=stale"
    upload_token = "upload-secret-from-file"
    remote_url = "https://files.example.com/upload/publicData/delivery_env_file.zip"
    cfg = tmp_path / "delivery_acceptance_env_file.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f"""
# stable delivery secrets
export SPIDER_GD_WECOM_WEBHOOK="{webhook}"
SPIDER_GD_REMOTE_UPLOAD_URL=https://files.example.com
SPIDER_GD_REMOTE_UPLOAD_TOKEN='{upload_token}'
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SPIDER_GD_WECOM_WEBHOOK", stale_webhook)
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
    respx.get(remote_url).mock(side_effect=lambda _request: httpx.Response(200, content=uploaded["content"]))
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-acceptance",
            "--env-file",
            str(env_file),
            "--require-remote-url",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    receipt = json.loads(Path(body["receipt_path"]).read_text(encoding="utf-8"))
    assert body["ok"] is True
    assert body["outbox"]["remote_url"] == remote_url
    assert body["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert body["overrides"]["deliveryRemoteUploadToken"] == "uplo...***"
    assert receipt["acceptance_ok"] is True
    assert receipt["verification"]["ok"] is True
    assert webhook not in out
    assert stale_webhook not in out
    assert upload_token not in out
    receipt_text = json.dumps(receipt, ensure_ascii=False)
    assert webhook not in receipt_text
    assert stale_webhook not in receipt_text
    assert upload_token not in receipt_text


@pytest.mark.integration
def test_delivery_acceptance_require_remote_url_requires_upload_config(tmp_path: Path, capsys):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    db_path = tmp_path / "spider.db"
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery_acceptance_direct.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: {webhook}
  file_delivery: wecom_file
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-acceptance",
            "--file",
            str(sample),
            "--require-remote-url",
        ])
        == 1
    )

    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["sent"] is False
    assert body["auto_force_remote_link"] is True
    assert body["overrides"]["deliveryFileDelivery"] == "remote_link"
    assert body["plan"]["requires_remote_upload"] is True
    assert "远程链接模式需要配置 remote_upload_url" in "\n".join(body["errors"])


@pytest.mark.integration
@respx.mock
def test_delivery_test_sample_zip_can_force_remote_link_and_download(tmp_path: Path, capsys):
    from email.parser import BytesParser
    from email.policy import default

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    remote_url = "https://files.example.com/upload/publicData/delivery_sample.zip"
    db_path = tmp_path / "spider.db"
    data_dir = tmp_path / "data"
    cfg = tmp_path / "delivery_sample_remote.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{data_dir}"
log:
  persist_to_file: false
delivery:
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
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

    def download_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=uploaded["content"])

    respx.get(remote_url).mock(side_effect=download_handler)
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-test",
            "--sample-zip",
            "--wecom-webhook",
            webhook,
            "--force-remote-link",
            "--remote-upload-url",
            "https://files.example.com",
            "--remote-upload-token",
            "upload-secret-1234",
            "--send",
            "--download-remote",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["sent"] is True
    assert body["file_delivery"] == "remote_link"
    assert body["overrides"]["deliveryEnabled"] == "true"
    assert body["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert body["overrides"]["deliveryRemoteUploadUrl"] == "https://files.example.com"
    assert body["overrides"]["deliveryRemoteUploadToken"] == "uplo...***"
    assert webhook not in out
    assert "upload-secret-1234" not in out
    assert body["outbox"]["remote_url"] == remote_url
    assert body["verification"]["ok"] is True
    assert body["verification"]["source"] == body["receipt_path"]
    assert any(
        check["name"] == "remote_zip_sha256"
        and check["ok"]
        and check["actual"] == body["outbox"]["zip_sha256"]
        for check in body["verification"]["checks"]
    )


@pytest.mark.integration
@respx.mock
def test_delivery_test_sample_zip_can_use_pushplus_link_backend(tmp_path: Path, capsys):
    from email.parser import BytesParser
    from email.policy import default

    remote_url = "https://files.example.com/upload/publicData/delivery_sample.zip"
    db_path = tmp_path / "spider.db"
    data_dir = tmp_path / "data"
    cfg = tmp_path / "delivery_sample_pushplus.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{data_dir}"
log:
  persist_to_file: false
delivery:
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
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
    respx.get(remote_url).mock(side_effect=lambda _request: httpx.Response(200, content=uploaded["content"]))
    pushplus_payloads: list[dict[str, object]] = []

    def pushplus_handler(request: httpx.Request) -> httpx.Response:
        pushplus_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"code": 200})

    respx.post("https://www.pushplus.plus/send").mock(side_effect=pushplus_handler)

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-test",
            "--sample-zip",
            "--pushplus-token",
            "pptok",
            "--remote-upload-url",
            "https://files.example.com",
            "--send",
            "--download-remote",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["sent"] is True
    assert body["backend"] == "pushplus"
    assert body["overrides"]["deliveryBackend"] == "pushplus"
    assert body["overrides"]["deliveryPushplusToken"] == "***"
    assert "pptok" not in out
    assert body["outbox"]["remote_url"] == remote_url
    assert body["verification"]["ok"] is True
    assert body["verification"]["source"] == body["receipt_path"]
    assert pushplus_payloads
    assert pushplus_payloads[0]["token"] == "pptok"
    assert remote_url in str(pushplus_payloads[0]["content"])
    assert any(
        check["name"] == "remote_zip_sha256"
        and check["ok"]
        and check["actual"] == body["outbox"]["zip_sha256"]
        for check in body["verification"]["checks"]
    )


@pytest.mark.integration
@respx.mock
def test_delivery_send_uses_stable_task_id_and_skips_duplicate(tmp_path: Path, capsys):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    db_path = tmp_path / "spider.db"
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery_send_command.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: {webhook}
  file_delivery: wecom_file
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    calls = {"upload": 0, "send": 0}

    def upload_handler(_request: httpx.Request) -> httpx.Response:
        calls["upload"] += 1
        return httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"})

    def send_handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return httpx.Response(200, json={"errcode": 0})

    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(side_effect=upload_handler)
    respx.post(webhook).mock(side_effect=send_handler)

    command = ["--config", str(cfg), "delivery-send", "--file", str(sample), "--verify"]
    assert main(command) == 0
    first_out = capsys.readouterr().out
    assert '"command": "delivery-send"' in first_out
    assert '"task_id": "DELIVERY_SEND_result_' in first_out
    assert '"status": "sent"' in first_out
    assert '"zip_size": 6' in first_out
    assert hashlib.sha256(b"123456").hexdigest() in first_out
    first_body = json.loads(first_out)
    receipt_path = Path(first_body["receipt_path"])
    assert receipt_path.parent == tmp_path / "data" / "_stable_delivery" / "receipts"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["command"] == "delivery-send"
    assert receipt["summary"] == {"total": 1, "success": 1, "failed": 0}
    assert receipt["outbox"]["task_id"].startswith("DELIVERY_SEND_result_")
    assert receipt["outbox"]["status"] == "sent"
    assert receipt["outbox"]["zip_size"] == 6
    assert receipt["outbox"]["zip_sha256"] == hashlib.sha256(b"123456").hexdigest()
    assert receipt["outbox"]["forwarding"]["ready"] is True
    assert receipt["outbox"]["forwarding"]["file_sent"] is True
    assert receipt["outbox"]["forwarding"]["can_forward_link"] is False
    assert "转发该文件" in receipt["outbox"]["forwarding"]["instruction"]
    assert first_body["verification"]["ok"] is True
    assert any(
        check["name"] == "local_zip_sha256" and check["ok"]
        for check in first_body["verification"]["checks"]
    )
    first_calls = dict(calls)
    assert first_calls == {"upload": 1, "send": 2}

    assert main(command) == 0
    second_out = capsys.readouterr().out
    assert '"sent": true' in second_out
    assert calls == first_calls

    store = TaskStateStore(db_path)
    try:
        sent = store.list_delivery_outbox(status="sent")
        assert len(sent) == 1
        assert sent[0]["task_id"].startswith("DELIVERY_SEND_result_")
        assert sent[0]["zip_size"] == 6
    finally:
        store.close()


@pytest.mark.integration
@respx.mock
def test_delivery_send_can_download_remote_after_send(tmp_path: Path, capsys):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    remote_url = "https://files.example.com/upload/publicData/result.zip"
    db_path = tmp_path / "spider.db"
    payload = b"123456"
    sample = tmp_path / "result.zip"
    sample.write_bytes(payload)
    cfg = tmp_path / "delivery_send_remote.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: {webhook}
  file_delivery: wecom_file
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    respx.post("https://files.example.com/guangdong/data/upload?relativePath=/upload/publicData").mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": remote_url}},
        )
    )
    respx.head(remote_url).mock(return_value=httpx.Response(200))
    respx.get(remote_url).mock(return_value=httpx.Response(200, content=payload))
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-send",
            "--file",
            str(sample),
            "--force-remote-link",
            "--remote-upload-url",
            "https://files.example.com",
            "--download-remote",
        ])
        == 0
    )

    body = json.loads(capsys.readouterr().out)
    assert body["sent"] is True
    assert body["file_delivery"] == "remote_link"
    assert body["overrides"]["deliveryFileDelivery"] == "remote_link"
    assert body["overrides"]["deliveryRemoteUploadUrl"] == "https://files.example.com"
    assert body["verification"]["ok"] is True
    assert body["outbox"]["remote_url"] == remote_url
    assert any(
        check["name"] == "remote_zip_sha256"
        and check["ok"]
        and check["actual"] == hashlib.sha256(payload).hexdigest()
        for check in body["verification"]["checks"]
    )


@pytest.mark.integration
@respx.mock
def test_wechat_poc_wecom_webhook_file_uses_stable_outbox(tmp_path: Path, capsys):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    db_path = tmp_path / "spider.db"
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "wechat_webhook.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    sent_payloads: list[dict[str, object]] = []

    def send_handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    respx.post(webhook).mock(side_effect=send_handler)

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--backend",
            "wecom-webhook",
            "--webhook",
            webhook,
            "--message",
            "任务完成，见附件",
            "--file",
            str(sample),
            "--verify",
            "--send",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["command"] == "wechat-poc wecom-webhook"
    assert body["stable_delivery"] is True
    assert body["sent"] is True
    assert body["outbox"]["status"] == "sent"
    assert body["outbox"]["zip_sha256"] == hashlib.sha256(b"123456").hexdigest()
    assert body["verification"]["ok"] is True
    assert Path(body["receipt_path"]).is_file()
    assert body["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert webhook not in out
    assert sent_payloads[0]["msgtype"] == "file"
    assert sent_payloads[1]["msgtype"] == "markdown"
    assert "备注：任务完成，见附件" in sent_payloads[1]["markdown"]["content"]

    store = TaskStateStore(db_path)
    try:
        rows = store.list_delivery_outbox(status="sent", limit=10)
        assert len(rows) == 1
        assert rows[0]["task_id"].startswith("DELIVERY_SEND_result_")
    finally:
        store.close()


@pytest.mark.integration
@respx.mock
def test_wechat_poc_wecom_webhook_reads_env_for_message(tmp_path: Path, capsys, monkeypatch):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    cfg = tmp_path / "wechat_webhook_env_message.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
""",
        encoding="utf-8",
    )
    payloads: list[dict[str, object]] = []

    def send_handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setenv("SPIDER_GD_WECOM_WEBHOOK", webhook)
    respx.post(webhook).mock(side_effect=send_handler)

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--backend",
            "wecom-webhook",
            "--message",
            "hello",
            "--send",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["platform"] == "webhook"
    assert payloads[0]["msgtype"] == "markdown"
    assert webhook not in out


@pytest.mark.integration
@respx.mock
def test_wechat_poc_wecom_webhook_reads_env_file_for_message(tmp_path: Path, capsys, monkeypatch):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    stale_webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=stale"
    cfg = tmp_path / "wechat_webhook_env_file_message.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
""",
        encoding="utf-8",
    )
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    payloads: list[dict[str, object]] = []

    def send_handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setenv("SPIDER_GD_WECOM_WEBHOOK", stale_webhook)
    respx.post(webhook).mock(side_effect=send_handler)

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--backend",
            "wecom-webhook",
            "--env-file",
            str(env_file),
            "--message",
            "hello",
            "--send",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["platform"] == "webhook"
    assert payloads[0]["msgtype"] == "markdown"
    assert webhook not in out
    assert stale_webhook not in out


@pytest.mark.integration
def test_wechat_poc_auto_without_stable_config_does_not_fallback_to_desktop(tmp_path: Path, capsys):
    cfg = tmp_path / "wechat_auto_no_stable.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
""",
        encoding="utf-8",
    )

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--target",
            "文件传输助手",
            "--message",
            "hello",
        ])
        == 1
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is False
    assert body["platform"] == "stable"
    assert "不会退回本机微信窗口" in body["error_msg"]
    assert "激活微信客户端" not in out
    assert "--backend powershell" in out
    assert "delivery-doctor" in out


@pytest.mark.integration
def test_wechat_poc_help_states_auto_is_stable_only(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["wechat-poc", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    normalized = " ".join(out.split())
    assert "auto 只使用已配置的稳定发送通道" in normalized
    assert "本机 PoC 需显式指定 powershell 或 wxauto" in normalized
    assert "默认走 PowerShell" not in out


@pytest.mark.integration
def test_wechat_poc_missing_env_file_returns_json_error(tmp_path: Path, capsys):
    cfg = tmp_path / "wechat_missing_env.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
""",
        encoding="utf-8",
    )
    missing = tmp_path / "missing.env"

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--env-file",
            str(missing),
            "--message",
            "hello",
        ])
        == 1
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is False
    assert body["command"] == "wechat-poc"
    assert body["env_file"] == str(missing)
    assert "env 文件不存在" in body["error_msg"]
    assert "Traceback" not in out


@pytest.mark.integration
def test_delivery_doctor_missing_env_file_returns_json_error(tmp_path: Path, capsys):
    cfg = tmp_path / "doctor_missing_env.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
""",
        encoding="utf-8",
    )
    missing = tmp_path / "missing.env"

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-doctor",
            "--env-file",
            str(missing),
        ])
        == 1
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is False
    assert body["command"] == "delivery-doctor"
    assert "env 文件不存在" in body["error_msg"]
    assert "Traceback" not in out


@pytest.mark.integration
def test_wechat_poc_explicit_powershell_still_allows_desktop_dry_run(tmp_path: Path, capsys):
    cfg = tmp_path / "wechat_powershell_explicit.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
""",
        encoding="utf-8",
    )

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--backend",
            "powershell",
            "--target",
            "文件传输助手",
            "--message",
            "hello",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert "校验当前微信会话包含目标: 文件传输助手" in body["actions"]


@pytest.mark.integration
@respx.mock
def test_wechat_poc_auto_uses_stable_delivery_config_for_message(tmp_path: Path, capsys):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    cfg = tmp_path / "wechat_auto_stable_config_message.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: "{webhook}"
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    payloads: list[dict[str, object]] = []

    def send_handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    respx.post(webhook).mock(side_effect=send_handler)

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--target",
            "文件传输助手",
            "--message",
            "hello",
            "--send",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["platform"] == "stable"
    assert body["command"] == "wechat-poc auto-stable"
    assert body["auto_selected"] is True
    assert "不控制本机微信窗口、不使用搜索结果" in body["actions"]
    assert "hello" in payloads[0]["markdown"]["content"]
    assert webhook not in out


@pytest.mark.integration
@respx.mock
def test_wechat_poc_auto_uses_env_file_stable_webhook_for_message(tmp_path: Path, capsys, monkeypatch):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    stale_webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=stale"
    cfg = tmp_path / "wechat_auto_stable_env_file_message.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
""",
        encoding="utf-8",
    )
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    payloads: list[dict[str, object]] = []

    def send_handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setenv("SPIDER_GD_WECOM_WEBHOOK", stale_webhook)
    respx.post(webhook).mock(side_effect=send_handler)

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--env-file",
            str(env_file),
            "--target",
            "文件传输助手",
            "--message",
            "hello",
            "--send",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["platform"] == "stable"
    assert body["auto_selected"] is True
    assert body["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert "hello" in payloads[0]["markdown"]["content"]
    assert webhook not in out
    assert stale_webhook not in out


@pytest.mark.integration
@respx.mock
def test_wechat_poc_auto_stable_file_uses_reliable_outbox(tmp_path: Path, capsys, monkeypatch):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    db_path = tmp_path / "spider.db"
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "wechat_auto_stable_file.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SPIDER_GD_WECOM_WEBHOOK", webhook)
    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--message",
            "任务完成，见附件",
            "--file",
            str(sample),
            "--verify",
            "--send",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["command"] == "wechat-poc auto-stable"
    assert body["platform"] == "stable"
    assert body["sent"] is True
    assert body["outbox"]["status"] == "sent"
    assert body["verification"]["ok"] is True
    assert webhook not in out


@pytest.mark.integration
@respx.mock
def test_wechat_poc_wecom_webhook_file_reads_env_for_stable_outbox(tmp_path: Path, capsys, monkeypatch):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    db_path = tmp_path / "spider.db"
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "wechat_webhook_env_file.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SPIDER_GD_WECOM_WEBHOOK", webhook)
    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "wechat-poc",
            "--backend",
            "wecom-webhook",
            "--message",
            "任务完成，见附件",
            "--file",
            str(sample),
            "--verify",
            "--send",
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["command"] == "wechat-poc wecom-webhook"
    assert body["sent"] is True
    assert body["outbox"]["status"] == "sent"
    assert body["verification"]["ok"] is True
    assert body["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert webhook not in out


@pytest.mark.integration
def test_delivery_verify_checks_outbox_zip_hash(tmp_path: Path, capsys):
    db_path = tmp_path / "spider.db"
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    cfg = tmp_path / "delivery_verify.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
""",
        encoding="utf-8",
    )
    store = TaskStateStore(db_path)
    try:
        store.enqueue_delivery_outbox(
            task_id="T_VERIFY",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="waiting",
        )
        store.mark_delivery_outbox_delivered("T_VERIFY", "企业微信群机器人；完成消息")
    finally:
        store.close()

    assert main(["--config", str(cfg), "delivery-verify", "--task-id", "T_VERIFY"]) == 0
    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["receipt"]["zip_size"] == 6
    assert body["receipt"]["zip_sha256"] == hashlib.sha256(b"123456").hexdigest()
    assert any(check["name"] == "local_zip_sha256" and check["ok"] for check in body["checks"])


@pytest.mark.integration
def test_delivery_verify_download_remote_requires_remote_url(tmp_path: Path, capsys):
    db_path = tmp_path / "spider.db"
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    cfg = tmp_path / "delivery_verify_no_remote.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
""",
        encoding="utf-8",
    )
    store = TaskStateStore(db_path)
    try:
        store.enqueue_delivery_outbox(
            task_id="T_NO_REMOTE",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="waiting",
        )
        store.mark_delivery_outbox_delivered("T_NO_REMOTE", "企业微信群机器人；企业微信文件直发")
    finally:
        store.close()

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-verify",
            "--task-id",
            "T_NO_REMOTE",
            "--download-remote",
        ])
        == 1
    )
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert "要求下载远端文件核验，但回执没有 remote_url" in body["errors"]
    assert any(
        check["name"] == "remote_url_present" and not check["ok"]
        for check in body["checks"]
    )


@pytest.mark.integration
@respx.mock
def test_delivery_verify_checks_receipt_remote_url(tmp_path: Path, capsys):
    remote_url = "https://files.example.com/result.zip"
    zip_path = tmp_path / "result.zip"
    payload = b"123456"
    zip_path.write_bytes(payload)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "command": "delivery-send",
                "outbox": {
                    "task_id": "T_REMOTE",
                    "status": "sent",
                    "zip_path": str(zip_path),
                    "zip_size": len(payload),
                    "zip_sha256": hashlib.sha256(payload).hexdigest(),
                    "remote_url": remote_url,
                    "file_sent": False,
                    "delivery_note": "企业微信群机器人；远程下载链接；完成消息",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = tmp_path / "delivery_verify_receipt.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    respx.head(remote_url).mock(return_value=httpx.Response(200))
    respx.get(remote_url).mock(return_value=httpx.Response(200, content=payload))

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-verify",
            "--receipt",
            str(receipt_path),
            "--download-remote",
        ])
        == 0
    )
    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["source"] == str(receipt_path)
    assert any(
        check["name"] == "remote_url_reachable" and check["ok"] and check["message"] == "HEAD 200"
        for check in body["checks"]
    )
    assert any(
        check["name"] == "remote_zip_sha256"
        and check["ok"]
        and check["actual"] == hashlib.sha256(payload).hexdigest()
        for check in body["checks"]
    )


@pytest.mark.integration
def test_delivery_doctor_reports_usable_config(tmp_path: Path, capsys):
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery_doctor.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
  file_delivery: auto
""",
        encoding="utf-8",
    )

    assert main(["--config", str(cfg), "delivery-doctor", "--file", str(sample)]) == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert "企业微信文件直发" in out
    assert "delivery-acceptance" in out


@pytest.mark.integration
def test_delivery_doctor_accepts_cli_overrides_for_personal_wechat_link(tmp_path: Path, capsys):
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery_doctor_overrides.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: false
""",
        encoding="utf-8",
    )

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-doctor",
            "--file",
            str(sample),
            "--pushplus-token",
            "pptok",
            "--remote-upload-url",
            "https://files.example.com",
        ])
        == 0
    )
    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["plan"]["backend"] == "pushplus"
    assert body["plan"]["requires_remote_upload"] is True
    assert body["overrides"]["deliveryBackend"] == "pushplus"
    assert body["overrides"]["deliveryPushplusToken"] == "***"
    assert body["overrides"]["deliveryRemoteUploadUrl"] == "https://files.example.com"
    assert "pptok" not in out
    assert "远程上传后发送个人微信下载链接" in body["plan"]["actions"]


@pytest.mark.integration
def test_delivery_doctor_reports_env_file_diagnostics_without_secrets(
    tmp_path: Path,
    capsys,
):
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    upload_token = "upload-secret-from-env-file"
    cfg = tmp_path / "delivery_doctor_env_file.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  send_file: false
""",
        encoding="utf-8",
    )
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f"""
SPIDER_GD_WECOM_WEBHOOK="{webhook}"
SPIDER_GD_REMOTE_UPLOAD_TOKEN="{upload_token}"
IGNORED_CUSTOM_KEY=custom
""",
        encoding="utf-8",
    )

    assert main([
        "--config",
        str(cfg),
        "delivery-doctor",
        "--env-file",
        str(env_file),
    ]) == 0

    out = capsys.readouterr().out
    body = json.loads(out)
    credentials = body["credentials"]
    env_diag = credentials["env_file"]
    assert body["ok"] is True
    assert credentials["backend"] == "wecom_webhook"
    assert credentials["selected_channel_configured"] is True
    assert credentials["channels"]["wecom_webhook"] is True
    assert env_diag["configured"] is True
    assert env_diag["loaded"] is True
    assert env_diag["path"] == str(env_file)
    assert env_diag["known_keys_present"] == [
        "SPIDER_GD_WECOM_WEBHOOK",
        "SPIDER_GD_REMOTE_UPLOAD_TOKEN",
    ]
    assert env_diag["ignored_key_count"] == 1
    assert webhook not in out
    assert upload_token not in out


@pytest.mark.integration
def test_delivery_doctor_flags_pending_outbox(tmp_path: Path, capsys):
    db_path = tmp_path / "spider.db"
    cfg = tmp_path / "delivery_doctor_pending.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
""",
        encoding="utf-8",
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store = TaskStateStore(db_path)
    try:
        store.enqueue_delivery_outbox(
            task_id="T_PENDING",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="waiting",
        )
    finally:
        store.close()

    assert main(["--config", str(cfg), "delivery-doctor"]) == 1
    out = capsys.readouterr().out
    assert '"pending": 1' in out
    assert '"task_id": "T_PENDING"' in out
    assert "delivery-retry" in out


@pytest.mark.integration
def test_delivery_retry_empty_outbox_returns_zero(tmp_path: Path, capsys):
    cfg = tmp_path / "delivery.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
  send_file: false
""",
        encoding="utf-8",
    )

    assert main(["--config", str(cfg), "delivery-retry"]) == 0
    out = capsys.readouterr().out
    assert '"retried": 0' in out


@pytest.mark.integration
@respx.mock
def test_delivery_retry_include_failed_requeues_and_sends(tmp_path: Path, capsys):
    db_path = tmp_path / "spider.db"
    cfg = tmp_path / "delivery.yaml"
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: {webhook}
  send_file: false
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store = TaskStateStore(db_path)
    try:
        store.enqueue_delivery_outbox(
            task_id="T_FAILED",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="send failed",
        )
        store.mark_delivery_outbox_failed("T_FAILED", "network down", status="failed")
    finally:
        store.close()
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert main(["--config", str(cfg), "delivery-retry", "--include-failed"]) == 0

    out = capsys.readouterr().out
    assert '"requeued_failed": 1' in out
    assert '"sent": 1' in out


@pytest.mark.integration
@respx.mock
def test_delivery_retry_reads_env_file_overrides_for_pending_outbox(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    db_path = tmp_path / "spider.db"
    cfg = tmp_path / "delivery_retry_env_file.yaml"
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    stale_webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=stale"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: false
  send_file: false
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    env_file = tmp_path / "delivery.env"
    env_file.write_text(
        f'SPIDER_GD_WECOM_WEBHOOK="{webhook}"\n',
        encoding="utf-8",
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store = TaskStateStore(db_path)
    try:
        store.enqueue_delivery_outbox(
            task_id="T_ENV_RETRY",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="waiting",
        )
    finally:
        store.close()

    monkeypatch.setenv("SPIDER_GD_WECOM_WEBHOOK", stale_webhook)
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-retry",
            "--env-file",
            str(env_file),
        ])
        == 0
    )

    out = capsys.readouterr().out
    body = json.loads(out)
    assert body["ok"] is True
    assert body["sent"] == 1
    assert body["overrides"]["deliveryWecomWebhook"] == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    )
    assert webhook not in out
    assert stale_webhook not in out


@pytest.mark.integration
def test_delivery_retry_returns_nonzero_when_zip_missing(tmp_path: Path, capsys):
    db_path = tmp_path / "spider.db"
    cfg = tmp_path / "delivery.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
  send_file: true
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    store = TaskStateStore(db_path)
    try:
        store.enqueue_delivery_outbox(
            task_id="T_MISSING",
            task_dir=str(tmp_path),
            zip_path=str(tmp_path / "missing.zip"),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="send failed",
        )
    finally:
        store.close()

    assert main(["--config", str(cfg), "delivery-retry"]) == 1

    out = capsys.readouterr().out
    assert '"ok": false' in out
    assert '"blocked": 1' in out
    assert "待补发文件不存在" in out


@pytest.mark.integration
@respx.mock
def test_delivery_retry_message_only_allows_missing_zip(tmp_path: Path, capsys):
    db_path = tmp_path / "spider.db"
    cfg = tmp_path / "delivery.yaml"
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: {webhook}
  send_file: false
  send_message: true
  retry_attempts: 1
  retry_delay_sec: 0
""",
        encoding="utf-8",
    )
    store = TaskStateStore(db_path)
    try:
        store.enqueue_delivery_outbox(
            task_id="T_MESSAGE_ONLY",
            task_dir=str(tmp_path),
            zip_path=str(tmp_path / "missing.zip"),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="send failed",
        )
    finally:
        store.close()
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": 0}))

    assert main(["--config", str(cfg), "delivery-retry"]) == 0

    out = capsys.readouterr().out
    assert '"ok": true' in out
    assert '"sent": 1' in out
    assert '"blocked": 0' in out


@pytest.mark.integration
def test_delivery_outbox_lists_requeues_and_cleans_records(tmp_path: Path, capsys):
    db_path = tmp_path / "spider.db"
    cfg = tmp_path / "delivery.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{db_path}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
""",
        encoding="utf-8",
    )
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"123456")
    store = TaskStateStore(db_path)
    try:
        store.enqueue_delivery_outbox(
            task_id="T_PENDING",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="waiting",
        )
        store.enqueue_delivery_outbox(
            task_id="T_FAILED",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 0, "failed": 1},
            error_msg="failed",
        )
        store.mark_delivery_outbox_failed("T_FAILED", "network down", status="failed")
        store.enqueue_delivery_outbox(
            task_id="T_SENT",
            task_dir=str(tmp_path),
            zip_path=str(zip_path),
            summary={"total": 1, "success": 1, "failed": 0},
            error_msg="sent",
        )
        store.mark_delivery_outbox_delivered("T_SENT")
    finally:
        store.close()

    assert main(["--config", str(cfg), "delivery-outbox", "--status", "pending"]) == 0
    out = capsys.readouterr().out
    assert '"pending": 1' in out
    assert '"task_id": "T_PENDING"' in out
    assert '"task_id": "T_FAILED"' not in out

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-outbox",
            "--status",
            "pending",
            "--requeue-failed",
        ])
        == 0
    )
    out = capsys.readouterr().out
    assert '"requeued_failed": 1' in out
    assert '"pending": 2' in out
    assert '"task_id": "T_FAILED"' in out

    assert (
        main([
            "--config",
            str(cfg),
            "delivery-outbox",
            "--status",
            "sent",
            "--cleanup",
            "sent",
        ])
        == 0
    )
    out = capsys.readouterr().out
    assert '"deleted": 1' in out
    assert '"sent": 0' in out


@pytest.mark.integration
def test_delivery_test_file_requires_send_file_enabled(tmp_path: Path, capsys):
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery_message_only.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
  send_message: true
  send_file: false
""",
        encoding="utf-8",
    )

    assert main(["--config", str(cfg), "delivery-test", "--file", str(sample)]) == 1
    out = capsys.readouterr().out
    assert "delivery.send_file=false" in out


@pytest.mark.integration
def test_delivery_test_dry_run_supports_serverchan_link_backend(tmp_path: Path, capsys):
    sample = tmp_path / "result.zip"
    sample.write_bytes(b"123456")
    cfg = tmp_path / "delivery_serverchan.yaml"
    cfg.write_text(
        f"""
storage:
  sqlite_path: "{tmp_path / 'spider.db'}"
  data_dir: "{tmp_path / 'data'}"
log:
  persist_to_file: false
delivery:
  enabled: true
  backend: serverchan
  serverchan_send_key: SCT123
  file_delivery: auto
  remote_upload_url: https://files.example.com
""",
        encoding="utf-8",
    )

    assert main(["--config", str(cfg), "delivery-test", "--file", str(sample)]) == 0
    out = capsys.readouterr().out
    assert "远程上传后发送个人微信下载链接" in out
