"""Agent 双向通信回传器。

提供两种回传通道（按现场可用性选用）：
- :func:`make_webhook_responder`：复用钉钉群机器人 Webhook 回传文本（无需企业应用，现成可用）。
- :class:`DingTalkStreamResponder`：钉钉 Stream 模式占位（需企业应用 AppKey/AppSecret +
  ``dingtalk-stream`` SDK，见 docs/08_远程控制外部依赖调研.md）；SDK/凭据就绪后在此接入。

回传器签名统一为 ``async (AgentResponse) -> bool``，可直接注入 :class:`AgentAdapter`。
"""

from __future__ import annotations

from loguru import logger

from spider_gd.gateway.alert.webhook_sender import send_dingtalk
from spider_gd.interfaces.agent import AgentResponse


def make_webhook_responder(webhook_url: str, secret: str = ""):
    """基于钉钉群机器人 Webhook 的回传器（双向通信的发送侧）。"""

    async def _responder(response: AgentResponse) -> bool:
        prefix = "✅" if response.success else "❌"
        text = f"{prefix} {response.message}"
        ok = await send_dingtalk(webhook_url, text, secret=secret)
        logger.info("Agent Webhook 回传：ok={}", ok)
        return ok

    return _responder


class DingTalkStreamResponder:
    """钉钉 Stream 模式回传（占位）。

    真正的 Stream 模式需要：企业内部应用 + ``dingtalk-stream`` SDK + 长连接接收回调。
    当前环境无凭据，故此类在 ``send`` 时降级为日志，待凭据/SDK 就绪后补全实现。
    """

    def __init__(self, app_key: str = "", app_secret: str = "") -> None:
        self._app_key = app_key
        self._app_secret = app_secret

    async def __call__(self, response: AgentResponse) -> bool:
        if not (self._app_key and self._app_secret):
            logger.info(
                "钉钉 Stream 未配置凭据，回传降级为日志：success={} {}",
                response.success, response.message,
            )
            return False
        # TODO(阶段3+): 凭据/SDK 就绪后用 dingtalk_stream 回传
        logger.warning("钉钉 Stream 回传尚未实现（凭据已配置），请补全 SDK 接入")
        return False
