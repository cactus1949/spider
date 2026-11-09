"""诊断包文件上限要先留下任务、环境和执行日志。"""

from __future__ import annotations

import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from spider_gd.gateway.execution_bundle import (
    build_execution_zip,
    safe_read_log_window,
    select_runtime_artifacts,
)


@pytest.mark.unit
def test_file_cap_keeps_tasks_env_and_execution_logs(tmp_path: Path):
    failure = tmp_path / "_failures"
    failure.mkdir()
    for index in range(45):
        (failure / f"shot_{index:02d}.png").write_bytes(b"x")
    (tmp_path / "env.json").write_text('{"password": "hunter2", "name": "现场"}', encoding="utf-8")
    (tmp_path / "tasks").mkdir()
    task_file = tmp_path / "tasks" / "daily.yml"
    task_file.write_text("name: 日报\npassword: secret\n", encoding="utf-8")
    (tmp_path / "execution_logs").mkdir()
    (tmp_path / "execution_logs" / "run.txt").write_text("步骤失败\n", encoding="utf-8")

    files, notes = select_runtime_artifacts(tmp_path, None, None)

    names = [path.name for path in files]
    assert names[:3] == ["env.json", "run.txt", "daily.yml"] or set(names[:3]) == {
        "env.json",
        "run.txt",
        "daily.yml",
    }
    assert "env.json" in names
    assert "daily.yml" in names
    assert "run.txt" in names
    assert len(files) == 40
    assert any("40" in note for note in notes)

    target = tmp_path / "diag.zip"
    build_execution_zip(
        target,
        execution_text="步骤失败",
        app_log_text="日志",
        summary={"name": "样例", "status": "failed", "reason": ""},
        runtime_files=[
            ("runtime/env.json", tmp_path / "env.json"),
            ("runtime/tasks/daily.yml", tmp_path / "tasks" / "daily.yml"),
        ],
    )
    with zipfile.ZipFile(target) as archive:
        env_text = archive.read("runtime/env.json").decode("utf-8")
        task_text = archive.read("runtime/tasks/daily.yml").decode("utf-8")
    assert "hunter2" not in env_text
    assert "secret" not in task_text
    assert "***" in env_text
    assert "***" in task_text


@pytest.mark.unit
def test_priority_files_are_kept_outside_the_run_window(tmp_path: Path):
    (tmp_path / "tasks").mkdir()
    task = tmp_path / "tasks" / "a.yml"
    task.write_text("name: t\n", encoding="utf-8")
    old = datetime(2020, 1, 1, 0, 0, 0)
    start = datetime(2026, 11, 9, 10, 0, 0)
    files, _notes = select_runtime_artifacts(tmp_path, start, start)
    assert task.resolve() in {path.resolve() for path in files}
    assert old < start


@pytest.mark.unit
def test_safe_select_records_timeout_instead_of_raising(monkeypatch, tmp_path: Path):
    import spider_gd.gateway.execution_bundle as bundle

    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["Get-PnpDevice"], timeout=20)

    monkeypatch.setattr(bundle, "select_runtime_artifacts", boom)
    files, notes = bundle.safe_select_runtime_artifacts(tmp_path, None, None)
    assert files == []
    assert any("20" in note or "timed out" in note.lower() for note in notes)
    moment = datetime(2026, 11, 9, 10, 0, 0)
    text = safe_read_log_window(tmp_path / "missing.log", moment, moment)
    assert "不存在" in text
