"""测试告警路由、静默时段与空实现兜底。"""

from __future__ import annotations

from datetime import datetime

import pytest

from spider_gd.gateway.alert import alert_gateway as ag_module
from spider_gd.gateway.alert.alert_gateway import AlertGateway, NullAlertGateway
from spider_gd.config.settings import AppSettings
from spider_gd.interfaces.alert import AlertLevel


def _config():
    return [
        {
            "type": "dingtalk_webhook",
            "url": "https://oapi.dingtalk.com/robot/send?access_token=t",
            "secret": "S",
            "events": ["error", "task_failed"],
        }
    ]


class _StateSettings:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get_all_settings(self) -> dict[str, str]:
        return self.values


@pytest.mark.unit
async def test_routes_to_channel_for_matching_event(mocker):
    sent = mocker.patch.object(ag_module, "send_dingtalk", return_value=True)
    gw = AlertGateway(channels=_config())
    result = await gw.send_alert(AlertLevel.ERROR, "Token expired", event_type="error")
    assert result.sent is True
    sent.assert_awaited_once()


@pytest.mark.unit
async def test_skips_channel_for_unmatched_event(mocker):
    sent = mocker.patch.object(ag_module, "send_dingtalk", return_value=True)
    gw = AlertGateway(channels=_config())
    result = await gw.send_alert(AlertLevel.INFO, "fyi", event_type="info")
    assert result.sent is False
    sent.assert_not_called()


@pytest.mark.unit
async def test_silence_window_suppresses(mocker):
    sent = mocker.patch.object(ag_module, "send_dingtalk", return_value=True)
    gw = AlertGateway(
        channels=_config(),
        silence_window="23:00-07:00",
        now_fn=lambda: datetime(2026, 1, 15, 23, 30),
    )
    result = await gw.send_alert(AlertLevel.ERROR, "x", event_type="error")
    assert result.suppressed is True
    sent.assert_not_called()


@pytest.mark.unit
def test_silence_window_overnight_boundaries():
    gw = AlertGateway(silence_window="23:00-07:00", now_fn=lambda: datetime(2026, 1, 15, 6, 0))
    assert gw.is_in_silence_window() is True
    gw2 = AlertGateway(silence_window="23:00-07:00", now_fn=lambda: datetime(2026, 1, 15, 12, 0))
    assert gw2.is_in_silence_window() is False


@pytest.mark.unit
async def test_null_gateway_never_raises():
    gw = NullAlertGateway()
    result = await gw.send_alert(AlertLevel.ERROR, "x", event_type="error")
    assert result.sent is False
    assert gw.is_in_silence_window() is False


@pytest.mark.unit
async def test_failed_alert_is_written_to_outbox_and_drained(mocker, tmp_path):
    sender = mocker.patch.object(ag_module, "send_dingtalk", side_effect=[False, True, True])
    outbox = tmp_path / "alert_outbox.jsonl"
    gw = AlertGateway(channels=_config(), outbox_path=outbox)

    failed = await gw.send_alert(AlertLevel.ERROR, "first", event_type="error")
    assert failed.sent is False
    assert outbox.exists()
    assert "first" in outbox.read_text(encoding="utf-8")

    recovered = await gw.send_alert(AlertLevel.ERROR, "second", event_type="error")
    assert recovered.sent is True
    assert not outbox.exists()
    assert sender.await_count == 3


@pytest.mark.unit
async def test_alert_outbox_can_be_retried_without_new_alert(mocker, tmp_path):
    sender = mocker.patch.object(ag_module, "send_dingtalk", side_effect=[False, True])
    outbox = tmp_path / "alert_outbox.jsonl"
    gw = AlertGateway(channels=_config(), outbox_path=outbox)

    failed = await gw.send_alert(AlertLevel.ERROR, "network down", event_type="error")
    assert failed.sent is False
    assert outbox.exists()

    sent = await gw.retry_outbox()

    assert sent == 1
    assert not outbox.exists()
    assert sender.await_count == 2


