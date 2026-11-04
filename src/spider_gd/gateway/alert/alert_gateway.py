"""统一告警网关：按事件路由到通道 + 静默时段抑制。

实现 :class:`spider_gd.interfaces.alert.IAlertGateway`。
:class:`NullAlertGateway` 为离线/无外网时的空实现兜底。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time
import json
from pathlib import Path
from typing import Any

from loguru import logger

from spider_gd.gateway.alert.webhook_sender import (
    send_dingtalk,
    send_pushplus,
    send_serverchan,
    send_wechat_template,
    send_wecom,
)
from spider_gd.interfaces.alert import AlertLevel, AlertResult


def _parse_window(window: str) -> tuple[time, time] | None:
    if not window or "-" not in window:
        return None
    start_s, end_s = window.split("-", 1)
    return (time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip()))


class AlertGateway:
    def __init__(
        self,
        channels: list[dict[str, Any]] | None = None,
        silence_window: str = "",
        *,
        now_fn: Callable[[], datetime] | None = None,
        outbox_path: str | Path | None = None,
    ) -> None:
        self._channels = channels or []
        self._window = _parse_window(silence_window)
        self._now = now_fn or datetime.now
        self._outbox_path = Path(outbox_path) if outbox_path else None

    def is_in_silence_window(self) -> bool:
        if self._window is None:
            return False
        start, end = self._window
        now = self._now().time()
        if start <= end:
            return start <= now <= end
        # 跨午夜，如 23:00-07:00
        return now >= start or now <= end

    def _channels_for(self, event_type: str) -> list[dict[str, Any]]:
        result = []
        for ch in self._channels:
            events = ch.get("events")
            if not events or event_type in events:
                result.append(ch)
        return result

    async def send_alert(
        self, level: AlertLevel, message: str, event_type: str = "info"
    ) -> AlertResult:
        if self.is_in_silence_window():
            logger.info("告警在静默时段被抑制：{}", message)
            return AlertResult(sent=False, suppressed=True)

        targets = self._channels_for(event_type)
        if not targets:
            return AlertResult(sent=False, error_msg="无匹配通道")

        await self._drain_outbox()
        sent_any, last_err = await self._dispatch(targets, level, message)
        if not sent_any:
            self._enqueue(level, message, event_type, last_err or "发送失败")
        return AlertResult(sent=sent_any, error_msg=None if sent_any else last_err)

    async def retry_outbox(self) -> int:
        """主动补发本地告警 outbox，供调度器在网络恢复后定时调用。"""
        if self.is_in_silence_window():
            logger.info("告警 outbox 补发在静默时段被抑制")
            return 0
        return await self._drain_outbox()

    async def _dispatch(
        self, targets: list[dict[str, Any]], level: AlertLevel, message: str
    ) -> tuple[bool, str | None]:
        text = f"**[{level.value.upper()}]** {message}"
        sent_any = False
        last_err: str | None = None
        for ch in targets:
            ch_type = ch.get("type", "")
            channel_err: str | None = None
            try:
                if ch_type == "dingtalk_webhook":
                    ok = await send_dingtalk(ch["url"], text, secret=ch.get("secret", ""))
                elif ch_type == "wecom_webhook":
                    ok = await send_wecom(ch["url"], text)
                elif ch_type == "serverchan":
                    ok = await send_serverchan(
                        ch["send_key"], text,
                        title=f"spider-gd [{level.value.upper()}]",
                        channel=ch.get("channel", ""),
                        openid=ch.get("openid", ""),
                    )
                elif ch_type == "pushplus":
                    ok = await send_pushplus(
                        ch["token"], text,
                        title=f"spider-gd [{level.value.upper()}]",
                        channel=ch.get("channel", ""),
                    )
                elif ch_type == "desktop_wechat":
                    ok = await self._send_desktop_wechat(ch, text)
                elif ch_type == "wechat_template":
                    data = ch.get("data", {}) or {}
                    if not data:
                        data = {
                            "title": f"spider-gd [{level.value.upper()}]",
                            "content": message,
                        }
                    ok = await send_wechat_template(
                        ch["appid"], ch["appsecret"], ch["template_id"],
                        ch.get("openids", []),
                        data=data,
                    )
                else:
                    logger.warning("未知告警通道类型: {}", ch_type)
                    continue
            except Exception as exc:
                ok = False
                channel_err = f"{ch_type} 异常: {exc}"
            sent_any = sent_any or ok
            if not ok:
                last_err = channel_err or f"{ch_type} 发送失败"
        return sent_any, last_err

    async def _send_desktop_wechat(self, channel: dict[str, Any], text: str) -> bool:
        from spider_gd.gateway.delivery.desktop_wechat import (
            DesktopWechatOptions,
            DesktopWechatSender,
        )

        def _float_value(value: Any, default: float) -> float:
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return default

        def _int_value(value: Any, default: int) -> int:
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                return default

        def _bool_value(value: Any, default: bool = False) -> bool:
            if isinstance(value, bool):
                return value
            normalized = str(value or "").strip().lower()
            if normalized in {"1", "true", "yes", "on", "y"}:
                return True
            if normalized in {"0", "false", "no", "off", "n"}:
                return False
            return default

        options = DesktopWechatOptions(
            target=str(channel.get("target") or "").strip(),
            message=text,
            app_name=str(channel.get("app_name") or "").strip(),
            pause_sec=max(0.0, _float_value(channel.get("pause_sec"), 0.7)),
            backend=str(channel.get("backend") or "powershell").strip().lower().replace("-", "_"),
            select_mode=(
                str(channel.get("select_mode") or "current").strip().lower().replace("_", "-")
            ),
            profile_path=str(channel.get("profile_path") or "./data/wechat_targets.json").strip(),
            hash_threshold=max(0, _int_value(channel.get("hash_threshold"), 12)),
            allow_unstable_search=_bool_value(channel.get("allow_unstable_search")),
            dry_run=False,
        )
        result = await asyncio.to_thread(DesktopWechatSender().send, options)
        if not result.ok:
            logger.warning("本机微信 PoC 告警发送失败: {}", result.error_msg)
            return False
        return True

    def _enqueue(self, level: AlertLevel, message: str, event_type: str, error: str) -> None:
        if self._outbox_path is None:
            return
        try:
            self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "level": level.value,
                "message": message,
                "event_type": event_type,
                "error": error,
                "created_at": self._now().isoformat(timespec="seconds"),
            }
            with self._outbox_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.warning("告警发送失败，已写入本地 outbox: {}", self._outbox_path)
        except Exception as exc:
            logger.warning("告警 outbox 写入失败: {}", exc)

    async def _drain_outbox(self) -> int:
        if self._outbox_path is None or not self._outbox_path.exists():
            return 0
        try:
            records = [
                json.loads(line)
                for line in self._outbox_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except Exception as exc:
            logger.warning("告警 outbox 读取失败: {}", exc)
            return 0

        remaining: list[dict[str, Any]] = []
        sent_count = 0
        for record in records[:50]:
            targets = self._channels_for(str(record.get("event_type") or "info"))
            if not targets:
                remaining.append(record)
                continue
            try:
                level = AlertLevel(record.get("level", "error"))
            except ValueError:
                level = AlertLevel.ERROR
            sent, _err = await self._dispatch(targets, level, record.get("message", ""))
            if not sent:
                remaining.append(record)
            else:
                sent_count += 1
        remaining.extend(records[50:])

        try:
            if remaining:
                self._outbox_path.write_text(
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in remaining) + "\n",
                    encoding="utf-8",
                )
            else:
                self._outbox_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("告警 outbox 更新失败: {}", exc)
        return sent_count


class NullAlertGateway:
    """空实现：离线/无外网时使用，仅记录日志不报错。"""

    async def send_alert(
        self, level: AlertLevel, message: str, event_type: str = "info"
    ) -> AlertResult:
        logger.debug("[NullAlert] {} {} ({})", level.value, message, event_type)
        return AlertResult(sent=False, suppressed=True)

    def is_in_silence_window(self) -> bool:
        return False

    async def retry_outbox(self) -> int:
        return 0
