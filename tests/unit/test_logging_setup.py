"""测试日志初始化、按次独立 sink 与脱敏。"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from spider_gd.config.logging import _mask_sensitive, attach_task_sink, setup_logging


@pytest.mark.unit
def test_setup_logging_creates_rolling_file(tmp_path: Path):
    setup_logging(tmp_path, persist_to_file=True)
    logger.bind(task="-").info("hello world")
    logger.complete()
    assert (tmp_path / "app.log").exists()


@pytest.mark.unit
def test_attach_task_sink_writes_isolated_file(tmp_path: Path):
    setup_logging(tmp_path, persist_to_file=False)
    sink_id = attach_task_sink(tmp_path, "日清算采集")
    logger.bind(task="日清算采集").info("task message")
    logger.complete()
    logger.remove(sink_id)

    day_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert day_dirs, "应创建按日期目录"
    log_files = list(day_dirs[0].glob("日清算采集_*.log"))
    assert log_files
    assert "task message" in log_files[0].read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["directory", "leaf"])
def test_beta_task_logging_rejects_foreign_links(monkeypatch, tmp_path: Path, kind: str):
    from datetime import datetime

    from spider_gd.config import logging as logging_module

    class Clock:
        @staticmethod
        def now():
            return datetime(2030, 1, 2, 3, 4, 5)

    home = tmp_path / "beta"
    logs = home / "logs"
    logs.mkdir(parents=True)
    formal = tmp_path / "formal"
    formal.mkdir()
    marker = formal / "marker"
    marker.write_bytes(b"formal data")
    day = logs / "2030-01-02"
    if kind == "directory":
        day.symlink_to(formal, target_is_directory=True)
    else:
        day.mkdir()
        (day / "采集_20300102_030405.log").symlink_to(marker)
    monkeypatch.setattr(logging_module, "datetime", Clock)
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    setup_logging(persist_to_file=False)

    with pytest.raises(ValueError, match="测试版"):
        attach_task_sink(logs, "采集")

    assert marker.read_bytes() == b"formal data"
    assert list(formal.iterdir()) == [marker]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["app", "error"])
@pytest.mark.parametrize("link_kind", ["existing", "dangling"])
def test_beta_log_rotation_preserves_foreign_archive_links(
    monkeypatch, tmp_path: Path, name: str, link_kind: str,
):
    from datetime import datetime

    from loguru import _file_sink

    from spider_gd.config import logging as logging_module

    home = tmp_path / "beta"
    logs = home / "logs"
    logs.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    marker = foreign / "marker"
    marker.write_bytes(b"foreign data")
    destination = marker if link_kind == "existing" else foreign / "missing.zip"
    archive = logs / f"{name}.2020-01-02_03-04-05_000000.log.zip"
    archive.symlink_to(destination)
    original_link = archive.readlink()
    (logs / "app.log").write_bytes(b"old app content\n" * 100)
    (logs / "error.log").write_bytes(b"old error content\n")
    created_at = datetime(2020, 1, 2, 3, 4, 5).timestamp()
    monkeypatch.setattr(_file_sink, "get_ctime", lambda _path: created_at)
    monkeypatch.setattr(logging_module, "DEFAULT_APP_LOG_MAX_SIZE_MB", 0.001)
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")

    try:
        setup_logging(logs)
        logger.bind(task="-").error("rotate logs")
        logger.complete()
    finally:
        logger.remove()

    assert marker.read_bytes() == b"foreign data"
    assert list(foreign.iterdir()) == [marker]
    assert archive.is_symlink()
    assert archive.readlink() == original_link


@pytest.mark.unit
@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_log_rotation_keeps_readable_current_and_previous_archives(
    monkeypatch, tmp_path: Path, channel: str,
):
    import zipfile
    from datetime import datetime

    from loguru import _file_sink

    from spider_gd.config import logging as logging_module

    home = tmp_path / channel
    logs = home / "logs"
    logs.mkdir(parents=True)
    content = b"old app content\n" * 100
    (logs / "app.log").write_bytes(content)
    existing = logs / "app.2020-01-02_03-04-05_000000.log.zip"
    with zipfile.ZipFile(existing, "w") as archive:
        archive.writestr("previous.log", b"previous archive")
    created_at = datetime(2020, 1, 2, 3, 4, 5).timestamp()
    monkeypatch.setattr(_file_sink, "get_ctime", lambda _path: created_at)
    monkeypatch.setattr(logging_module, "DEFAULT_APP_LOG_MAX_SIZE_MB", 0.001)
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", channel)

    try:
        setup_logging(logs, retention_days=36500)
        logger.bind(task="-").info("new app content")
        logger.complete()
    finally:
        logger.remove()

    payloads = []
    for path in logs.glob("*.zip"):
        with zipfile.ZipFile(path) as archive:
            payloads.extend(archive.read(name) for name in archive.namelist())
    assert b"previous archive" in payloads
    assert content in payloads
    assert "new app content" in (logs / "app.log").read_text(encoding="utf-8")


@pytest.mark.unit
def test_mask_sensitive_redacts_token_and_password():
    record = {"message": "password=secret123 token: abc.def HdKGccbL=xyz789&next"}
    _mask_sensitive(record)
    assert "secret123" not in record["message"]
    assert "abc.def" not in record["message"]
    assert "xyz789" not in record["message"]
