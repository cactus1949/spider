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
