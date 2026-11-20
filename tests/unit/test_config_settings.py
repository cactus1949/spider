"""测试配置加载与 yaml inherits 递归合并。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from spider_gd.config.deploy_mode import DeployMode
from spider_gd.config.settings import _deep_merge, _load_yaml, load_config


@pytest.mark.unit
def test_deep_merge_nested_override():
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    override = {"nested": {"y": 20, "z": 30}}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "nested": {"x": 1, "y": 20, "z": 30}}


@pytest.mark.unit
def test_load_yaml_resolves_inherits(tmp_path: Path):
    (tmp_path / "common.yaml").write_text(
        "platform:\n  domain: base.example\nlog:\n  console_level: INFO\n",
        encoding="utf-8",
    )
    (tmp_path / "child.yaml").write_text(
        "inherits: common.yaml\nlog:\n  console_level: DEBUG\n",
        encoding="utf-8",
    )
    merged = _load_yaml(tmp_path / "child.yaml")
    assert merged["platform"]["domain"] == "base.example"
    assert merged["log"]["console_level"] == "DEBUG"
    assert "inherits" not in merged


@pytest.mark.unit
def test_load_config_offline_mode():
    settings = load_config("configs/mode_d_offline.yaml")
    assert settings.deploy_mode == DeployMode.OFFLINE
    assert settings.platform.domain == "pm.gd.csg.cn"
    assert settings.modules.alert_gateway is False
    assert settings.modules.crawler is True


@pytest.mark.unit
def test_load_config_online_mode_enables_alert():
    settings = load_config("configs/mode_a_wired.yaml")
    assert settings.deploy_mode == DeployMode.STANDALONE_WIRED
    assert settings.modules.alert_gateway is True


@pytest.mark.unit
@pytest.mark.parametrize(("channel", "default_port"), [("stable", 8800), ("beta", 8801)])
@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize(
    ("override", "expected_port"), [(None, None), ("yaml", 9123), ("env", 9234)],
)
def test_load_config_web_port_uses_channel_only_as_default(
    tmp_path: Path, monkeypatch, channel, default_port, frozen, override, expected_port
):
    from spider_gd import build_stamp

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPIDER_GD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WEB__PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(build_stamp, "RELEASE_CHANNEL", channel)
    monkeypatch.setattr(
        build_stamp, "RELEASE_VERSION", "0.2.8-beta.7" if channel == "beta" else "0.2.8",
    )
    opposite = "stable" if channel == "beta" else "beta"
    monkeypatch.setenv("SPIDER_GD_CHANNEL", opposite if frozen else channel)
    config = tmp_path / "mode.yaml"
    config.write_text(
        "web:\n  port: 9123\n" if override == "yaml" else "web:\n  host: 127.0.0.1\n",
        encoding="utf-8",
    )
    if override == "env":
        monkeypatch.setenv("WEB__PORT", "9234")
    assert load_config(config).web.port == (expected_port or default_port)


@pytest.mark.unit
def test_load_config_delivery_settings(tmp_path: Path):
    config = tmp_path / "delivery.yaml"
    config.write_text(
        """
delivery:
  enabled: true
  env_file: /c/secure/spider-gd-delivery.env
  backend: wecom_webhook
  wecom_webhook: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc
  serverchan_send_key: SCT123
  serverchan_channel: "9"
  serverchan_openid: "openid1"
  pushplus_token: pptok
  pushplus_channel: wechat
  desktop_wechat_target: 文件传输助手
  desktop_wechat_backend: powershell
  desktop_wechat_select_mode: current
  desktop_wechat_profile_path: ./data/wechat_targets.json
  desktop_wechat_app_name: WeChat
  desktop_wechat_pause_sec: 0.8
  desktop_wechat_hash_threshold: 10
  desktop_wechat_allow_unstable_search: true
  send_message: true
  send_file: true
  file_delivery: remote_link
  wecom_file_max_mb: 20
  remote_upload_url: https://files.example.com
  remote_upload_token: tok
  remote_upload_relative_path: /upload/publicData
  remote_upload_verify_url: false
  retry_attempts: 3
  retry_delay_sec: 1.5
  title: 采集结果
