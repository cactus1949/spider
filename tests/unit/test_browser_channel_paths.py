from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from spider_gd import build_stamp
from spider_gd.core.crawler import shared_browser


def test_beta_browser_settings_do_not_overwrite_stable_session(tmp_path, monkeypatch):
    local = tmp_path / "local"
    beta_home = tmp_path / "beta"
    stable_session = local / "SpiderGD" / "browser-session.json"
    stable_session.parent.mkdir(parents=True)
    original = b'{"config":{"auth_policy":"reuse_or_auto"},"run":null}'
    stable_session.write_bytes(original)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(beta_home))
    monkeypatch.delenv("SPIDER_BROWSER_CONFIG", raising=False)
    monkeypatch.setattr(shared_browser, "_shared_browser", None)

    browser = shared_browser.get_shared_browser()
    assert browser.status()["config"]["auth_policy"] == "reuse_only"
    browser.configure(auth_policy="manual")

    assert json.loads(stable_session.read_bytes())["config"]["auth_policy"] == "reuse_or_auto"
    assert stable_session.read_bytes() == original
    saved = json.loads((beta_home / "browser-session.json").read_text(encoding="utf-8"))
    assert saved["config"]["auth_policy"] == "manual"
    assert saved["config"]["profile_dir"] == str(beta_home / "browser-profiles" / "default")


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("frozen", [False, True])
def test_default_beta_browser_persists_only_in_its_platform_home(
    tmp_path, monkeypatch, platform, frozen,
):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(build_stamp, "RELEASE_CHANNEL", "beta")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "stable" if frozen else "beta")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SPIDER_GD_HOME", raising=False)
    monkeypatch.delenv("SPIDER_BROWSER_CONFIG", raising=False)
    monkeypatch.setattr(shared_browser, "_shared_browser", None)
    expected = {
        "win32": tmp_path / "roaming" / "spider-gd-desktop-beta",
        "darwin": tmp_path / "Library" / "Application Support" / "spider-gd-desktop-beta",
        "linux": tmp_path / "xdg" / "spider-gd-desktop-beta",
    }[platform]

    shared_browser.get_shared_browser().configure(auth_policy="manual")

    saved = json.loads((expected / "browser-session.json").read_text(encoding="utf-8"))
    assert saved["config"]["auth_policy"] == "manual"
    assert saved["config"]["profile_dir"] == str(expected / "browser-profiles" / "default")


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("frozen", [False, True])
def test_stable_browser_preserves_legacy_directory_despite_home_override(
    tmp_path, monkeypatch, platform, frozen,
):
    beta_home = tmp_path / "beta"
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(build_stamp, "RELEASE_CHANNEL", "stable")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta" if frozen else "stable")
    monkeypatch.setenv("SPIDER_GD_HOME", str(beta_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("SPIDER_BROWSER_CONFIG", raising=False)
    monkeypatch.setattr(shared_browser, "_shared_browser", None)
    expected = {
        "win32": tmp_path / "local" / "SpiderGD",
        "darwin": tmp_path / "Library" / "Application Support" / "SpiderGD",
        "linux": tmp_path / ".local" / "share" / "spider-gd",
    }[platform]

    shared_browser.get_shared_browser().configure(auth_policy="reuse_or_auto")

    saved = json.loads((expected / "browser-session.json").read_text(encoding="utf-8"))
    assert saved["config"]["auth_policy"] == "reuse_or_auto"
    assert saved["config"]["profile_dir"] == str(expected / "browser-profiles" / "default")
    assert not (beta_home / "browser-session.json").exists()


def test_beta_rejects_foreign_browser_state_before_reading_it(tmp_path, monkeypatch):
    formal = tmp_path / "formal-session.json"
    original = b'{"config":{"auth_policy":"reuse_or_auto"},"run":null}'
    formal.write_bytes(original)
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(tmp_path / "beta"))
    monkeypatch.setenv("SPIDER_BROWSER_CONFIG", str(formal))
    monkeypatch.setattr(shared_browser, "_shared_browser", None)

    with pytest.raises(ValueError, match=r"测试版.*浏览器状态"):
        shared_browser.get_shared_browser()

    assert formal.read_bytes() == original


@pytest.mark.parametrize("setting", ["profile", "download"])
def test_beta_browser_rejects_external_persistent_directory(tmp_path, monkeypatch, setting):
    home = tmp_path / "beta"
    formal = tmp_path / "formal"
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    browser = shared_browser.SharedBrowser(home / "browser-session.json")

    operation = browser.configure if setting == "profile" else browser.set_download_dir
    argument = "profile_dir" if setting == "profile" else "directory"
    with pytest.raises(ValueError, match="测试版"):
        operation(**{argument: str(formal)})

    assert browser.status()["config"]["profile_dir"] == str(home / "browser-profiles" / "default")
    assert not (home / "browser-session.json").exists()
    assert not formal.exists()


def test_beta_browser_save_rejects_temporary_file_link_before_writing(tmp_path, monkeypatch):
    home = tmp_path / "beta"
    home.mkdir()
    formal = tmp_path / "formal-session.json"
    original = b"formal session remains unchanged"
    formal.write_bytes(original)
    temporary = home / "browser-session.tmp"
    temporary.symlink_to(formal)
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    browser = shared_browser.SharedBrowser(home / "browser-session.json")

    with pytest.raises(ValueError, match="测试版"):
        browser.configure(auth_policy="manual")

    assert formal.read_bytes() == original
    assert temporary.is_symlink()
    assert not (home / "browser-session.json").exists()


async def test_beta_saved_external_profile_is_rejected_before_chrome_start(tmp_path, monkeypatch):
    home = tmp_path / "beta"
    home.mkdir()
    formal = tmp_path / "formal"
    state = home / "browser-session.json"
    state.write_text(json.dumps({"config": {"profile_dir": str(formal)}}), encoding="utf-8")
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    monkeypatch.setattr(shared_browser, "_get_chrome_path", lambda: "fixture-chrome")

    def forbidden_start(*args, **kwargs):
        pytest.fail("无效 Profile 不能启动 Chrome")

    monkeypatch.setattr(shared_browser.subprocess, "Popen", forbidden_start)
    browser = shared_browser.SharedBrowser(state)

    with pytest.raises(ValueError, match="测试版"):
        await browser.start()

    assert not formal.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf", ["chrome.log", "spider-cdp-endpoint", "downloads"])
async def test_beta_start_rejects_foreign_browser_sidecars_before_launch(
    monkeypatch, tmp_path, leaf,
):
    home = tmp_path / "beta"
    profile = home / "profile"
    profile.mkdir(parents=True)
    formal = tmp_path / "formal"
    formal.mkdir()
    marker = formal / "marker"
    marker.write_text("formal data", encoding="utf-8")
    link = profile / leaf
    link.symlink_to(formal if leaf == "downloads" else marker,
                    target_is_directory=leaf == "downloads")
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    monkeypatch.setattr(shared_browser, "_get_chrome_path", lambda: "fixture-chrome")

    def forbidden_start(*args, **kwargs):
        pytest.fail("越界的浏览器文件不能启动 Chrome")

    monkeypatch.setattr(shared_browser.subprocess, "Popen", forbidden_start)
    browser = shared_browser.SharedBrowser(home / "state.json")
    browser.configure(mode="managed", profile_dir=str(profile))

    with pytest.raises(ValueError, match="测试版"):
        await browser.start()

    assert marker.read_text(encoding="utf-8") == "formal data"
    assert link.is_symlink()


@pytest.fixture
async def beta_managed_browser(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    home = tmp_path / "beta"
    profile = home / "profile"
    profile.mkdir(parents=True)
    (profile / "spider-cdp-endpoint").write_text(
        "ws://127.0.0.1:9222/devtools/browser/fixture", encoding="utf-8",
    )
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setattr(shared_browser, "_get_chrome_path", lambda: "fixture-chrome")

    def forbidden_start(*args, **kwargs):
        pytest.fail("fixture 不应启动本机 Chrome")

    monkeypatch.setattr(shared_browser.subprocess, "Popen", forbidden_start)
    events = {}
    page = MagicMock()
    page.url = "about:blank"
    page.is_closed.return_value = False
    page.title = AsyncMock(return_value="Fixture")
    page.goto = AsyncMock()
    page.on.side_effect = lambda event, callback: events.__setitem__(event, callback)
    context = MagicMock(pages=[page])
    page.context = context
    remote = MagicMock(contexts=[context])
    remote.is_connected.return_value = True
    driver = MagicMock()
    driver.chromium.connect_over_cdp = AsyncMock(return_value=remote)
    driver.stop = AsyncMock()
    manager = MagicMock()
    manager.start = AsyncMock(return_value=driver)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: manager)
    browser = shared_browser.SharedBrowser(home / "state.json")
    browser.configure(mode="managed", profile_dir=str(profile))
    await browser.start(url="about:blank")
    try:
        yield browser, home, profile, events
    finally:
        await browser.detach()


async def test_beta_clearing_download_override_rejects_foreign_fallback(
    beta_managed_browser, tmp_path,
):
    browser, home, profile, _ = beta_managed_browser
    browser.set_download_dir(str(home / "chosen"))
    foreign = tmp_path / "formal"
    foreign.mkdir()
    fallback = profile / "downloads"
    fallback.rmdir()
    fallback.symlink_to(foreign, target_is_directory=True)

    with pytest.raises(ValueError, match="测试版"):
        browser.set_download_dir("")

    assert list(foreign.iterdir()) == []
    assert fallback.is_symlink()


@pytest.mark.parametrize("kind", ["directory", "leaf", "collision", "own"])
async def test_beta_managed_download_events_preserve_foreign_files(
    beta_managed_browser, tmp_path, monkeypatch, kind,
):
    import uuid
    from types import SimpleNamespace

    browser, _, profile, events = beta_managed_browser
    foreign = tmp_path / "formal"
    foreign.mkdir()
    marker = foreign / "marker"
    marker.write_bytes(b"formal data")
    downloads = profile / "downloads"
    target = downloads / "report.txt"
    if kind == "directory":
        downloads.rmdir()
        downloads.symlink_to(foreign, target_is_directory=True)
    elif kind == "leaf":
        target.symlink_to(foreign / "missing.txt")
    elif kind == "collision":
        target.write_bytes(b"earlier report")
        (downloads / "report-12345678.txt").symlink_to(marker)
        monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="12345678abcdef"))

    async def save_as(path):
        Path(path).write_bytes(b"download report")

    events["download"](SimpleNamespace(suggested_filename="report.txt", save_as=save_as))
    await browser.detach()

    assert marker.read_bytes() == b"formal data"
    assert list(foreign.iterdir()) == [marker]
    if kind == "own":
        assert target.read_bytes() == b"download report"
    elif kind == "directory":
        assert downloads.is_symlink()
    elif kind == "leaf":
        assert target.is_symlink()
    else:
        assert target.read_bytes() == b"earlier report"
        assert (downloads / "report-12345678.txt").is_symlink()
