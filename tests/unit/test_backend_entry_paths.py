"""Bootstrap reads beta credentials without changing external dotenv inputs."""

from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import backend_entry
import pytest

from spider_gd import build_stamp
from spider_gd.release_channel import runtime_release


@pytest.mark.unit
@pytest.mark.parametrize("channel", ["beta", "stable"])
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_main_loads_gbk_env_preserving_beta_input(
    tmp_path: Path, monkeypatch, channel: str, frozen: bool,
):
    install = tmp_path / "install"
    configs = install / "configs"
    configs.mkdir(parents=True)
    (configs / "mode_a_wired.yaml").write_text("{}\n", encoding="utf-8")
    executable = install / "spider-gd-service.exe"
    executable.write_bytes(b"fake executable; never launched")
    home = tmp_path / "home"
    home.mkdir()
    foreign = tmp_path / "foreign.env"
    content = "CRAWLER__PASSWORD=测试密码\n"
    original = content.encode("gbk")
    foreign.write_bytes(original)
    env_link = install / ".env"
    env_link.symlink_to(foreign)
    link_target = env_link.readlink()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {
        "SPIDER_GD_HOME": str(home),
        "SPIDER_GD_CHANNEL": ("stable" if channel == "beta" else "beta") if frozen else channel,
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "argv", [str(executable)])
    monkeypatch.setattr(sys, "_MEIPASS", str(install), raising=False)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(backend_entry, "__file__", str(install / "backend_entry.py"))
    compiled_channel = channel if frozen else ("stable" if channel == "beta" else "beta")
    monkeypatch.setattr(build_stamp, "RELEASE_CHANNEL", compiled_channel)
    monkeypatch.setattr(
        build_stamp, "RELEASE_VERSION", "0.2.8-beta.7" if compiled_channel == "beta" else "0.2.8",
    )
    assert runtime_release()["channel"] == channel

    captured = []
    app = ModuleType("spider_gd.app")

    def run_app(settings):
        captured.append(settings)
        return 0

    app.run_app = run_app
    monkeypatch.setitem(sys.modules, "spider_gd.app", app)

    assert backend_entry.main() == 0
    assert len(captured) == 1
    assert captured[0].crawler.password == "测试密码"
    assert captured[0].web.port == (8801 if channel == "beta" else 8800)
    assert captured[0].storage.sqlite_path == str(home / "data" / "spider.db")
    assert Path.cwd() == install
    assert env_link.is_symlink()
    assert env_link.readlink() == link_target
    assert foreign.read_bytes() == (original if channel == "beta" else content.encode("utf-8"))
    if channel == "beta":
        assert list(home.iterdir()) == []
        assert "CRAWLER__PASSWORD" not in os.environ
    assert not (home / "data" / "spider.db").exists()


@pytest.mark.unit
@pytest.mark.parametrize("channel", ["stable", "beta"])
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_build_self_test_dispatch_is_read_only_and_preserves_runtime_identity(
    monkeypatch, tmp_path, capsys, channel, frozen, platform,
):
    _exercise_build_self_test(monkeypatch, tmp_path, capsys, channel, frozen, platform)


@pytest.mark.unit
@pytest.mark.parametrize("failure", [
    "pythoncom", "initialize", "wmi", "win32com.client", "pywintypes", "uiautomation",
])
def test_build_self_test_dependency_failure_is_nonzero_with_diagnostics(
    monkeypatch, tmp_path, capsys, failure,
):
    _exercise_build_self_test(monkeypatch, tmp_path, capsys, "beta", True, "win32", failure)


def _exercise_build_self_test(
    monkeypatch, tmp_path, capsys, channel, frozen, platform, failure=None,
):
    from spider_gd import __version__

    env_file = tmp_path / ".env"
    original = "PASSWORD=测试\n".encode("gbk")
    env_file.write_bytes(original)
    home = tmp_path / "must-not-create-home"
    env = {
        "SPIDER_GD_CHANNEL": ("stable" if channel == "beta" else "beta") if frozen else channel,
        "SPIDER_GD_HOME": str(home),
    }
    monkeypatch.setattr(os, "environ", env.copy())
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "argv", ["fake.exe", "--self-test-build"])
    monkeypatch.setattr(build_stamp, "BUILD_ID", "012345abcdef" if frozen else "source")
    monkeypatch.setattr(
        build_stamp, "RELEASE_CHANNEL",
        channel if frozen else ("stable" if channel == "beta" else "beta"),
    )
    monkeypatch.setattr(
        build_stamp, "RELEASE_VERSION", "0.2.8-beta.1" if channel == "beta" else "0.2.8",
    )
    monkeypatch.setattr(backend_entry, "_fix_console", lambda: None)
    for name in (
        "_dispatch_picker", "_dispatch_ocr_self_test", "_exe_dir", "_unpack_configs",
        "_fix_env_encoding",
    ):
        monkeypatch.setattr(
            backend_entry, name, lambda *a: pytest.fail("self-test reached normal bootstrap"),
        )
    monkeypatch.setattr(os, "chdir", lambda *a: pytest.fail("self-test must not change cwd"))
    monkeypatch.setattr(
        os, "mkdir", lambda *a, **k: pytest.fail("self-test must not initialize directories"),
    )
    monkeypatch.setattr(Path, "home", lambda: pytest.fail("self-test must not resolve HOME"))
    monkeypatch.setattr(
        Path, "write_bytes", lambda *a: pytest.fail("self-test must not write files"),
    )
    monkeypatch.setattr(
        Path, "write_text", lambda *a, **k: pytest.fail("self-test must not write files"),
    )
    events = []

    def initialize():
        events.append("initialize")
        if failure == "initialize":
            raise RuntimeError("initialize failed")

    pythoncom = SimpleNamespace(
        CoInitialize=initialize, CoUninitialize=lambda: events.append("uninitialize"),
    )
    real_import = builtins.__import__
    ports = ("pythoncom", "wmi", "win32com.client", "pywintypes", "uiautomation")

    def import_port(name, *args, **kwargs):
        if name in ports:
            assert platform == "win32", "non-Windows must not attempt native imports"
            events.append(name)
            if name != "pythoncom":
                assert "initialize" in events, "COM initialization must precede dynamic imports"
            if name == failure:
                raise ImportError(f"missing {name}")
            return pythoncom if name == "pythoncom" else SimpleNamespace(client=SimpleNamespace())
        assert name not in (
            "spider_gd.app", "spider_gd.config.settings", "spider_gd.config.user_home",
            "spider_gd.gateway.api.server",
        ), "self-test imported app/HOME/service"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_port)
    code = backend_entry.main()
    captured = capsys.readouterr()
    if failure:
        assert code == 1
        assert failure in captured.err
        assert captured.out == "", "dependency failure must not print a successful identity"
    else:
        assert code == 0
        assert captured.err == ""
        assert json.loads(captured.out) == {
            "build_id": "012345abcdef" if frozen else "source",
            "channel": channel,
            "version": (
                ("0.2.8-beta.1" if channel == "beta" else "0.2.8") if frozen else __version__
            ),
        }
    expected = [] if platform != "win32" else [
        "pythoncom", "initialize", "pywintypes", "uiautomation", "win32com.client", "wmi",
    ]
    if failure:
        expected = expected[:expected.index(failure) + 1]
    if platform == "win32" and failure not in ("pythoncom", "initialize"):
        expected += ["uninitialize"]
    assert events == expected
    assert not home.exists()
    assert env_file.read_bytes() == original
    assert os.environ == env
