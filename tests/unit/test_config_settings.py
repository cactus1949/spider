"""测试配置加载与 yaml inherits 递归合并。"""

from __future__ import annotations

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
