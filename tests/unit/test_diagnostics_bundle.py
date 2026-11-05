"""诊断包包含日志和配置，并且不留下明文密钥。"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from spider_gd.gateway.diagnostics_bundle import (
    build_diagnostics_zip,
    collect_secret_values,
    partial_mask,
    redact_known_secrets,
)
from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.interfaces.storage import ExecutionLogRecord


@pytest.mark.unit
def test_partial_mask_and_known_secret_strip():
    assert partial_mask("alice") == "a***e"
    secrets = collect_secret_values({
        "certPassword": "pin-7788",
        "account": "alice",
        "siteUrl": "https://pm.gd.csg.cn",
        "deliveryWecomWebhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123xyz",
    })
    assert "pin-7788" in secrets
    assert "alice" not in secrets
    text = redact_known_secrets("使用 pin-7788 登录 alice", secrets)
    assert "pin-7788" not in text


@pytest.mark.unit
def test_bundle_contains_expected_files_and_redacts_secrets(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text("证书密码 pin-7788\n", encoding="utf-8")
    (log_dir / "error.log").write_text(
        "webhook https://qyapi.weixin.qq.com/key/abc123xyz\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "daily.yml").write_text("name: 日常\ncert_password: pin-7788\n", encoding="utf-8")
    failures = tmp_path / "downloads" / "_failures"
    failures.mkdir(parents=True)
    (failures / "login_diagnostics_20261105.log").write_text(
        "pin-7788", encoding="utf-8",
    )
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "spot.yml").write_text("name: 现货\n", encoding="utf-8")

    store = TaskStateStore(tmp_path / "spider.db")
    store.create_execution_log(ExecutionLogRecord(
        exec_id="e1",
        name="日常",
        type="scheduled_manual",
        target_id="t1",
        status="failed",
        reason="等待元素超时",
        detail_log="填了 pin-7788",
        created_at="2026-11-05 09:00:00",
        updated_at="2026-11-05 09:10:00",
    ))
    store.save_workflow_config({
        "id": "spot",
        "name": "现货",
        "yaml_text": "name: 现货\npassword: pin-7788\n",
    })
    dest = tmp_path / "out" / "diag.zip"
    secrets = ["pin-7788", "abc123xyz"]
    build_diagnostics_zip(
        dest,
        settings={
            "certPassword": "pin-7788",
            "account": "alice",
            "siteUrl": "https://pm.gd.csg.cn",
            "wifiPassword": "wifi-9988",
        },
        store=store,
        log_dir=log_dir,
        download_dirs=[tmp_path / "downloads"],
        workflow_dirs=[workflows],
        tasks_dir=tasks,
        install_path=str(tmp_path / "install"),
        data_path=str(tmp_path / "data"),
        extra_secrets=secrets,
        wifi_ssid="办公WiFi",
        now=datetime(2026, 11, 5, 12, 0, 0),
    )
    with zipfile.ZipFile(dest) as archive:
        names = set(archive.namelist())
        blob = "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in names
            if not name.endswith(".png")
        )
    assert "env.json" in names
    assert any(name.startswith("logs/") for name in names)
    assert any(name.startswith("execution_logs/") for name in names)
    assert any(name.startswith("workflows/") for name in names)
    assert any(name.startswith("tasks/") for name in names)
    assert any("login_diagnostics" in name or name.startswith("failures/") for name in names)
    assert "pin-7788" not in blob
    assert "wifi-9988" not in blob
    assert "abc123xyz" not in blob
    assert "a***e" in blob or "alice" not in blob
    store.close()
