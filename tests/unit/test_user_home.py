"""用户数据目录：相对路径改到 SPIDER_GD_HOME，并从安装目录复制旧数据。"""

from __future__ import annotations

from pathlib import Path

import pytest

from spider_gd.config.settings import AppSettings, load_config
from spider_gd.config.user_home import (
    HOME_ENV,
    migrate_install_data,
    rebase_path,
    rebase_user_paths,
    tasks_dir,
    user_workflows_dir,
)


@pytest.mark.unit
def test_rebase_relative_paths_into_home(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setenv(HOME_ENV, str(home))
    settings = rebase_user_paths(AppSettings())
    assert settings.storage.sqlite_path == str((home / "data" / "spider.db").resolve())
    assert settings.storage.data_dir == str((home / "data").resolve())
    assert settings.log.file_dir == str((home / "logs").resolve())
    assert settings.crawler.user_data_dir == str((home / "data" / ".browser").resolve())
    assert Path(settings.storage.sqlite_path).is_absolute()


@pytest.mark.unit
def test_absolute_and_memory_paths_stay(tmp_path: Path):
    home = tmp_path / "home"
    assert rebase_path(":memory:", home) == ":memory:"
    assert rebase_path(r"D:\软件安装\data\spider.db", home) == r"D:\软件安装\data\spider.db"
    assert rebase_path(str(tmp_path / "keep.db"), home) == str(tmp_path / "keep.db")


@pytest.mark.unit
def test_unset_home_keeps_relative_defaults(monkeypatch):
    monkeypatch.delenv(HOME_ENV, raising=False)
    settings = rebase_user_paths(AppSettings())
    assert settings.storage.sqlite_path == "./data/spider.db"
    assert tasks_dir() == Path.cwd() / "tasks"


@pytest.mark.unit
def test_migrate_copies_only_missing_user_data(tmp_path: Path):
    install = tmp_path / "install"
    home = tmp_path / "home"
    (install / "data").mkdir(parents=True)
    (install / "data" / "spider.db").write_text("old-db", encoding="utf-8")
    (install / "tasks").mkdir()
    (install / "tasks" / "daily.yml").write_text("name: 日常\n", encoding="utf-8")
    (install / "logs").mkdir()
    (install / "logs" / "app.log").write_text("hello", encoding="utf-8")
    (install / ".env").write_text("CRAWLER__CERT_PASSWORD=secret\n", encoding="utf-8")
    (install / "workflows").mkdir()
    (install / "workflows" / "spot.yml").write_text("name: spot\n", encoding="utf-8")

    copied = migrate_install_data(install, home)
    assert "data" in copied
    assert (home / "data" / "spider.db").read_text(encoding="utf-8") == "old-db"
    assert (home / "tasks" / "daily.yml").is_file()
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert env_text.startswith("CRAWLER__CERT_PASSWORD")

    (install / "data" / "spider.db").write_text("newer", encoding="utf-8")
    again = migrate_install_data(install, home)
    assert "data" not in again
    assert (home / "data" / "spider.db").read_text(encoding="utf-8") == "old-db"


@pytest.mark.unit
def test_load_config_rebases_when_home_set(monkeypatch, tmp_path: Path):
    home = tmp_path / "SpiderGD"
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "mode.yaml"
    config.write_text(
        "storage:\n  data_dir: ./data\n  sqlite_path: ./data/spider.db\n"
        "log:\n  file_dir: ./logs\n",
        encoding="utf-8",
    )
    settings = load_config(config)
    assert settings.storage.sqlite_path == str((home / "data" / "spider.db").resolve())
    assert settings.log.file_dir == str((home / "logs").resolve())


@pytest.mark.unit
def test_beta_rejects_configured_database_outside_its_home(monkeypatch, tmp_path: Path):
    home = tmp_path / "beta"
    formal = tmp_path / "formal.db"
    original = b"formal database must remain unchanged"
    formal.write_bytes(original)
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "mode.yaml"
    config.write_text(f"storage:\n  sqlite_path: '{formal}'\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"测试版.*storage\.sqlite_path"):
        load_config(config)

    assert formal.read_bytes() == original


@pytest.mark.unit
@pytest.mark.parametrize(
    ("group", "field"),
    [
        ("storage", "data_dir"),
        ("storage", "archive_dir"),
        ("log", "file_dir"),
        ("crawler", "user_data_dir"),
        ("delivery", "desktop_wechat_profile_path"),
    ],
)
def test_beta_rejects_other_persistent_targets_outside_home(
    monkeypatch, tmp_path: Path, group: str, field: str,
):
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "beta"))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "mode.yaml"
    config.write_text(f"{group}:\n  {field}: '{tmp_path / 'formal'}'\n", encoding="utf-8")

    with pytest.raises(ValueError, match=rf"测试版.*{group}\.{field}"):
        load_config(config)