""",
        encoding="utf-8",
    )

    settings = load_config(config)

    assert settings.delivery.enabled is True
    assert settings.delivery.env_file == "/c/secure/spider-gd-delivery.env"
    assert settings.delivery.backend == "wecom_webhook"
    assert settings.delivery.wecom_webhook.endswith("key=abc")
    assert settings.delivery.serverchan_send_key == "SCT123"
    assert settings.delivery.serverchan_channel == "9"
    assert settings.delivery.serverchan_openid == "openid1"
    assert settings.delivery.pushplus_token == "pptok"
    assert settings.delivery.pushplus_channel == "wechat"
    assert settings.delivery.desktop_wechat_target == "文件传输助手"
    assert settings.delivery.desktop_wechat_backend == "powershell"
    assert settings.delivery.desktop_wechat_select_mode == "current"
    assert settings.delivery.desktop_wechat_profile_path == "./data/wechat_targets.json"
    assert settings.delivery.desktop_wechat_app_name == "WeChat"
    assert settings.delivery.desktop_wechat_pause_sec == 0.8
    assert settings.delivery.desktop_wechat_hash_threshold == 10
    assert settings.delivery.desktop_wechat_allow_unstable_search is True
    assert settings.delivery.file_delivery == "remote_link"
    assert settings.delivery.remote_upload_url == "https://files.example.com"
    assert settings.delivery.remote_upload_token == "tok"
    assert settings.delivery.remote_upload_verify_url is False
    assert settings.delivery.retry_attempts == 3
    assert settings.delivery.retry_delay_sec == 1.5
    assert settings.delivery.title == "采集结果"


@pytest.fixture
def isolated_env_config(tmp_path: Path, monkeypatch):
    from spider_gd import build_stamp

    install = tmp_path / "install"
    install.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    config = install / "mode.yaml"
    config.write_text("{}\n", encoding="utf-8")
    monkeypatch.chdir(install)
    monkeypatch.setattr(os, "environ", {
        "SPIDER_GD_HOME": str(home),
        "NO_PROXY": "*",
        "no_proxy": "*",
    })

    def identity(channel: str, frozen: bool):
        monkeypatch.setattr(sys, "frozen", frozen, raising=False)
        monkeypatch.setattr(build_stamp, "RELEASE_CHANNEL", channel)
        monkeypatch.setattr(
            build_stamp, "RELEASE_VERSION", "0.2.8-beta.7" if channel == "beta" else "0.2.8",
        )
        opposite = "stable" if channel == "beta" else "beta"
        monkeypatch.setenv("SPIDER_GD_CHANNEL", opposite if frozen else channel)

    return install, home, config, identity


@pytest.mark.unit
@pytest.mark.parametrize("encoding", ["gbk", "utf-8", "utf-8-sig"])
@pytest.mark.parametrize("linked", [False, True], ids=["file", "foreign-symlink"])
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_load_config_beta_env_is_read_only(
    tmp_path: Path, isolated_env_config, encoding: str, linked: bool, frozen: bool,
):
    install, home, config, identity = isolated_env_config
    identity("beta", frozen)
    env_path = install / ".env"
    source = tmp_path / "foreign.env" if linked else env_path
    original = "CRAWLER__PASSWORD=测试密码\n".encode(encoding)
    source.write_bytes(original)
    if linked:
        env_path.symlink_to(source)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    settings = load_config(config)

    assert settings.crawler.password == "测试密码"
    assert settings.storage.sqlite_path == str(home / "data" / "spider.db")
    assert source.read_bytes() == original
    assert env_path.is_symlink() == linked
    if linked:
        assert env_path.readlink() == source
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert "CRAWLER__PASSWORD" not in os.environ


@pytest.mark.unit
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_load_config_stable_keeps_default_env_encoding(isolated_env_config, frozen: bool):
    install, home, config, identity = isolated_env_config
    identity("stable", frozen)
    # HOME 与安装目录相同，避免旧版迁移行为预先灌入凭据。
    os.environ["SPIDER_GD_HOME"] = str(install)
    env_path = install / ".env"
    original = "CRAWLER__PASSWORD=测试密码\n".encode("gbk")
    env_path.write_bytes(original)

    with pytest.raises(UnicodeDecodeError):
        load_config(config)

    assert env_path.read_bytes() == original
    assert list(home.iterdir()) == []


@pytest.mark.unit
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_load_config_beta_without_env_does_not_create_it(isolated_env_config, frozen: bool):
    install, home, config, identity = isolated_env_config
    identity("beta", frozen)

    settings = load_config(config)

    assert settings.crawler.password == ""
    assert not (install / ".env").exists()
    assert list(home.iterdir()) == []