@pytest.mark.unit
async def test_runtime_alert_uses_delivery_wecom_over_legacy_alert_state(mocker, tmp_path):
    from spider_gd.app import build_alert_gateway

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=delivery"
    settings = AppSettings(storage={"data_dir": str(tmp_path)})
    gw = build_alert_gateway(
        settings,
        _StateSettings({
            "alertMode": "serverchan",
            "webhook": "SCT_legacy",
            "serverchanOpenid": "openid_a",
            "alertChannelsJson": (
                '[{"enabled":true,"type":"serverchan","credential":"SCT_table"}]'
            ),
            "deliveryEnabled": "true",
            "deliveryBackend": "wecom_webhook",
            "deliveryWecomWebhook": webhook,
        }),
    )
    serverchan = mocker.patch.object(ag_module, "send_serverchan", return_value=True)
    wecom = mocker.patch.object(ag_module, "send_wecom", return_value=True)

    result = await gw.send_alert(AlertLevel.ERROR, "任务失败", event_type="task_failed")

    assert result.sent is True
    wecom.assert_awaited_once()
    serverchan.assert_not_called()


@pytest.mark.unit
async def test_runtime_alert_uses_delivery_wecom_over_yaml_alert_channels(mocker, tmp_path):
    from spider_gd.app import build_alert_gateway

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=delivery"
    settings = AppSettings(storage={"data_dir": str(tmp_path)})
    settings.alert.channels = [
        {
            "type": "serverchan",
            "send_key": "SCT_yaml",
            "events": ["task_failed"],
        }
    ]
    gw = build_alert_gateway(
        settings,
        _StateSettings({
            "deliveryEnabled": "true",
            "deliveryBackend": "wecom_webhook",
            "deliveryWecomWebhook": webhook,
        }),
    )
    serverchan = mocker.patch.object(ag_module, "send_serverchan", return_value=True)
    wecom = mocker.patch.object(ag_module, "send_wecom", return_value=True)

    result = await gw.send_alert(AlertLevel.ERROR, "任务失败", event_type="task_failed")

    assert result.sent is True
    wecom.assert_awaited_once()
    serverchan.assert_not_called()


@pytest.mark.unit
async def test_runtime_alert_reuses_delivery_wecom_channel(mocker, tmp_path):
    from spider_gd.app import build_alert_gateway

    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=delivery"
    settings = AppSettings(storage={"data_dir": str(tmp_path)})
    gw = build_alert_gateway(
        settings,
        _StateSettings({
            "alertMode": "off",
            "webhook": "",
            "deliveryEnabled": "true",
            "deliveryBackend": "wecom_webhook",
            "deliveryWecomWebhook": webhook,
        }),
    )
    wecom = mocker.patch.object(ag_module, "send_wecom", return_value=True)

    result = await gw.send_alert(AlertLevel.ERROR, "定时任务失败", event_type="task_failed")

    assert result.sent is True
    wecom.assert_awaited_once()


@pytest.mark.unit
async def test_runtime_alert_reuses_delivery_desktop_wechat_channel(mocker, tmp_path):
    from spider_gd.app import build_alert_gateway

    settings = AppSettings(storage={"data_dir": str(tmp_path)})
    gw = build_alert_gateway(
        settings,
        _StateSettings({
            "deliveryEnabled": "true",
            "deliveryBackend": "desktop_wechat",
            "deliveryDesktopWechatTarget": "文件传输助手",
            "deliveryDesktopWechatBackend": "powershell",
            "deliveryDesktopWechatSelectMode": "current",
        }),
    )
    desktop = mocker.patch.object(AlertGateway, "_send_desktop_wechat", return_value=True)

    result = await gw.send_alert(AlertLevel.ERROR, "定时任务失败", event_type="task_failed")

    assert result.sent is True
    desktop.assert_awaited_once()
    channel = desktop.await_args.args[0]
    assert channel["target"] == "文件传输助手"
    assert channel["backend"] == "powershell"