@pytest.mark.unit
@pytest.mark.parametrize("folder", ["tasks", "workflows"])
def test_beta_rejects_linked_task_and_workflow_write_directories(
    monkeypatch, tmp_path: Path, folder: str,
):
    home = tmp_path / "beta"
    formal = tmp_path / "formal"
    home.mkdir()
    formal.mkdir()
    target = formal / "daily.yml"
    target.write_text("formal workflow", encoding="utf-8")
    (home / folder).symlink_to(formal, target_is_directory=True)
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")

    operation = tasks_dir if folder == "tasks" else user_workflows_dir
    kwargs = {} if folder == "tasks" else {"create": True}
    with pytest.raises(ValueError, match="测试版"):
        operation(**kwargs)

    assert target.read_text(encoding="utf-8") == "formal workflow"
    assert (home / folder).is_symlink()


@pytest.mark.unit
@pytest.mark.parametrize("field", ["sqlite_path", "file_dir"])
def test_beta_assemble_rejects_foreign_targets_before_initialization(
    monkeypatch, tmp_path: Path, field: str,
):
    from spider_gd.app import assemble

    home = tmp_path / "beta"
    formal = tmp_path / "formal"
    formal.mkdir()
    marker = formal / "marker"
    marker.write_bytes(b"formal data")
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.chdir(tmp_path)
    settings = AppSettings()
    settings.log.persist_to_file = False
    if field == "sqlite_path":
        settings.storage.sqlite_path = str(formal / "spider.db")
    else:
        settings.log.file_dir = str(formal)

    with pytest.raises(ValueError, match=rf"测试版.*{field}"):
        assemble(settings)

    assert marker.read_bytes() == b"formal data"
    assert list(formal.iterdir()) == [marker]
    assert not home.exists()


@pytest.mark.unit
@pytest.mark.parametrize(("kind", "filename"), [
    ("database", "spider.db"),
    ("database", "spider.db-wal"),
    ("database", "spider.db-shm"),
    ("database", "spider.db-journal"),
    ("logging", "app.log"),
    ("logging", "error.log"),
])
def test_beta_persistent_writers_reject_foreign_file_links(
    monkeypatch, tmp_path: Path, kind: str, filename: str,
):
    from spider_gd.config.logging import setup_logging
    from spider_gd.infrastructure.storage.database import TaskStateStore

    home = tmp_path / "beta"
    home.mkdir()
    formal = tmp_path / "formal"
    formal.write_bytes(b"formal data")
    link = home / filename
    link.symlink_to(formal)
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    operation = TaskStateStore if kind == "database" else setup_logging
    target = home / "spider.db" if kind == "database" else home

    with pytest.raises(ValueError, match="测试版"):
        operation(target)

    assert formal.read_bytes() == b"formal data"
    assert link.is_symlink()
    if filename != "spider.db":
        assert not (home / "spider.db").exists()


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["database", "logging"])
def test_beta_persistent_writers_reject_ambiguous_tilde_paths(tmp_path, monkeypatch, kind):
    from spider_gd.config.logging import setup_logging
    from spider_gd.infrastructure.storage.database import TaskStateStore

    user = tmp_path / "user"
    home = user / "beta"
    home.mkdir(parents=True)
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.chdir(working)
    operation = TaskStateStore if kind == "database" else setup_logging
    target = "~/beta/spider.db" if kind == "database" else "~/beta/logs"

    with pytest.raises(ValueError, match="测试版"):
        operation(target)

    assert list(working.iterdir()) == []
    assert list(home.iterdir()) == []


@pytest.mark.unit
def test_stable_versioned_writer_preserves_literal_tilde_path(tmp_path, monkeypatch):
    from spider_gd.core.pipeline.file_versions import save_versioned_bytes

    user = tmp_path / "user"
    user.mkdir()
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "stable")
    monkeypatch.chdir(working)

    result = save_versioned_bytes("~/report.txt", b"report")

    assert result.path == Path("~/report.txt")
    assert (working / "~" / "report.txt").read_bytes() == b"report"
    assert list(user.iterdir()) == []
