"""诊断包包含日志和配置，并且不留下明文密钥。"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from spider_gd.gateway.diagnostics_bundle import (
    build_diagnostics_zip,
    collect_secret_values,
    diagnostic_output_dir,
    partial_mask,
    redact_known_secrets,
)
from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.interfaces.storage import ExecutionLogRecord


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["diagnostics", "execution", "ukey", "receipt"])
def test_beta_exports_reject_foreign_output_links(monkeypatch, tmp_path: Path, kind: str):
    from spider_gd.app import write_delivery_receipt
    from spider_gd.gateway.execution_bundle import build_execution_zip, build_ukey_zip

    home = tmp_path / "beta"
    home.mkdir()
    formal = tmp_path / "formal"
    formal.write_bytes(b"formal data")
    output = home / ("receipt.json" if kind == "receipt" else "report.zip")
    output.symlink_to(formal)
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    if kind == "diagnostics":
        operation, args, kwargs = build_diagnostics_zip, (output,), {"settings": {}}
    elif kind == "execution":
        operation, args, kwargs = build_execution_zip, (output,), {
            "execution_text": "fixture", "app_log_text": "", "summary": {},
        }
    elif kind == "ukey":
        operation, args, kwargs = build_ukey_zip, (output,), {
            "report": {}, "raw_usb": "", "raw_certificates": "",
        }
    else:
        operation, args, kwargs = write_delivery_receipt, (home, {}), {"output_path": output}

    with pytest.raises(ValueError, match="测试版"):
        operation(*args, **kwargs)

    assert formal.read_bytes() == b"formal data"
    assert output.is_symlink()
    own = home / ("own.json" if kind == "receipt" else "own.zip")
    if kind == "receipt":
        kwargs["output_path"] = own
    else:
        args = (own,)
    operation(*args, **kwargs)
    if kind == "receipt":
        assert json.loads(own.read_text(encoding="utf-8"))["outbox"] == {}
    else:
        with zipfile.ZipFile(own) as archive:
            assert "README.txt" in archive.namelist()


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["requested", "saved", "probe", "target"])
def test_beta_diagnostic_directory_rejects_foreign_writes_without_fallback(
    monkeypatch, tmp_path: Path, kind: str,
):
    home = tmp_path / "beta"
    downloads = home / "downloads"
    data = home / "data"
    downloads.mkdir(parents=True)
    data.mkdir()
    formal = tmp_path / "formal"
    formal.mkdir()
    marker = formal / "marker"
    marker.write_bytes(b"formal data")
    requested = str(downloads)
    saved = ""
    if kind == "requested":
        requested = str(formal)
    elif kind == "saved":
        requested, saved = "", str(formal)
    elif kind == "probe":
        (downloads / ".spider-gd-write-probe").symlink_to(marker)
    else:
        requested = ""
        (data / "诊断包").symlink_to(formal, target_is_directory=True)
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")

    with pytest.raises(ValueError, match="测试版"):
        diagnostic_output_dir(requested, saved, data)

    assert marker.read_bytes() == b"formal data"
    assert list(formal.iterdir()) == [marker]


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
def test_missing_download_dir_does_not_create_another_users_path(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    saved = tmp_path / "D-download"
    saved.mkdir()
    missing = Path("C:/Users/Administrator/Desktop/download/1031根目录")
    chosen, note = diagnostic_output_dir(str(missing), str(saved), data_dir)
    assert chosen == saved / "诊断包"
    assert chosen.is_dir()
    assert "指定的下载目录不可用" in note
    assert not Path("C:/Users/Administrator").exists() or chosen != missing / "诊断包"
    nowhere, fallback = diagnostic_output_dir(str(missing), str(tmp_path / "absent"), data_dir)
    assert nowhere == data_dir / "诊断包"
    assert "数据目录" in fallback


@pytest.mark.unit
def test_bundle_keeps_env_when_file_limit_is_hit(tmp_path: Path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for index in range(50):
        (log_dir / f"app-{index:02d}.log").write_text(f"line-{index}\n", encoding="utf-8")
    monkeypatch.setattr("spider_gd.gateway.diagnostics_bundle._MAX_FILES", 8)
    dest = tmp_path / "diag.zip"
    notes = build_diagnostics_zip(
        dest,
        settings={},
        log_dir=log_dir,
        app_version="0.2.1",
        now=datetime(2026, 11, 5, 12, 0, 0),
    )
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
        env = json.loads(archive.read("env.json").decode("utf-8"))
    assert "env.json" in names
    assert "README.txt" in names
    assert env["app_version"] == "0.2.1"
    assert env.get("spider_gd")
    assert "frozen" in env
    assert any("文件上限" in item for item in notes)
    assert len(names) <= 8


@pytest.mark.unit
def test_bundle_keeps_tasks_when_workflows_fill_the_cap(tmp_path: Path, monkeypatch):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    for index in range(30):
        (workflows / f"wf-{index:02d}.yml").write_text(f"name: wf-{index}\n", encoding="utf-8")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "daily.yml").write_text("name: 日常\ncert_password: pin-7788\n", encoding="utf-8")
    monkeypatch.setattr("spider_gd.gateway.diagnostics_bundle._MAX_FILES", 6)
    dest = tmp_path / "diag.zip"
    build_diagnostics_zip(
        dest,
        settings={"certPassword": "pin-7788"},
        workflow_dirs=[workflows],
        tasks_dir=tasks,
        app_version="0.2.4",
        now=datetime(2026, 11, 18, 8, 55, 0),
    )
    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
        env = json.loads(archive.read("env.json").decode("utf-8"))
        task = archive.read("tasks/daily.yml").decode("utf-8")
    assert "env.json" in names
    assert "README.txt" in names
    assert "tasks/daily.yml" in names
    assert env["spider_gd"]
    assert env["app_version"] == "0.2.4"
    assert "pin-7788" not in task


@pytest.mark.unit
@pytest.mark.parametrize("crowded", ["logs", "workflows", "executions"])
def test_bundle_keeps_latest_failure_and_execution_before_bulk_material(
    tmp_path: Path, monkeypatch, crowded: str,
):
    import os

    moment = datetime(2026, 11, 25, 1, 0, 0)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text("current app\n", encoding="utf-8")
    (log_dir / "error.log").write_text("current error\n", encoding="utf-8")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    failures = tmp_path / "downloads" / "_failures"
    failures.mkdir(parents=True)
    (failures / "current.html").write_text("failed query pin-7788", encoding="utf-8")
    for index in range(30):
        old = failures / f"old-{index:02d}.html"
        old.write_text("old failure", encoding="utf-8")
        os.utime(old, (moment.timestamp() - 60, moment.timestamp() - 60))
    store = TaskStateStore(tmp_path / "spider.db")
    try:
        store.create_execution_log(ExecutionLogRecord(
            exec_id="current", name="query", type="workflow", target_id="query",
            status="failed", reason="timeout", detail_log="pin-7788",
            created_at="2026-11-25 01:00:00", updated_at="2026-11-25 01:00:00",
        ))
        for index in range(40):
            if crowded == "logs":
                (log_dir / f"00-archive-{index:02d}.log").write_text("archive", encoding="utf-8")
            elif crowded == "workflows":
                (workflows / f"wf-{index:02d}.yml").write_text("name: workflow", encoding="utf-8")
            else:
                store.create_execution_log(ExecutionLogRecord(
                    exec_id=f"old-{index}", name="old", type="workflow", target_id="old",
                    status="success", created_at="2026-11-24 23:00:00",
                    updated_at="2026-11-24 23:00:00",
                ))
        monkeypatch.setattr("spider_gd.gateway.diagnostics_bundle._MAX_FILES", 8)
        dest = tmp_path / "diag.zip"
        notes = build_diagnostics_zip(
            dest, settings={"certPassword": "pin-7788"}, store=store,
            log_dir=log_dir, workflow_dirs=[workflows],
            download_dirs=[tmp_path / "downloads"], now=moment,
        )
        with zipfile.ZipFile(dest) as archive:
            names = archive.namelist()
            assert "env.json" in names
            assert "README.txt" in names
            assert "execution_logs/current.txt" in names
            assert "logs/app.log" in names
            assert "logs/error.log" in names
            failure = next(name for name in names if name.endswith("current.html"))
            assert "pin-7788" not in archive.read(failure).decode("utf-8")
            assert "pin-7788" not in archive.read("execution_logs/current.txt").decode("utf-8")
        assert len(names) <= 8
        assert any("文件上限" in note for note in notes)
    finally:
        store.close()


@pytest.mark.parametrize("entrypoint", [
    pytest.param("zip", marks=pytest.mark.unit),
    pytest.param("api", marks=pytest.mark.integration),
])
def test_latest_failure_scene_survives_default_byte_pressure(tmp_path: Path, monkeypatch, entrypoint):
    import os
    from datetime import timedelta

    from spider_gd.config.delivery_env import DELIVERY_ENV_KEYS

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SPIDER_GD_HOME", str(tmp_path))
    # Beta skips legacy-data migration; every input/output stays under the synthetic home.
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    for key in (*DELIVERY_ENV_KEYS, "SPIDER_GD_DELIVERY_ENV_FILE", "SPIDER_BROWSER_CONFIG"):
        monkeypatch.delenv(key, raising=False)

    mib = 1024 * 1024
    moment = datetime.now().replace(microsecond=0)
    stamp = moment.strftime("%Y%m%d_%H%M%S")
    older = moment - timedelta(minutes=1)
    old_stamp = older.strftime("%Y%m%d_%H%M%S")
    log_dir = tmp_path / "logs"
    downloads = tmp_path / "downloads"
    failures = downloads / "_failures"
    workflows = tmp_path / "workflows"
    tasks = tmp_path / "tasks"
    html = failures / f"login_{stamp}.html"
    screenshot = failures / f"login_error_{stamp}.png"
    diagnostic = failures / f"login_diagnostics_{stamp}.log"
    # The login producer uses different prefixes for the same scene's three files.
    materials = [
        (log_dir / "app.log", 8 * mib, b"\ncurrent app pin-7788\n", 0),
        (log_dir / "error.log", 8 * mib, b"\ncurrent error pin-7788\n", 1),
        (log_dir / "app.log.1", 8 * mib, b"\narchived app\n", 60),
        (html, 3 * mib, b"\n<html>current login pin-7788</html>\n", 2),
        (diagnostic, 64 * 1024, b"\ncurrent diagnostic profile-secret\n", 3),
        (screenshot, 6 * mib, b"\x89PNG\r\n\x1a\ncurrent screenshot", 4),
        (failures / f"login_{old_stamp}.html", 8 * mib, b"\nolder HTML\n", 60),
        (failures / f"login_error_{old_stamp}.png", 8 * mib, b"older screenshot", 61),
        (failures / f"diagnostics_{old_stamp}.log", 8 * mib, b"older diagnostic", 62),
        (failures / f"captcha_exhausted_{old_stamp}.png", 8 * mib, b"unrelated screenshot", 63),
        (workflows / "bulk.yml", mib, b"\nname: bulk\ntoken: unlisted-token\n", 60),
        (tasks / "daily.yml", 128, b"\nname: daily\ncert_password: pin-7788\n", 0),
    ]
    for path, size, tail, age in materials:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (size - len(tail)) + tail)
        mtime = moment.timestamp() - age
        os.utime(path, (mtime, mtime))

    ctx = None
    if entrypoint == "api":
        from fastapi.testclient import TestClient

        from spider_gd.app import assemble
        from spider_gd.config import load_config
        from spider_gd.core.crawler import shared_browser
        from spider_gd.gateway.api.server import create_app
        from spider_gd.gateway.control_service import ControlService

        config = load_config(str(Path(__file__).resolve().parents[2] / "configs/mode_d_offline.yaml"))
        config.storage.sqlite_path = str(tmp_path / "spider.db")
        config.storage.data_dir = str(tmp_path / "data")
        config.log.file_dir = str(log_dir)
        config.log.persist_to_file = False
        monkeypatch.setattr(shared_browser, "_shared_browser", None)
        monkeypatch.setattr("spider_gd.config.user_home.bundled_workflows_dir", lambda: workflows)
        monkeypatch.setattr(
            "spider_gd.gateway.api.server._diagnostic_host_facts",
            lambda _service: ({"fixture": True}, "fixture-wifi", {"device_count": 0}),
        )
        ctx = assemble(config)
        store = ctx.state_store
    else:
        store = TaskStateStore(tmp_path / "spider.db")
    try:
        settings = {
            "certPassword": "pin-7788",
            "account": "alice",
            "stationProfiles": json.dumps([{
                "id": "fixture", "name": "fixture", "certPassword": "profile-secret",
            }]),
        }
        store.save_settings(settings)
        for exec_id, created, marker in (
            ("current", moment, "current execution pin-7788"),
            ("older", older, "older execution"),
        ):
            created_at = created.strftime("%Y-%m-%d %H:%M:%S")
            store.create_execution_log(ExecutionLogRecord(
                exec_id=exec_id, name="login", type="workflow", target_id="login",
                status="failed", reason="timeout", detail_log="e" * (8 * mib) + "\n" + marker,
                created_at=created_at, updated_at=created_at,
            ))
        if entrypoint == "api":
            with TestClient(create_app(ControlService(ctx))) as client:
                response = client.post("/api/diagnostics/bundle", json={
                    "download_dir": str(downloads), "app_version": "fixture-version",
                })
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "ok"
            dest = Path(body["path"])
            assert dest.parent == downloads / "诊断包"
            assert body["filename"] == dest.name
            assert "总大小上限" in body["message"]
        else:
            dest = tmp_path / "diag.zip"
            notes = build_diagnostics_zip(
                dest, settings=settings, store=store, log_dir=log_dir,
                download_dirs=[downloads], workflow_dirs=[workflows], tasks_dir=tasks,
                extra_secrets=["profile-secret"], now=moment, app_version="fixture-version",
            )
            assert any("总大小上限" in note for note in notes)
        with zipfile.ZipFile(dest) as archive:
            names = archive.namelist()
            # Inspect the ZIP, not just reported selections; the screenshot must survive scrubbing.
            for path in (screenshot, html, diagnostic):
                matches = [name for name in names if name.endswith("/" + path.name)]
                assert len(matches) == 1, f"missing newest artifact: {path.name}; ZIP: {names}"
                if path == screenshot:
                    assert archive.read(matches[0]) == screenshot.read_bytes()
                else:
                    assert b"current" in archive.read(matches[0])
            assert {"env.json", "README.txt", "tasks/daily.yml", "logs/app.log",
                    "logs/error.log", "execution_logs/current.txt", "workflows/bulk.yml"} <= set(names)
            assert "execution_logs/older.txt" not in names
            assert "logs/app.log.1" not in names
            assert not any(old_stamp in name for name in names)
            assert b"current execution ***" in archive.read("execution_logs/current.txt")
            for name in names:
                blob = archive.read(name)
                assert not any(secret in blob for secret in (
                    b"pin-7788", b"profile-secret", b"unlisted-token",
                ))
            env = json.loads(archive.read("env.json"))
            assert env["app_version"] == "fixture-version"
            assert env["settings"]["certPassword"] == "***"
            assert env["settings"]["account"] == "a***e"
            infos = archive.infolist()
            assert len(infos) <= 40
            assert max(info.file_size for info in infos) <= 8 * mib
            assert sum(info.file_size for info in infos) <= 40 * mib
    finally:
        store.close()


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


@pytest.mark.unit
def test_existing_unwritable_dir_falls_back_without_creating_it(tmp_path: Path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    try:
        chosen, note = diagnostic_output_dir(str(locked), "", data_dir)
    finally:
        locked.chmod(0o755)
    assert chosen == data_dir / "诊断包"
    assert chosen.is_dir()
    assert "指定的下载目录不可用" in note
    assert not (locked / "诊断包").exists()
