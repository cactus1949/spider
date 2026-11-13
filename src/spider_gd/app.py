"""依赖注入装配入口。

业务模块永远不写 ``if mode == ...``；这里按 ``settings.modules.*`` 决定装配：
- 离线核心（存储/落地/调度）始终装配。
- 告警关闭时用 :class:`NullAlertGateway` 兜底。
- Agent 关闭时不加载，不影响主流程。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger

from spider_gd.config.logging import setup_logging
from spider_gd.config.settings import AppSettings, DeliverySettings, load_endpoints
from spider_gd.core.pipeline.local_landing import LocalLanding
from spider_gd.core.scheduler.task_scheduler import TaskScheduler
from spider_gd.gateway.alert.alert_gateway import AlertGateway, NullAlertGateway
from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.interfaces.alert import AlertLevel, IAlertGateway
from spider_gd.interfaces.crawler import EndpointConfig
from spider_gd.interfaces.storage import ExecutionLogRecord, TaskItemRecord, TaskRecord

_DELIVERY_RETRY_STALE_SEC = 600
_WECOM_ROBOT_FILE_MAX_BYTES = 20 * 1024 * 1024


@dataclass
class AppContext:
    settings: AppSettings
    state_store: TaskStateStore
    landing: LocalLanding
    scheduler: TaskScheduler
    alert: IAlertGateway
    agent: object | None
    endpoints: list[EndpointConfig]
    control: object | None = None
    delivery_overrides: dict[str, str] | None = None
    _delivery_retry_lock: asyncio.Lock | None = None
    _delivery_retry_lock_loop: asyncio.AbstractEventLoop | None = None


class _RunnerPage:
    """把 :class:`PlaywrightRunner` 适配成 BrowserSessionProvider 期望的 page 接口。

    runner.page 在 ``open()`` 后才创建，这里懒读取，使会话提供者构造期不依赖已启动的浏览器。
    """

    def __init__(self, runner: object) -> None:
        self._runner = runner

    @property
    def url(self) -> str:
        return self._runner.page.url  # type: ignore[attr-defined]

    async def evaluate(self, expr: str) -> object:
        return await self._runner.page.evaluate(expr)  # type: ignore[attr-defined]

    @property
    def context(self) -> object:
        return self._runner.page.context  # type: ignore[attr-defined]


def build_alert_gateway(settings: AppSettings, state_store: TaskStateStore) -> IAlertGateway:
    try:
        app_settings = state_store.get_all_settings()
    except Exception:
        app_settings = {}

    # 告警与“结果发送”同源；旧 alert.* / alertChannelsJson 不再参与运行时路由。
    channels: list[dict[str, object]] = []

    def add_channel(channel: dict[str, object]) -> None:
        if channel not in channels:
            channels.append(channel)

    for channel in _delivery_alert_channels(settings, app_settings):
        add_channel(channel)

    return AlertGateway(
        channels,
        settings.alert.silence_window,
        outbox_path=Path(settings.storage.data_dir) / "alert_outbox.jsonl",
    )


def _delivery_alert_channels(
    settings: AppSettings,
    app_settings: dict[str, str],
) -> list[dict[str, object]]:
    """把“结果发送”配置映射为异常通知通道。"""
    env_overrides: dict[str, object] = {}
    env_file = (app_settings.get("deliveryEnvFile") or settings.delivery.env_file or "").strip()
    if not env_file:
        import os

        env_file = os.environ.get("SPIDER_GD_DELIVERY_ENV_FILE", "").strip()
    if env_file:
        try:
            from spider_gd.config.delivery_env import read_delivery_env_file_overrides

            env_overrides = read_delivery_env_file_overrides(env_file)
        except Exception as exc:
            logger.warning("读取结果发送 env 文件失败，告警通道跳过 env 覆盖: {}", exc)

    def text(key: str, default: str) -> str:
        if key in app_settings:
            value = str(app_settings.get(key) or "").strip()
            if value or key not in env_overrides:
                return value
        if key in env_overrides:
            return str(env_overrides[key] or "").strip()
        return default.strip()

    def flag(key: str, default: bool) -> bool:
        if key in app_settings:
            value = str(app_settings.get(key) or "").strip().lower()
            if value in {"1", "true", "yes", "on", "y"}:
                return True
            if value in {"0", "false", "no", "off", "n"}:
                return False
        if key in env_overrides:
            value = str(env_overrides[key] or "").strip().lower()
            if value in {"1", "true", "yes", "on", "y"}:
                return True
            if value in {"0", "false", "no", "off", "n"}:
                return False
        return default

    def number(key: str, default: int) -> int:
        if key in app_settings:
            return _int_setting(str(app_settings.get(key) or ""), default)
        if key in env_overrides:
            return _int_setting(str(env_overrides[key] or ""), default)
        return default

    def decimal(key: str, default: float) -> float:
        if key in app_settings:
            return _float_setting(str(app_settings.get(key) or ""), default)
        if key in env_overrides:
            return _float_setting(str(env_overrides[key] or ""), default)
        return default

    if not flag("deliveryEnabled", settings.delivery.enabled):
        return []

    backend = text("deliveryBackend", settings.delivery.backend).lower().replace("-", "_")
    if backend == "wecom_webhook":
        webhook = text("deliveryWecomWebhook", settings.delivery.wecom_webhook)
        return [{"type": "wecom_webhook", "url": webhook}] if webhook else []
    if backend == "serverchan":
        send_key = text("deliveryServerchanSendKey", settings.delivery.serverchan_send_key)
        if not send_key:
            return []
        return [{
            "type": "serverchan",
            "send_key": send_key,
            "channel": text("deliveryServerchanChannel", settings.delivery.serverchan_channel),
            "openid": text("deliveryServerchanOpenid", settings.delivery.serverchan_openid),
        }]
    if backend == "pushplus":
        token = text("deliveryPushplusToken", settings.delivery.pushplus_token)
        if not token:
            return []
        return [{
            "type": "pushplus",
            "token": token,
            "channel": text("deliveryPushplusChannel", settings.delivery.pushplus_channel),
        }]
    if backend == "desktop_wechat":
        target = text("deliveryDesktopWechatTarget", settings.delivery.desktop_wechat_target)
        if not target:
            return []
        return [{
            "type": "desktop_wechat",
            "target": target,
            "backend": text(
                "deliveryDesktopWechatBackend",
                settings.delivery.desktop_wechat_backend,
            ),
            "select_mode": text(
                "deliveryDesktopWechatSelectMode",
                settings.delivery.desktop_wechat_select_mode,
            ),
            "profile_path": text(
                "deliveryDesktopWechatProfilePath",
                settings.delivery.desktop_wechat_profile_path,
            ),
            "app_name": text(
                "deliveryDesktopWechatAppName",
                settings.delivery.desktop_wechat_app_name,
            ),
            "pause_sec": decimal(
                "deliveryDesktopWechatPauseSec",
                settings.delivery.desktop_wechat_pause_sec,
            ),
            "hash_threshold": number(
                "deliveryDesktopWechatHashThreshold",
                settings.delivery.desktop_wechat_hash_threshold,
            ),
            "allow_unstable_search": flag(
                "deliveryDesktopWechatAllowUnstableSearch",
                settings.delivery.desktop_wechat_allow_unstable_search,
            ),
        }]
    return []


def ensure_dual_nic_ready(ctx: AppContext) -> tuple[bool, str, list[str]]:
    """有路由规则时，运行一次动态双网卡闭环预检。"""
    wifi_ok, wifi_message, wifi_details = prepare_wifi_connection(ctx)
    if not wifi_ok:
        return False, wifi_message, wifi_details

    rules = ctx.state_store.list_route_rules()
    enabled = [
        r for r in rules
        if r.get("enabled", True)
        and str(r.get("domain_pattern", "")).strip()
        and str(r.get("interface_name", "")).strip()
    ]
    if not enabled:
        if wifi_details:
            return True, f"{wifi_message}；未配置专网路由规则，跳过双网卡预检", wifi_details
        return True, "未配置专网路由规则，跳过双网卡预检", []

    from spider_gd.infrastructure.network import DualNicClosure

    cache_dir = Path(ctx.settings.storage.data_dir) / "network" / "dns_cache"
    report = DualNicClosure(cache_dir=cache_dir).apply_rules(enabled)
    details = [*wifi_details, *report.details]
    message = f"{wifi_message}；{report.message}" if wifi_details else report.message
    return report.status in {"ok", "degraded"}, message, details


def network_preflight_label(message: str, details: list[str] | None = None) -> str:
    """Choose the user-facing label for the combined network preflight."""
    text = f"{message}\n" + "\n".join(details or [])
    has_wifi = "WiFi" in text or "wifi" in text.lower()
    has_dual_nic = any(token in text for token in ("双网卡", "路由", "专网", "公网"))
    if has_dual_nic and not has_wifi:
        return "双网卡预检"
    if has_dual_nic:
        return "网络准备/双网卡预检"
    return "网络准备"


def _read_state_setting(ctx: AppContext, key: str) -> str | None:
    state_store = getattr(ctx, "state_store", None)
    getter = getattr(state_store, "get_setting", None)
    if not callable(getter):
        return None
    try:
        value = getter(key)
    except Exception:
        logger.warning("读取应用设置失败: {}", key)
        return None
    if value is None:
        return None
    return str(value)


def _bool_setting(value: str, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "y"}:
        return True
    if normalized in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _int_setting(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return default


def _float_setting(value: str, default: float) -> float:
    try:
        return float(value.strip())
    except ValueError:
        return default


def prepare_wifi_connection(ctx: AppContext) -> tuple[bool, str, list[str]]:
    enabled = _bool_setting(_read_state_setting(ctx, "wifiAutoConnectEnabled") or "", False)
    if not enabled:
        return True, "未启用自动连接 WiFi", []

    ssid = (_read_state_setting(ctx, "wifiSsid") or "").strip()
    password = _read_state_setting(ctx, "wifiPassword") or ""
    interface_name = (_read_state_setting(ctx, "wifiInterfaceName") or "").strip()
    timeout_sec = max(
        1,
        _int_setting(_read_state_setting(ctx, "wifiConnectTimeoutSec") or "", 30),
    )
    fail_on_error = _bool_setting(
        _read_state_setting(ctx, "wifiFailTaskOnError") or "",
        True,
    )

    from spider_gd.infrastructure.network import WifiConnectOptions, WifiConnector

    result = WifiConnector().prepare(
        WifiConnectOptions(
            ssid=ssid,
            password=password,
            interface_name=interface_name,
            timeout_sec=timeout_sec,
        )
    )
    details = list(result.details)
    if result.current_ssid:
        details.append(f"当前 WiFi: {result.current_ssid}")
    if result.ok:
        return True, result.message or "WiFi 准备完成", details

    message = result.message or result.error or "WiFi 准备失败"
    details.append(f"ERR WiFi: {message}")
    if fail_on_error:
        return False, message, details
    return True, f"{message}；设置为允许任务继续", details


def _delivery_zip_identity(zip_path: str | Path) -> str:
    try:
        path = Path(zip_path).expanduser().resolve()
        stat = path.stat()
    except OSError:
        return ""
    return f"{path}|{stat.st_size}|{stat.st_mtime_ns}"


def _file_sha256(path: str | Path) -> str:
    try:
        file_path = Path(path).expanduser().resolve()
        digest = hashlib.sha256()
        with file_path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def create_delivery_sample_zip(base_dir: str | Path) -> Path:
    root = Path(base_dir).expanduser().resolve() / "_stable_delivery" / "samples"
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    zip_path = root / f"delivery_sample_{ts}.zip"
    payload = (
        "spider-gd stable delivery sample\n"
        f"created_at={datetime.now().isoformat(timespec='seconds')}\n"
    )
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", payload)
    return zip_path


def _delivery_identity(ctx: AppContext, settings: DeliverySettings | None = None) -> str:
    """Hash the effective destination/mode so outbox idempotency follows recipients."""
    settings = settings or _effective_delivery_settings(ctx)
    backend = settings.backend.strip().lower().replace("-", "_")
    upload_url = settings.remote_upload_url.strip() or (
        _read_state_setting(ctx, "workshopUploadUrl") or ""
    ).strip()
    parts = [
        f"backend={backend}",
        f"send_message={int(settings.send_message)}",
        f"send_file={int(settings.send_file)}",
        f"file_delivery={settings.file_delivery.strip().lower().replace('-', '_')}",
        f"wecom_file_max_mb={max(1, settings.wecom_file_max_mb)}",
        f"remote_upload_url={upload_url}",
        f"remote_upload_relative_path={settings.remote_upload_relative_path.strip()}",
    ]
    if backend == "wecom_webhook":
        parts.append(f"wecom_webhook={settings.wecom_webhook.strip()}")
    elif backend == "serverchan":
        parts.extend([
            f"serverchan_send_key={settings.serverchan_send_key.strip()}",
            f"serverchan_channel={settings.serverchan_channel.strip()}",
            f"serverchan_openid={settings.serverchan_openid.strip()}",
        ])
    elif backend == "pushplus":
        parts.extend([
            f"pushplus_token={settings.pushplus_token.strip()}",
            f"pushplus_channel={settings.pushplus_channel.strip()}",
        ])
    elif backend == "desktop_wechat":
        parts.extend([
            f"desktop_wechat_target={settings.desktop_wechat_target.strip()}",
            f"desktop_wechat_backend={settings.desktop_wechat_backend.strip()}",
            f"desktop_wechat_select_mode={settings.desktop_wechat_select_mode.strip()}",
            f"desktop_wechat_profile_path={settings.desktop_wechat_profile_path.strip()}",
            f"desktop_wechat_app_name={settings.desktop_wechat_app_name.strip()}",
            f"desktop_wechat_hash_threshold={max(0, settings.desktop_wechat_hash_threshold)}",
            f"desktop_wechat_allow_unstable_search={int(settings.desktop_wechat_allow_unstable_search)}",
        ])
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _delivery_success_note(ctx: AppContext, item: dict | None = None) -> str:
    settings = _effective_delivery_settings(ctx)
    backend = settings.backend.strip().lower().replace("-", "_")
    channel = {
        "wecom_webhook": "企业微信群机器人",
        "serverchan": "Server酱个人微信",
        "pushplus": "PushPlus个人微信",
        "desktop_wechat": "本机微信 PoC",
    }.get(backend, settings.backend)
    remote_url = ""
    file_sent = False
    if isinstance(item, dict):
        remote_url = str(item.get("remote_url") or "").strip()
        file_sent = bool(int(item.get("file_sent") or 0))
    parts = [channel]
    if remote_url:
        parts.append("远程下载链接")
    elif file_sent:
        parts.append("企业微信文件直发")
    elif settings.send_file:
        parts.append("未单独发送文件")
    if settings.send_message or remote_url:
        parts.append("完成消息")
    return "；".join(parts)


def delivery_forwarding_summary(receipt: dict[str, object]) -> dict[str, object]:
    """Summarize what an operator can forward to the final recipient."""
    status = str(receipt.get("status") or "")
    remote_url = str(receipt.get("remote_url") or "").strip()
    file_sent = bool(receipt.get("file_sent"))
    delivery_note = str(receipt.get("delivery_note") or "")
    ready = status == "sent" and bool(remote_url or file_sent or "完成消息" in delivery_note)
    if remote_url:
        instruction = "转发 remote_url 下载链接，并可用 zip_sha256 核对文件"
    elif file_sent:
        instruction = "已收到附件文件，可在微信/企微内转发该文件"
    elif ready:
        instruction = "已发送完成消息；本次没有可转发下载链接或附件"
    else:
        instruction = "尚未送达，先检查 outbox.status/last_error 并补发"
    return {
        "ready": ready,
        "method": delivery_note,
        "can_forward_link": bool(remote_url),
        "remote_url": remote_url,
        "file_sent": file_sent,
        "zip_size": int(receipt.get("zip_size") or 0),
        "zip_sha256": str(receipt.get("zip_sha256") or ""),
        "instruction": instruction,
    }


def delivery_outbox_receipt(item: dict | None) -> dict[str, object]:
    if not isinstance(item, dict):
        return {}
    receipt: dict[str, object] = {
        "task_id": str(item.get("task_id") or ""),
        "status": str(item.get("status") or ""),
        "attempts": int(item.get("attempts") or 0),
        "zip_path": str(item.get("zip_path") or ""),
        "zip_size": int(item.get("zip_size") or 0),
        "zip_sha256": str(item.get("zip_sha256") or ""),
        "remote_url": str(item.get("remote_url") or ""),
        "file_sent": bool(int(item.get("file_sent") or 0)),
        "delivery_note": str(item.get("delivery_note") or ""),
        "last_error": str(item.get("last_error") or ""),
        "delivered_at": str(item.get("delivered_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
    }
    receipt["forwarding"] = delivery_forwarding_summary(receipt)
    return receipt


def write_delivery_receipt(
    base_dir: str | Path,
    receipt: dict[str, object],
    *,
    command: str = "",
    output_path: str | Path | None = None,
    extra: dict[str, object] | None = None,
) -> Path:
    """Write a stable JSON receipt for onsite delivery acceptance and troubleshooting."""
    task_id = str(receipt.get("task_id") or "UNKNOWN")
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._-") or "UNKNOWN"
    if output_path is None:
        path = (
            Path(base_dir).expanduser().resolve()
            / "_stable_delivery"
            / "receipts"
            / f"{safe_task_id}.json"
        )
    else:
        path = Path(output_path).expanduser().resolve()
    payload: dict[str, object] = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "command": command,
        "outbox": receipt,
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_delivery_receipt_for_task(
    ctx: AppContext,
    task_id: str,
    *,
    command: str,
    extra: dict[str, object] | None = None,
) -> str:
    reader = getattr(getattr(ctx, "state_store", None), "get_delivery_outbox", None)
    if not callable(reader):
        return ""
    receipt = delivery_outbox_receipt(reader(task_id))
    if not receipt:
        return ""
    try:
        path = write_delivery_receipt(
            ctx.settings.storage.data_dir,
            receipt,
            command=command,
            extra=extra,
        )
    except Exception as exc:
        logger.warning("写入稳定发送回执失败：task={} error={}", task_id, exc)
        return ""
    return str(path)


def _delivery_receipt_path(base_dir: str | Path, task_id: str) -> Path:
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._-") or "UNKNOWN"
    return Path(base_dir).expanduser().resolve() / "_stable_delivery" / "receipts" / f"{safe_task_id}.json"


def delivery_execution_log_lines(
    ctx: AppContext,
    task_id: str,
    *,
    zip_path: str = "",
) -> list[str]:
    """Build concise execution-log lines showing delivery evidence for operators."""
    lines: list[str] = []
    reader = getattr(getattr(ctx, "state_store", None), "get_delivery_outbox", None)
    receipt = delivery_outbox_receipt(reader(task_id)) if callable(reader) else {}
    final_zip = zip_path or str(receipt.get("zip_path") or "")
    if final_zip:
        lines.append(f"📦 稳定发送压缩包: {final_zip}")
    if not receipt:
        return lines

    status = str(receipt.get("status") or "")
    note = str(receipt.get("delivery_note") or "").strip()
    error = str(receipt.get("last_error") or "").strip()
    status_line = f"📨 稳定发送状态: {status}"
    if note:
        status_line += f"；{note}"
    elif error:
        status_line += f"；{error}"
    lines.append(status_line)

    forwarding = receipt.get("forwarding")
    if isinstance(forwarding, dict):
        instruction = str(forwarding.get("instruction") or "").strip()
        if instruction:
            lines.append(f"🔁 可转发: {instruction}")
        remote_url = str(forwarding.get("remote_url") or "").strip()
        if remote_url:
            lines.append(f"🔗 下载链接: {remote_url}")

    receipt_path = _delivery_receipt_path(ctx.settings.storage.data_dir, task_id)
    if receipt_path.is_file():
        lines.append(f"🧾 稳定发送回执: {receipt_path}")
    return lines


def _read_delivery_receipt_file(path: str | Path) -> dict[str, object]:
    receipt_path = Path(path).expanduser().resolve()
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        outbox = data.get("outbox")
        if isinstance(outbox, dict):
            return dict(outbox)
        return dict(data)
    return {}


async def verify_delivery_receipt(
    ctx: AppContext,
    *,
    task_id: str = "",
    receipt_path: str | Path | None = None,
    verify_remote: bool = True,
    download_remote: bool = False,
    max_remote_bytes: int = 200 * 1024 * 1024,
) -> dict[str, object]:
    """Verify delivery evidence: outbox/receipt status, local zip hash, and remote URL."""
    from spider_gd.gateway.workshop_uploader import verify_remote_file_url

    checks: list[dict[str, object]] = []
    errors: list[str] = []
    source = ""
    receipt: dict[str, object] = {}

    if receipt_path:
        source = str(Path(receipt_path).expanduser().resolve())
        try:
            receipt = _read_delivery_receipt_file(receipt_path)
        except Exception as exc:
            errors.append(f"读取回执失败: {exc}")
    elif task_id.strip():
        source = f"outbox:{task_id.strip()}"
        reader = getattr(getattr(ctx, "state_store", None), "get_delivery_outbox", None)
        if callable(reader):
            receipt = delivery_outbox_receipt(reader(task_id.strip()))
        else:
            errors.append("state_store 不支持 delivery_outbox")
    else:
        errors.append("必须指定 --task-id 或 --receipt")

    if not receipt and not errors:
        errors.append("未找到稳定发送回执")

    status = str(receipt.get("status") or "")
    if receipt:
        ok_status = status == "sent"
        checks.append({
            "name": "outbox_status",
            "ok": ok_status,
            "value": status,
        })
        if not ok_status:
            errors.append(f"outbox.status 不是 sent: {status or '<empty>'}")

    zip_path = str(receipt.get("zip_path") or "").strip()
    expected_size = int(receipt.get("zip_size") or 0)
    expected_sha = str(receipt.get("zip_sha256") or "").strip()
    if zip_path:
        path = Path(zip_path).expanduser()
        exists = path.is_file()
        checks.append({
            "name": "local_zip_exists",
            "ok": exists,
            "value": str(path),
        })
        if exists:
            actual_size = path.stat().st_size
            size_ok = expected_size <= 0 or actual_size == expected_size
            checks.append({
                "name": "local_zip_size",
                "ok": size_ok,
                "expected": expected_size,
                "actual": actual_size,
            })
            if not size_ok:
                errors.append(f"本地 zip 大小不匹配: expected={expected_size} actual={actual_size}")
            actual_sha = _file_sha256(path)
            sha_ok = not expected_sha or actual_sha == expected_sha
            checks.append({
                "name": "local_zip_sha256",
                "ok": sha_ok,
                "expected": expected_sha,
                "actual": actual_sha,
            })
            if not sha_ok:
                errors.append("本地 zip SHA256 不匹配")
        else:
            errors.append(f"本地 zip 不存在: {path}")

    remote_url = str(receipt.get("remote_url") or "").strip()
    if remote_url and verify_remote:
        ok, message = await verify_remote_file_url(
            remote_url,
            retry_attempts=max(1, ctx.settings.delivery.retry_attempts),
            retry_delay_sec=max(0.0, ctx.settings.delivery.retry_delay_sec),
        )
        checks.append({
            "name": "remote_url_reachable",
            "ok": ok,
            "value": remote_url,
            "message": message,
        })
        if not ok:
            errors.append(f"远程链接不可访问: {message}")
    elif remote_url:
        checks.append({
            "name": "remote_url_reachable",
            "ok": True,
            "value": remote_url,
            "skipped": True,
        })

    if download_remote and verify_remote and not remote_url:
        checks.append({
            "name": "remote_url_present",
            "ok": False,
            "value": "",
            "message": "要求下载远端文件核验，但回执没有 remote_url",
        })
        errors.append("要求下载远端文件核验，但回执没有 remote_url")

    if remote_url and verify_remote and download_remote:
        remote = await _download_remote_file_fingerprint(remote_url, max_bytes=max_remote_bytes)
        download_ok = bool(remote.get("ok"))
        checks.append({
            "name": "remote_download",
            "ok": download_ok,
            "value": remote_url,
            "message": str(remote.get("message") or ""),
        })
        if download_ok:
            actual_remote_size = int(remote.get("size") or 0)
            remote_size_ok = expected_size <= 0 or actual_remote_size == expected_size
            checks.append({
                "name": "remote_zip_size",
                "ok": remote_size_ok,
                "expected": expected_size,
                "actual": actual_remote_size,
            })
            if not remote_size_ok:
                errors.append(
                    f"远程 zip 大小不匹配: expected={expected_size} actual={actual_remote_size}"
                )
            actual_remote_sha = str(remote.get("sha256") or "")
            remote_sha_ok = not expected_sha or actual_remote_sha == expected_sha
            checks.append({
                "name": "remote_zip_sha256",
                "ok": remote_sha_ok,
                "expected": expected_sha,
                "actual": actual_remote_sha,
            })
            if not remote_sha_ok:
                errors.append("远程 zip SHA256 不匹配")
        else:
            errors.append(str(remote.get("message") or "远程文件下载核验失败"))

    return {
        "ok": not errors,
        "source": source,
        "receipt": receipt,
        "checks": checks,
        "errors": errors,
    }


async def _download_remote_file_fingerprint(
    remote_url: str,
    *,
    max_bytes: int,
) -> dict[str, object]:
    import httpx

    url = remote_url.strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "message": f"下载链接不是 HTTP(S): {url}"}
    max_size = max(0, int(max_bytes))
    digest = hashlib.sha256()
    total = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            async with client.stream("GET", url, follow_redirects=True) as resp:
                if resp.status_code < 200 or resp.status_code >= 400:
                    return {"ok": False, "message": f"GET {resp.status_code}"}
                content_length = resp.headers.get("content-length")
                if max_size and content_length:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > max_size:
                        return {
                            "ok": False,
                            "message": f"远程文件超过核验上限: {declared} > {max_size}",
                        }
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if max_size and total > max_size:
                        return {
                            "ok": False,
                            "message": f"远程文件超过核验上限: {total} > {max_size}",
                        }
                    digest.update(chunk)
        return {
            "ok": True,
            "message": "GET 200",
            "size": total,
            "sha256": digest.hexdigest(),
        }
    except Exception as exc:
        return {"ok": False, "message": f"GET 异常: {exc}"}


def _delivery_env_file_path(ctx: AppContext) -> str:
    import os

    base = ctx.settings.delivery
    overrides = getattr(ctx, "delivery_overrides", None) or {}
    env_file_path = str(overrides.get("deliveryEnvFile") or "").strip()
    if not env_file_path:
        env_file_path = (_read_state_setting(ctx, "deliveryEnvFile") or "").strip()
    if not env_file_path:
        env_file_path = base.env_file.strip()
    if not env_file_path:
        env_file_path = os.environ.get("SPIDER_GD_DELIVERY_ENV_FILE", "").strip()
    return env_file_path


def _effective_delivery_settings(ctx: AppContext) -> DeliverySettings:
    from spider_gd.config.delivery_env import read_delivery_env_file_overrides

    base = ctx.settings.delivery
    overrides = getattr(ctx, "delivery_overrides", None) or {}

    env_file_path = _delivery_env_file_path(ctx)
    env_overrides = read_delivery_env_file_overrides(env_file_path) if env_file_path else {}

    def text(key: str, default: str) -> str:
        if key in overrides:
            return str(overrides[key]).strip()
        value = _read_state_setting(ctx, key)
        if value is not None:
            stripped = value.strip()
            if stripped or key not in env_overrides:
                return stripped
        if key in env_overrides:
            return str(env_overrides[key]).strip()
        return default

    def flag(key: str, default: bool) -> bool:
        if key in overrides:
            return _bool_setting(str(overrides[key]), default)
        value = _read_state_setting(ctx, key)
        if value is not None:
            return _bool_setting(value, default)
        if key in env_overrides:
            return _bool_setting(str(env_overrides[key]), default)
        return default

    def number(key: str, default: int) -> int:
        if key in overrides:
            return _int_setting(str(overrides[key]), default)
        value = _read_state_setting(ctx, key)
        if value is not None:
            return _int_setting(value, default)
        if key in env_overrides:
            return _int_setting(str(env_overrides[key]), default)
        return default

    def decimal(key: str, default: float) -> float:
        if key in overrides:
            return _float_setting(str(overrides[key]), default)
        value = _read_state_setting(ctx, key)
        if value is not None:
            return _float_setting(value, default)
        if key in env_overrides:
            return _float_setting(str(env_overrides[key]), default)
        return default

    return DeliverySettings(
        enabled=flag("deliveryEnabled", base.enabled),
        env_file=env_file_path,
        backend=text("deliveryBackend", base.backend),
        wecom_webhook=text("deliveryWecomWebhook", base.wecom_webhook),
        serverchan_send_key=text("deliveryServerchanSendKey", base.serverchan_send_key),
        serverchan_channel=text("deliveryServerchanChannel", base.serverchan_channel),
        serverchan_openid=text("deliveryServerchanOpenid", base.serverchan_openid),
        pushplus_token=text("deliveryPushplusToken", base.pushplus_token),
        pushplus_channel=text("deliveryPushplusChannel", base.pushplus_channel),
        desktop_wechat_target=text(
            "deliveryDesktopWechatTarget",
            base.desktop_wechat_target,
        ),
        desktop_wechat_backend=text(
            "deliveryDesktopWechatBackend",
            base.desktop_wechat_backend,
        ),
        desktop_wechat_select_mode=text(
            "deliveryDesktopWechatSelectMode",
            base.desktop_wechat_select_mode,
        ),
        desktop_wechat_profile_path=text(
            "deliveryDesktopWechatProfilePath",
            base.desktop_wechat_profile_path,
        ),
        desktop_wechat_app_name=text(
            "deliveryDesktopWechatAppName",
            base.desktop_wechat_app_name,
        ),
        desktop_wechat_pause_sec=max(
            0.0,
            decimal("deliveryDesktopWechatPauseSec", base.desktop_wechat_pause_sec),
        ),
        desktop_wechat_hash_threshold=max(
            0,
            number(
                "deliveryDesktopWechatHashThreshold",
                base.desktop_wechat_hash_threshold,
            ),
        ),
        desktop_wechat_allow_unstable_search=flag(
            "deliveryDesktopWechatAllowUnstableSearch",
            base.desktop_wechat_allow_unstable_search,
        ),
        send_message=flag("deliverySendMessage", base.send_message),
        send_file=flag("deliverySendFile", base.send_file),
        file_delivery=text("deliveryFileDelivery", base.file_delivery),
        wecom_file_max_mb=number("deliveryWecomFileMaxMb", base.wecom_file_max_mb),
        remote_upload_url=text("deliveryRemoteUploadUrl", base.remote_upload_url),
        remote_upload_token=text("deliveryRemoteUploadToken", base.remote_upload_token),
        remote_upload_relative_path=text(
            "deliveryRemoteUploadRelativePath",
            base.remote_upload_relative_path,
        ),
        remote_upload_verify_url=flag(
            "deliveryRemoteUploadVerifyUrl",
            base.remote_upload_verify_url,
        ),
        retry_attempts=max(1, number("deliveryRetryAttempts", base.retry_attempts)),
        retry_delay_sec=max(0.0, decimal("deliveryRetryDelaySec", base.retry_delay_sec)),
        title=text("deliveryTitle", base.title),
    )


def _desktop_wechat_options(
    settings: DeliverySettings,
    *,
    message: str,
    file_path: str | Path | None = None,
    dry_run: bool,
):
    from spider_gd.gateway.delivery.desktop_wechat import DesktopWechatOptions

    backend = settings.desktop_wechat_backend.strip().lower().replace("-", "_") or "powershell"
    select_mode = (
        settings.desktop_wechat_select_mode.strip().lower().replace("_", "-")
        or "current"
    )
    return DesktopWechatOptions(
        target=settings.desktop_wechat_target.strip(),
        message=message,
        file_path=file_path,
        app_name=settings.desktop_wechat_app_name.strip(),
        pause_sec=max(0.0, settings.desktop_wechat_pause_sec),
        backend=backend,
        select_mode=select_mode,
        profile_path=settings.desktop_wechat_profile_path.strip() or "./data/wechat_targets.json",
        hash_threshold=max(0, settings.desktop_wechat_hash_threshold),
        allow_unstable_search=settings.desktop_wechat_allow_unstable_search,
        dry_run=dry_run,
    )


async def _send_desktop_wechat_delivery(
    settings: DeliverySettings,
    *,
    message: str,
    file_path: str | Path | None = None,
    dry_run: bool = False,
):
    from spider_gd.gateway.delivery.desktop_wechat import DesktopWechatSender

    options = _desktop_wechat_options(
        settings,
        message=message,
        file_path=file_path,
        dry_run=dry_run,
    )
    result = await asyncio.to_thread(DesktopWechatSender().send, options)
    if not result.ok:
        raise RuntimeError(result.error_msg or "本机微信 PoC 发送失败")
    return result


def _build_delivery_hook(ctx: AppContext):
    settings = _effective_delivery_settings(ctx)
    if not settings.enabled:
        return None

    def _state_setting(key: str) -> str:
        value = _read_state_setting(ctx, key)
        return "" if value is None else value.strip()

    async def _delivery_hook(
        task_id: str,
        task_dir: str,
        zip_path: str,
        summary: dict[str, object],
    ) -> None:
        backend = settings.backend.strip().lower().replace("-", "_")
        if backend not in {"wecom_webhook", "serverchan", "pushplus", "desktop_wechat"}:
            raise RuntimeError(f"不支持的产物发送后端: {settings.backend}")
        if backend == "wecom_webhook" and not settings.wecom_webhook.strip():
            raise RuntimeError("delivery.wecom_webhook 未配置")
        if backend == "serverchan" and not settings.serverchan_send_key.strip():
            raise RuntimeError("delivery.serverchan_send_key 未配置")
        if backend == "pushplus" and not settings.pushplus_token.strip():
            raise RuntimeError("delivery.pushplus_token 未配置")
        if backend == "desktop_wechat" and not settings.desktop_wechat_target.strip():
            raise RuntimeError("delivery.desktop_wechat_target 未配置")
        if not settings.send_message and not settings.send_file:
            logger.info("产物发送已启用，但 send_message/send_file 均为 false，跳过")
            return

        from spider_gd.gateway.alert.webhook_sender import (
            send_pushplus,
            send_serverchan,
            send_wecom,
            send_wecom_file,
        )
        from spider_gd.gateway.workshop_uploader import (
            upload_file_to_cloud,
            verify_remote_file_url,
        )

        zip_file = Path(zip_path)
        total = summary.get("total", 0)
        success = summary.get("success", 0)
        failed = summary.get("failed", 0)
        operator_message = str(
            summary.get("operator_message") or summary.get("message") or ""
        ).strip()
        ok = True
        remote_url = str(summary.get("remote_url") or "").strip()
        file_sent = _bool_setting(str(summary.get("file_sent") or ""), False)
        file_note = "未发送文件"
        zip_size = int(summary.get("zip_size") or 0)
        zip_sha256 = str(summary.get("zip_sha256") or "").strip()
        file_delivery = settings.file_delivery.strip().lower().replace("-", "_")
        if file_delivery not in {"auto", "wecom_file", "remote_link"}:
            raise RuntimeError(f"不支持的文件发送方式: {settings.file_delivery}")

        file_exists = zip_file.exists()
        file_size = zip_file.stat().st_size if file_exists else 0
        if file_exists:
            zip_size = zip_size or file_size
            if not zip_sha256:
                zip_sha256 = _file_sha256(zip_file)
        direct_limit = min(
            max(1, settings.wecom_file_max_mb) * 1024 * 1024,
            _WECOM_ROBOT_FILE_MAX_BYTES,
        )
        link_only_backend = backend in {"serverchan", "pushplus"}
        desktop_direct_backend = backend == "desktop_wechat"
        exceeds_wecom_file_limit = (
            backend == "wecom_webhook"
            and file_delivery == "wecom_file"
            and not file_sent
            and file_size > _WECOM_ROBOT_FILE_MAX_BYTES
        )
        should_upload_remote = settings.send_file and (
            bool(remote_url)
            or link_only_backend
            or file_delivery == "remote_link"
            or (
                file_delivery == "auto"
                and not desktop_direct_backend
                and file_size > direct_limit
            )
            or exceeds_wecom_file_limit
        )
        should_send_wecom_file = (
            settings.send_file
            and backend == "wecom_webhook"
            and not should_upload_remote
        )
        should_send_desktop_file = (
            settings.send_file
            and desktop_direct_backend
            and not should_upload_remote
        )
        if settings.send_file and link_only_backend and file_delivery == "wecom_file":
            logger.info("个人微信通知后端不支持文件直发，自动切换为远程链接")
        if exceeds_wecom_file_limit:
            logger.info("企业微信机器人文件超过 20MB，自动切换为远程链接")

        async def _verify_existing_remote_url() -> None:
            if not remote_url or not settings.remote_upload_verify_url:
                return
            verified, message = await verify_remote_file_url(
                remote_url,
                retry_attempts=settings.retry_attempts,
                retry_delay_sec=settings.retry_delay_sec,
            )
            if not verified:
                raise RuntimeError(f"下载链接验证失败: {message}")

        async def _upload_remote_link(reason: str = "") -> str:
            nonlocal remote_url
            if remote_url:
                await _verify_existing_remote_url()
                return remote_url
            if not file_exists:
                raise RuntimeError(f"待发送文件不存在: {zip_path}")
            upload_base = (
                settings.remote_upload_url.strip()
                or _state_setting("workshopUploadUrl")
            )
            upload_token = (
                settings.remote_upload_token.strip()
                or _state_setting("workshopUploadToken")
            )
            if not upload_base:
                raise RuntimeError(
                    "需要远程上传发送链接，但未配置 remote_upload_url/workshopUploadUrl"
                )
            upload_result = await upload_file_to_cloud(
                upload_base,
                upload_token,
                zip_path,
                relative_path=settings.remote_upload_relative_path,
                retry_attempts=settings.retry_attempts,
                retry_delay_sec=settings.retry_delay_sec,
                verify_remote_url=settings.remote_upload_verify_url,
            )
            if not upload_result.ok or not upload_result.remote_url:
                raise RuntimeError(upload_result.message or "远程上传任务产物失败")
            remote_url = upload_result.remote_url
            marker_remote = getattr(
                getattr(ctx, "state_store", None),
                "mark_delivery_outbox_remote_url",
                None,
            )
            if callable(marker_remote):
                marker_remote(task_id, remote_url)
            if reason:
                logger.info("{}，已切换为远程下载链接：{}", reason, remote_url)
            return remote_url

        if should_upload_remote:
            if remote_url:
                await _verify_existing_remote_url()
                file_note = f"下载链接：{remote_url}"
            else:
                remote_url = await _upload_remote_link()
                file_note = f"下载链接：{remote_url}"
        elif should_send_wecom_file:
            if file_sent:
                file_note = "附件：已在前一次发送中送达"
            elif not file_exists:
                raise RuntimeError(f"待发送文件不存在: {zip_path}")
            else:
                file_note = "附件：已通过企业微信机器人文件消息发送"
        elif should_send_desktop_file:
            if file_sent:
                file_note = "附件：已在前一次发送中送达"
            elif not file_exists:
                raise RuntimeError(f"待发送文件不存在: {zip_path}")
            else:
                file_note = "附件：将通过本机微信发送"

        if should_send_wecom_file and not file_sent:
            ok = await send_wecom_file(
                settings.wecom_webhook,
                zip_path,
                retry_attempts=settings.retry_attempts,
                retry_delay_sec=settings.retry_delay_sec,
            )
            if not ok:
                fallback_configured = bool(
                    settings.remote_upload_url.strip()
                    or _state_setting("workshopUploadUrl")
                )
                if fallback_configured:
                    try:
                        remote_url = await _upload_remote_link("企业微信文件直发失败")
                    except Exception as exc:
                        raise RuntimeError(
                            f"任务产物文件发送失败，远程链接兜底也失败: {exc}"
                        ) from exc
                    file_note = f"附件直发失败，已改为下载链接：{remote_url}"
                    ok = True
                else:
                    raise RuntimeError("任务产物文件发送失败")
            else:
                marker_file_sent = getattr(
                    getattr(ctx, "state_store", None),
                    "mark_delivery_outbox_file_sent",
                    None,
                )
                if callable(marker_file_sent):
                    marker_file_sent(task_id)

        if settings.send_message or remote_url or (should_send_desktop_file and not file_sent):
            zip_meta_lines = []
            if zip_size:
                zip_meta_lines.append(f"大小：{zip_size} bytes")
            if zip_sha256:
                zip_meta_lines.append(f"SHA256：`{zip_sha256}`")
            zip_meta = "\n\n".join(zip_meta_lines)
            note_line = f"备注：{operator_message}\n\n" if operator_message else ""
            message = (
                f"任务ID：{task_id}\n\n"
                f"成功：{success}/{total}\n\n"
                f"失败：{failed}\n\n"
                f"压缩包：{zip_file.name}\n\n"
                f"{zip_meta}\n\n"
                f"{file_note}\n\n"
                f"{note_line}"
                f"本机路径：`{zip_path}`"
            )
            desktop_file_path: str | None = None
            if should_send_desktop_file and not file_sent:
                desktop_file_path = str(zip_file)
            message_text = message if settings.send_message or remote_url else ""
            if backend == "wecom_webhook":
                ok = await send_wecom(
                    settings.wecom_webhook,
                    message,
                    title=settings.title,
                    retry_attempts=settings.retry_attempts,
                    retry_delay_sec=settings.retry_delay_sec,
                )
            elif backend == "serverchan":
                ok = await send_serverchan(
                    settings.serverchan_send_key,
                    message,
                    title=settings.title,
                    channel=settings.serverchan_channel,
                    openid=settings.serverchan_openid,
                    retry_attempts=settings.retry_attempts,
                    retry_delay_sec=settings.retry_delay_sec,
                )
            elif backend == "pushplus":
                ok = await send_pushplus(
                    settings.pushplus_token,
                    message,
                    title=settings.title,
                    channel=settings.pushplus_channel,
                    retry_attempts=settings.retry_attempts,
                    retry_delay_sec=settings.retry_delay_sec,
                )
            else:
                result = await _send_desktop_wechat_delivery(
                    settings,
                    message=message_text,
                    file_path=desktop_file_path,
                    dry_run=False,
                )
                ok = result.ok
                if desktop_file_path:
                    marker_file_sent = getattr(
                        getattr(ctx, "state_store", None),
                        "mark_delivery_outbox_file_sent",
                        None,
                    )
                    if callable(marker_file_sent):
                        marker_file_sent(task_id)

        if not ok:
            raise RuntimeError("任务产物通知发送失败")

        logger.success("任务产物已发送：task={} file={} remote={}", task_id, zip_path, remote_url)

    return _delivery_hook


def _build_reliable_delivery_hook(ctx: AppContext):
    async def _delivery_hook(
        task_id: str,
        task_dir: str,
        zip_path: str,
        summary: dict[str, object],
    ) -> None:
        effective_summary = dict(summary)
        zip_identity = _delivery_zip_identity(zip_path)
        reader = getattr(ctx.state_store, "get_delivery_outbox", None)
        enqueue = getattr(ctx.state_store, "enqueue_delivery_outbox", None)
        try:
            raw_hook = _build_delivery_hook(ctx)
            if raw_hook is None:
                logger.info("稳定发送未启用，跳过发送：task={} file={}", task_id, zip_path)
                return
            delivery_identity = _delivery_identity(ctx)
        except Exception as exc:
            if callable(enqueue):
                enqueue(
                    task_id=task_id,
                    task_dir=task_dir,
                    zip_path=zip_path,
                    summary=summary,
                    error_msg=str(exc),
                    delivery_identity="",
                )
                _write_delivery_receipt_for_task(
                    ctx,
                    task_id,
                    command="auto-delivery",
                    extra={"task_dir": task_dir, "summary": summary},
                )
            logger.warning("稳定发送配置不可用，已保留待补发记录：task={} error={}", task_id, exc)
            raise

        if callable(reader):
            existing = reader(task_id)
            existing_delivery_identity = str(existing.get("delivery_identity") or "") if isinstance(existing, dict) else ""
            if (
                isinstance(existing, dict)
                and str(existing.get("status") or "") == "sent"
                and str(existing.get("zip_path") or "") == zip_path
                and str(existing.get("zip_identity") or "") == zip_identity
                and existing_delivery_identity == delivery_identity
                and zip_identity
            ):
                logger.info("任务产物已发送，跳过重复发送：task={} file={}", task_id, zip_path)
                _write_delivery_receipt_for_task(
                    ctx,
                    task_id,
                    command="auto-delivery",
                    extra={
                        "task_dir": task_dir,
                        "summary": summary,
                        "skipped_duplicate": True,
                    },
                )
                return
        if callable(enqueue):
            enqueue(
                task_id=task_id,
                task_dir=task_dir,
                zip_path=zip_path,
                summary=summary,
                error_msg="等待发送",
                delivery_identity=delivery_identity,
            )
        if callable(reader):
            item = reader(task_id)
            if isinstance(item, dict):
                remote_url = str(item.get("remote_url") or "").strip()
                if remote_url and not effective_summary.get("remote_url"):
                    effective_summary["remote_url"] = remote_url
                file_sent = int(item.get("file_sent") or 0)
                if file_sent and not effective_summary.get("file_sent"):
                    effective_summary["file_sent"] = "true"
                zip_size = int(item.get("zip_size") or 0)
                if zip_size and not effective_summary.get("zip_size"):
                    effective_summary["zip_size"] = zip_size
                zip_sha256 = str(item.get("zip_sha256") or "").strip()
                if zip_sha256 and not effective_summary.get("zip_sha256"):
                    effective_summary["zip_sha256"] = zip_sha256
        try:
            await raw_hook(task_id, task_dir, zip_path, effective_summary)
            marker = getattr(ctx.state_store, "mark_delivery_outbox_delivered", None)
            if callable(marker):
                delivered_item = reader(task_id) if callable(reader) else None
                marker(task_id, _delivery_success_note(ctx, delivered_item))
            _write_delivery_receipt_for_task(
                ctx,
                task_id,
                command="auto-delivery",
                extra={"task_dir": task_dir, "summary": summary},
            )
        except Exception as exc:
            marker_failed = getattr(ctx.state_store, "mark_delivery_outbox_failed", None)
            if callable(marker_failed):
                marker_failed(task_id, str(exc))
            _write_delivery_receipt_for_task(
                ctx,
                task_id,
                command="auto-delivery",
                extra={"task_dir": task_dir, "summary": summary},
            )
            logger.warning("任务产物发送失败，已保留在待补发队列：task={} error={}", task_id, exc)
            raise

    return _delivery_hook


async def send_delivery_test_message(ctx: AppContext) -> bool:
    """按当前 delivery backend 发送一条无文件测试消息。"""
    settings = _effective_delivery_settings(ctx)
    backend = settings.backend.strip().lower().replace("-", "_")
    if backend == "wecom_webhook":
        if not settings.wecom_webhook.strip():
            raise RuntimeError("delivery.wecom_webhook 未配置")
        from spider_gd.gateway.alert.webhook_sender import send_wecom

        return await send_wecom(
            settings.wecom_webhook,
            "delivery-test",
            title=settings.title,
            retry_attempts=settings.retry_attempts,
            retry_delay_sec=settings.retry_delay_sec,
        )
    if backend == "serverchan":
        if not settings.serverchan_send_key.strip():
            raise RuntimeError("delivery.serverchan_send_key 未配置")
        from spider_gd.gateway.alert.webhook_sender import send_serverchan

        return await send_serverchan(
            settings.serverchan_send_key,
            "delivery-test",
            title=settings.title,
            channel=settings.serverchan_channel,
            openid=settings.serverchan_openid,
            retry_attempts=settings.retry_attempts,
            retry_delay_sec=settings.retry_delay_sec,
        )
    if backend == "pushplus":
        if not settings.pushplus_token.strip():
            raise RuntimeError("delivery.pushplus_token 未配置")
        from spider_gd.gateway.alert.webhook_sender import send_pushplus

        return await send_pushplus(
            settings.pushplus_token,
            "delivery-test",
            title=settings.title,
            channel=settings.pushplus_channel,
            retry_attempts=settings.retry_attempts,
            retry_delay_sec=settings.retry_delay_sec,
        )
    if backend == "desktop_wechat":
        if not settings.desktop_wechat_target.strip():
            raise RuntimeError("delivery.desktop_wechat_target 未配置")
        result = await _send_desktop_wechat_delivery(
            settings,
            message="delivery-test",
            dry_run=False,
        )
        return result.ok
    raise RuntimeError(f"不支持的产物发送后端: {settings.backend}")


async def retry_pending_deliveries(
    ctx: AppContext,
    *,
    limit: int = 20,
    include_recoverable_failed: bool = False,
) -> dict[str, object]:
    """补发 delivery outbox 中的任务产物。"""
    lock = _delivery_retry_lock(ctx)
    async with lock:
        return await _retry_pending_deliveries_locked(
            ctx,
            limit=limit,
            include_recoverable_failed=include_recoverable_failed,
        )


def _delivery_retry_lock(ctx: AppContext) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = getattr(ctx, "_delivery_retry_lock", None)
    lock_loop = getattr(ctx, "_delivery_retry_lock_loop", None)
    if lock is None or lock_loop is not loop:
        lock = asyncio.Lock()
        ctx._delivery_retry_lock = lock
        ctx._delivery_retry_lock_loop = loop
    return lock


async def _retry_pending_deliveries_locked(
    ctx: AppContext,
    *,
    limit: int,
    include_recoverable_failed: bool,
) -> dict[str, object]:
    """补发 delivery outbox 中的任务产物。调用方需持有 retry lock。"""
    try:
        hook = _build_delivery_hook(ctx)
    except Exception as exc:
        from spider_gd.config.delivery_env import DeliveryEnvFileError

        if not isinstance(exc, DeliveryEnvFileError):
            raise
        return {
            "ok": False,
            "retried": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "blocked": 0,
            "requeued_failed": 0,
            "errors": [str(exc)],
            "env_file_error": True,
        }
    lister = getattr(ctx.state_store, "list_delivery_outbox", None)
    if hook is None or not callable(lister):
        errors: list[str] = []
        if hook is None:
            errors.append("delivery.enabled 未开启")
        if not callable(lister):
            errors.append("state_store 不支持 delivery_outbox")
        return {
            "ok": False,
            "retried": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "blocked": 0,
            "requeued_failed": 0,
            "errors": errors,
        }
    settings = _effective_delivery_settings(ctx)
    delivery_identity = _delivery_identity(ctx, settings)

    reset_stale = getattr(ctx.state_store, "reset_stale_delivery_outbox", None)
    if callable(reset_stale):
        reset_stale(older_than_sec=_DELIVERY_RETRY_STALE_SEC)
    requeued_failed = (
        _requeue_recoverable_failed_deliveries(ctx, limit=limit)
        if include_recoverable_failed
        else 0
    )
    pending = lister(status="pending", limit=limit)
    enqueuer = getattr(ctx.state_store, "enqueue_delivery_outbox", None)
    reader = getattr(ctx.state_store, "get_delivery_outbox", None)
    claimer = getattr(ctx.state_store, "claim_delivery_outbox", None)
    marker_ok = getattr(ctx.state_store, "mark_delivery_outbox_delivered", None)
    marker_failed = getattr(ctx.state_store, "mark_delivery_outbox_failed", None)
    sent = 0
    failed = 0
    skipped = 0
    blocked = 0
    errors: list[str] = []
    receipt_paths: list[str] = []

    for item in pending:
        task_id = str(item.get("task_id", ""))
        task_dir = str(item.get("task_dir", ""))
        zip_path = str(item.get("zip_path", ""))
        summary = item.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        item_delivery_identity = str(item.get("delivery_identity") or "")
        existing_remote_url = str(item.get("remote_url") or "").strip()
        if (
            item_delivery_identity != delivery_identity
            and callable(enqueuer)
        ):
            enqueuer(
                task_id=task_id,
                task_dir=task_dir,
                zip_path=zip_path,
                summary=summary,
                error_msg="发送目标已变更，等待补发",
                remote_url=existing_remote_url,
                delivery_identity=delivery_identity,
            )
            if callable(reader):
                refreshed = reader(task_id)
                if isinstance(refreshed, dict):
                    item = refreshed
                    refreshed_summary = item.get("summary")
                    if isinstance(refreshed_summary, dict):
                        summary = refreshed_summary
        remote_url = str(item.get("remote_url") or "").strip()
        if remote_url and not summary.get("remote_url"):
            summary = {**summary, "remote_url": remote_url}
        file_sent = int(item.get("file_sent") or 0)
        if file_sent and not summary.get("file_sent"):
            summary = {**summary, "file_sent": "true"}
        zip_size = int(item.get("zip_size") or 0)
        if zip_size and not summary.get("zip_size"):
            summary = {**summary, "zip_size": zip_size}
        zip_sha256 = str(item.get("zip_sha256") or "").strip()
        if zip_sha256 and not summary.get("zip_sha256"):
            summary = {**summary, "zip_sha256": zip_sha256}

        if not task_id or (not zip_path and not remote_url):
            skipped += 1
            continue
        if callable(claimer) and not claimer(task_id):
            skipped += 1
            continue
        if (
            settings.send_file
            and zip_path
            and not Path(zip_path).exists()
            and not remote_url
            and not file_sent
        ):
            blocked += 1
            message = f"待补发文件不存在: {zip_path}"
            errors.append(f"{task_id}: {message}")
            if callable(marker_failed):
                marker_failed(task_id, message, status="failed")
            receipt_path = _write_delivery_receipt_for_task(
                ctx,
                task_id,
                command="delivery-retry",
                extra={"task_dir": task_dir, "summary": summary, "blocked": True},
            )
            if receipt_path:
                receipt_paths.append(receipt_path)
            continue

        try:
            await hook(task_id, task_dir, zip_path, summary)
            sent += 1
            if callable(marker_ok):
                delivered_item = reader(task_id) if callable(reader) else None
                marker_ok(task_id, _delivery_success_note(ctx, delivered_item))
            receipt_path = _write_delivery_receipt_for_task(
                ctx,
                task_id,
                command="delivery-retry",
                extra={"task_dir": task_dir, "summary": summary},
            )
            if receipt_path:
                receipt_paths.append(receipt_path)
        except Exception as exc:
            failed += 1
            message = str(exc)
            errors.append(f"{task_id}: {message}")
            if callable(marker_failed):
                marker_failed(task_id, message)
            receipt_path = _write_delivery_receipt_for_task(
                ctx,
                task_id,
                command="delivery-retry",
                extra={"task_dir": task_dir, "summary": summary},
            )
            if receipt_path:
                receipt_paths.append(receipt_path)
            logger.warning("待补发任务产物发送失败：task={} error={}", task_id, exc)

    return {
        "ok": failed == 0 and blocked == 0,
        "retried": len(pending),
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "blocked": blocked,
        "requeued_failed": requeued_failed,
        "receipt_paths": receipt_paths,
        "errors": errors,
    }


def _requeue_recoverable_failed_deliveries(ctx: AppContext, *, limit: int) -> int:
    """恢复 still-sendable failed records for unattended retry jobs."""
    lister = getattr(ctx.state_store, "list_delivery_outbox", None)
    enqueuer = getattr(ctx.state_store, "enqueue_delivery_outbox", None)
    if not callable(lister) or not callable(enqueuer):
        return 0
    requeued = 0
    for item in lister(status="failed", limit=limit):
        task_id = str(item.get("task_id") or "")
        task_dir = str(item.get("task_dir") or "")
        zip_path = str(item.get("zip_path") or "")
        remote_url = str(item.get("remote_url") or "").strip()
        if not task_id:
            continue
        if not remote_url and (not zip_path or not Path(zip_path).exists()):
            continue
        summary = item.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        enqueuer(
            task_id=task_id,
            task_dir=task_dir,
            zip_path=zip_path,
            summary=summary,
            error_msg="自动恢复失败记录，等待补发",
            remote_url=remote_url,
            delivery_identity=str(item.get("delivery_identity") or ""),
        )
        requeued += 1
    return requeued


def _delivery_retry_job(ctx: AppContext):
    async def _handler(_payload: dict[str, object]) -> None:
        result = await retry_pending_deliveries(ctx, include_recoverable_failed=True)
        retried = int(result.get("retried", 0))
        sent = int(result.get("sent", 0))
        failed = int(result.get("failed", 0))
        skipped = int(result.get("skipped", 0))
        requeued = int(result.get("requeued_failed", 0))
        if retried or sent or failed or skipped or requeued:
            logger.info(
                "任务产物待补发处理完成：requeued_failed={} retried={} sent={} failed={} skipped={}",
                requeued,
                retried,
                sent,
                failed,
                skipped,
            )

    return _handler


def _safe_artifact_name(value: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", value).strip("._") or "task"


_VERSION_MANIFEST_NAME = ".file_versions.json"
_VERSION_DIR_NAME = "_versions"


class DownloadFileSnapshot(dict[str, tuple[int, int]]):
    """采集前的下载文件快照。

    键是相对下载根目录的路径，值是 ``(文件大小字节, mtime_ns)``。
    ``manifests`` 另记每个 ``.file_versions.json`` 里各产物的确认时间与内容哈希。
    内容哈希未变时文件本身不会改 mtime，但清单的 ``updated_at`` 会刷新；
    哈希变了而大小和 mtime 碰巧没变时，也能从清单看出来。

    Examples:
        >>> snap = DownloadFileSnapshot()
        >>> snap.manifests
        {}
        >>> snap == {}
        True
    """

    manifests: dict[str, dict[str, str]]

    def __init__(
        self,
        files: dict[str, tuple[int, int]] | None = None,
        manifests: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """建立快照，并复制清单确认时间，避免调用方事后改到原字典。

        Args:
            files: 产物文件的大小和 mtime。空表示还没有产物。
            manifests: 清单相对路径到 ``{文件名: 确认时间与哈希}`` 的映射。

        Returns:
            None: 初始化即完成。

        Examples:
            >>> DownloadFileSnapshot({"a.pdf": (1, 2)})["a.pdf"]
            (1, 2)
        """
        super().__init__(files or {})
        self.manifests = {key: dict(value) for key, value in (manifests or {}).items()}


def _snapshot_download_files(download_dir: str | Path) -> DownloadFileSnapshot:
    """记录采集前已有的产物，以及版本清单里各文件的确认时间。

    内部文件（压缩包、登录态、版本清单、``_versions`` 归档）不进入大小/mtime 表，
    清单内容单独放在返回值的 ``manifests`` 上。

    Args:
        download_dir: 本次下载根目录。目录不存在时返回空快照。

    Returns:
        DownloadFileSnapshot: 采集前快照。

    Examples:
        >>> snap = _snapshot_download_files("/no/such/spider-gd-downloads-snapshot")
        >>> snap == {}
        True
        >>> snap.manifests
        {}
    """
    root = Path(download_dir)
    if not root.exists():
        return DownloadFileSnapshot()
    snapshot: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or _is_internal_download_artifact(root, path):
            continue
        try:
            stat = path.stat()
            rel = str(path.relative_to(root))
        except (OSError, ValueError):
            continue
        snapshot[rel] = (stat.st_size, stat.st_mtime_ns)
    return DownloadFileSnapshot(snapshot, _version_manifest_index(root))


def _is_internal_download_artifact(root: Path, path: Path) -> bool:
    """判断路径是不是下载目录里的内部文件，不应当作采集产物上传。

    版本清单 ``.file_versions.json``（含原子写入的 ``.tmp``）和 ``_versions``
    目录下的历史副本只留在本地。以点开头的真实文件（例如 ``.notes.pdf``）仍保留。

    Args:
        root: 本次下载根目录。
        path: 待判断的文件。落在根目录外时视为内部文件。

    Returns:
        bool: 内部文件时为 True。

    Examples:
        >>> root = Path("/downloads")
        >>> _is_internal_download_artifact(root, root / ".file_versions.json")
        True
        >>> _is_internal_download_artifact(root, root / "_versions" / "old.pdf")
        True
        >>> _is_internal_download_artifact(root, root / ".notes.pdf")
        False
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    name = path.name.lower()
    dir_parts = rel.parts[:-1]
    return (
        path.suffix.lower() == ".zip"
        or path.name == "storage_state.json"
        or name == _VERSION_MANIFEST_NAME
        or name == f"{_VERSION_MANIFEST_NAME}.tmp"
        or "_stable_delivery" in rel.parts
        or "_failures" in rel.parts
        or _VERSION_DIR_NAME in dir_parts
    )


def _manifest_product_name(name: object) -> str:
    """取出清单里可以安全拼回同目录的产物文件名。

    Args:
        name: 清单 ``files`` 的键。

    Returns:
        str: 合法文件名。含路径分隔符或为空时返回空字符串。

    Examples:
        >>> _manifest_product_name("日报.pdf")
        '日报.pdf'
        >>> _manifest_product_name("../x.pdf")
        ''
    """
    if not isinstance(name, str):
        return ""
    cleaned = name.strip()
    if not cleaned or cleaned in {".", ".."}:
        return ""
    if "/" in cleaned or "\\" in cleaned or cleaned != Path(cleaned).name:
        return ""
    return cleaned


def _manifest_file_stamps(path: Path) -> dict[str, str]:
    """读取一份版本清单里各产物的确认时间与内容哈希。

    两者拼成一个标记。重跑未改内容时只有时间变；同秒内换成同大小的新内容时
    只有哈希变。任一变化都表示这个文件是本轮产物。

    Args:
        path: ``.file_versions.json`` 的路径。

    Returns:
        dict[str, str]: 文件名到标记的映射。读失败或结构不对时为空。

    Examples:
        >>> _manifest_file_stamps(Path("/no/such/spider-gd-manifest.json"))
        {}
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        return {}
    stamps: dict[str, str] = {}
    for raw_name, item in files.items():
        name = _manifest_product_name(raw_name)
        if not name:
            continue
        updated = ""
        digest = ""
        if isinstance(item, dict):
            updated = str(item.get("updated_at") or "")
            digest = str(item.get("current_sha256") or "")
        stamps[name] = f"{updated}\n{digest}"
    return stamps


def _version_manifest_index(root: Path) -> dict[str, dict[str, str]]:
    """收集下载根目录下每一份版本清单的确认时间。

    Args:
        root: 下载根目录。

    Returns:
        dict[str, dict[str, str]]: 清单相对路径到 ``{文件名: 确认时间与哈希}``。

    Examples:
        >>> _version_manifest_index(Path("/no/such/spider-gd-manifests"))
        {}
    """
    if not root.exists():
        return {}
    index: dict[str, dict[str, str]] = {}
    for path in root.rglob(_VERSION_MANIFEST_NAME):
        if not path.is_file() or path.name.lower() != _VERSION_MANIFEST_NAME:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            continue
        index[rel] = _manifest_file_stamps(path)
    return index


def _reconfirmed_download_files(
    root: Path,
    before: dict[str, tuple[int, int]],
) -> list[Path]:
    """找出本轮重新写入版本清单、但文件字节可能没变的产物。

    只认确认时间或内容哈希相对采集前快照有变化（或新出现）的文件名，
    同目录里更早的其他产物不会被一起带上。快照不是
    :class:`DownloadFileSnapshot` 时无法比较清单，返回空列表。

    Args:
        root: 下载根目录。
        before: 采集前快照。带 ``manifests`` 时才比较清单。

    Returns:
        list[Path]: 本轮确认过、且仍然存在的产物路径。

    Examples:
        >>> _reconfirmed_download_files(Path("/no/such/spider-gd-reconfirmed"), {})
        []
    """
    previous = getattr(before, "manifests", None)
    if not isinstance(previous, dict):
        return []
    found: list[Path] = []
    for rel_manifest, stamps in _version_manifest_index(root).items():
        old = previous.get(rel_manifest)
        if not isinstance(old, dict):
            old = {}
        parent = (root / rel_manifest).parent
        for name, updated in stamps.items():
            if old.get(name) == updated:
                continue
            candidate = parent / name
            try:
                if not candidate.is_file() or _is_internal_download_artifact(root, candidate):
                    continue
            except OSError:
                continue
            found.append(candidate)
    return found


def _changed_download_files(
    download_dir: str | Path,
    before: dict[str, tuple[int, int]],
) -> list[Path]:
    """列出这次要打包或上传的产物。

    包含相对快照有大小或 mtime 变化的文件，以及内容未变、但版本清单在本轮
    被重新确认的文件。``.file_versions.json`` 和 ``_versions`` 归档一律排除。

    Args:
        download_dir: 下载根目录。
        before: :func:`_snapshot_download_files` 的返回值。普通 dict 只做 mtime 对比。

    Returns:
        list[Path]: 按路径排序的产物。目录不存在时为空。

    Examples:
        >>> _changed_download_files("/no/such/spider-gd-downloads-changed", {})
        []
    """
    root = Path(download_dir)
    if not root.exists():
        return []
    files: list[Path] = []
    seen: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or _is_internal_download_artifact(root, path):
            continue
        try:
            stat = path.stat()
            rel = str(path.relative_to(root))
        except (OSError, ValueError):
            continue
        if before.get(rel) != (stat.st_size, stat.st_mtime_ns):
            files.append(path)
            seen.add(rel)
    for path in _reconfirmed_download_files(root, before):
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            continue
        if rel in seen:
            continue
        files.append(path)
        seen.add(rel)
    return sorted(files)


def _zip_download_files(
    download_dir: str | Path,
    files: list[Path],
    *,
    task_name: str,
    exec_id: str,
) -> str | None:
    """把本次产物打成稳定发送用的 zip。

    版本清单、``_versions`` 归档和其他内部文件即使出现在候选列表里也不会写入。

    Args:
        download_dir: 下载根目录。zip 放在其中的 ``_stable_delivery``。
        files: 候选文件。
        task_name: 任务名，只用于 zip 文件名。
        exec_id: 执行 id，只用于 zip 文件名。

    Returns:
        str | None: zip 绝对路径。没有可打包的产物时为 None。

    Examples:
        >>> _zip_download_files("/tmp", [], task_name="t", exec_id="e") is None
        True
    """
    if not files:
        return None
    root = Path(download_dir)
    export_dir = root / "_stable_delivery"
    export_dir.mkdir(parents=True, exist_ok=True)
    zip_path = export_dir / f"{_safe_artifact_name(task_name)}_{exec_id}.zip"
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            try:
                if not path.is_file() or _is_internal_download_artifact(root, path):
                    continue
                zf.write(path, path.relative_to(root))
            except (OSError, ValueError):
                continue
            written += 1
    if written == 0:
        zip_path.unlink(missing_ok=True)
        return None
    return str(zip_path) if zip_path.exists() and zip_path.stat().st_size > 0 else None


async def deliver_task_runner_downloads(
    ctx: AppContext,
    *,
    exec_id: str,
    task_config_id: str,
    task_name: str,
    download_dir: str | Path,
    before: dict[str, tuple[int, int]],
    task_status: str = "success",
    reason: str = "",
) -> str | None:
    """把 TaskRunner 本次新增/变更下载文件打包，并走可靠发送链路。"""
    files = _changed_download_files(download_dir, before)
    if not files:
        logger.info("TaskRunner 未发现本次新增下载文件，跳过稳定发送：exec={}", exec_id)
        return None
    zip_path = _zip_download_files(
        download_dir,
        files,
        task_name=task_name,
        exec_id=exec_id,
    )
    if zip_path is None:
        logger.info("TaskRunner 未生成稳定发送 zip，跳过：exec={}", exec_id)
        return None
    delivery_hook = _build_reliable_delivery_hook(ctx)
    if delivery_hook is None:
        logger.info("稳定发送未启用，TaskRunner zip 已保留在本地：{}", zip_path)
        return zip_path

    summary: dict[str, object] = {
        "total": len(files),
        "success": len(files),
        "failed": 0 if task_status == "success" else 1,
        "source": "task_runner",
        "task_config_id": task_config_id,
        "task_name": task_name,
        "task_status": task_status,
        "reason": reason,
    }
    await delivery_hook(exec_id, str(download_dir), zip_path, summary)
    return zip_path


def build_delivery_test_plan(
    ctx: AppContext,
    *,
    file_path: str | Path | None = None,
    dry_run: bool = True,
) -> dict[str, object]:
    """Build a no-side-effect plan for validating the effective delivery settings."""
    path = Path(file_path).expanduser().resolve() if file_path else None
    env_file_path = _delivery_env_file_path(ctx)
    try:
        settings = _effective_delivery_settings(ctx)
    except Exception as exc:
        from spider_gd.config.delivery_env import DeliveryEnvFileError

        if not isinstance(exc, DeliveryEnvFileError):
            raise
        return {
            "ok": False,
            "dry_run": dry_run,
            "backend": ctx.settings.delivery.backend,
            "delivery_enabled": ctx.settings.delivery.enabled,
            "env_file_configured": bool(env_file_path),
            "env_file_loaded": False,
            "env_file": env_file_path,
            "file": str(path) if path is not None else "",
            "actions": ["读取 delivery 配置"],
            "errors": [str(exc)],
            "env_file_error": True,
        }
    remote_upload_configured = bool(
        settings.remote_upload_url.strip()
        or (_read_state_setting(ctx, "workshopUploadUrl") or "").strip()
    )
    plan: dict[str, object] = {
        "ok": True,
        "dry_run": dry_run,
        "backend": settings.backend,
        "delivery_enabled": settings.enabled,
        "env_file_configured": bool(settings.env_file.strip()),
        "env_file_loaded": bool(settings.env_file.strip()),
        "env_file": settings.env_file.strip(),
        "wecom_webhook_configured": bool(settings.wecom_webhook.strip()),
        "serverchan_configured": bool(settings.serverchan_send_key.strip()),
        "pushplus_configured": bool(settings.pushplus_token.strip()),
        "desktop_wechat_configured": bool(settings.desktop_wechat_target.strip()),
        "desktop_wechat_backend": settings.desktop_wechat_backend,
        "desktop_wechat_select_mode": settings.desktop_wechat_select_mode,
        "send_message": settings.send_message,
        "send_file": settings.send_file,
        "file_delivery": settings.file_delivery,
        "wecom_file_max_mb": settings.wecom_file_max_mb,
        "retry_attempts": settings.retry_attempts,
        "retry_delay_sec": settings.retry_delay_sec,
        "remote_upload_configured": remote_upload_configured,
        "remote_upload_verify_url": settings.remote_upload_verify_url,
        "file": str(path) if path is not None else "",
        "actions": ["读取 delivery 配置", "构建稳定发送 hook"],
    }

    errors: list[str] = []
    warnings: list[str] = []
    backend = settings.backend.strip().lower().replace("-", "_")
    file_delivery = settings.file_delivery.strip().lower().replace("-", "_")
    if not settings.enabled:
        errors.append("delivery.enabled 未开启")
    if backend not in {"wecom_webhook", "serverchan", "pushplus", "desktop_wechat"}:
        errors.append(f"不支持的 delivery.backend: {settings.backend}")
    if backend == "wecom_webhook" and not settings.wecom_webhook.strip():
        errors.append("delivery.wecom_webhook 未配置")
    if backend == "serverchan" and not settings.serverchan_send_key.strip():
        errors.append("delivery.serverchan_send_key 未配置")
    if backend == "pushplus" and not settings.pushplus_token.strip():
        errors.append("delivery.pushplus_token 未配置")
    if backend == "desktop_wechat" and not settings.desktop_wechat_target.strip():
        errors.append("delivery.desktop_wechat_target 未配置")
    if file_delivery not in {"auto", "wecom_file", "remote_link"}:
        errors.append(f"不支持的 delivery.file_delivery: {settings.file_delivery}")

    if backend == "desktop_wechat":
        warnings.append("本机微信 PoC 依赖已登录且前台可操作的 Windows 微信客户端")
        direct_file_path = path if path is not None and settings.send_file and file_delivery != "remote_link" else None
        planned_message = "delivery-test"
        if path is not None and settings.send_file and file_delivery == "remote_link":
            planned_message = "delivery-test remote link"
        try:
            from spider_gd.gateway.delivery.desktop_wechat import DesktopWechatSender

            desktop_result = DesktopWechatSender().send(
                _desktop_wechat_options(
                    settings,
                    message=planned_message,
                    file_path=direct_file_path,
                    dry_run=True,
                )
            )
            plan["desktop_wechat_platform"] = desktop_result.platform
            if desktop_result.actions:
                plan["actions"].extend(desktop_result.actions)
            if not desktop_result.ok and desktop_result.error_msg:
                errors.append(desktop_result.error_msg)
        except Exception as exc:
            errors.append(f"本机微信 PoC 检查失败: {exc}")

    if path is not None:
        if not path.exists():
            errors.append(f"文件不存在: {path}")
        elif path.is_dir():
            errors.append(f"不支持发送文件夹: {path}")
        else:
            size = path.stat().st_size
            limit = min(
                max(1, settings.wecom_file_max_mb) * 1024 * 1024,
                _WECOM_ROBOT_FILE_MAX_BYTES,
            )
            link_only_backend = backend in {"serverchan", "pushplus"}
            desktop_direct_backend = backend == "desktop_wechat"
            exceeds_wecom_file_limit = (
                backend == "wecom_webhook"
                and file_delivery == "wecom_file"
                and size > _WECOM_ROBOT_FILE_MAX_BYTES
            )
            use_remote = (
                link_only_backend
                or file_delivery == "remote_link"
                or (
                    file_delivery == "auto"
                    and not desktop_direct_backend
                    and size > limit
                )
                or exceeds_wecom_file_limit
            )
            remote_fallback_available = (
                backend == "wecom_webhook"
                and settings.send_file
                and not use_remote
                and remote_upload_configured
            )
            plan["file_size"] = size
            plan["requires_remote_upload"] = use_remote
            plan["remote_fallback_available"] = remote_fallback_available
            if settings.send_file:
                if exceeds_wecom_file_limit:
                    action = "企业微信文件超过 20MB，远程上传后发送下载链接"
                elif use_remote and link_only_backend:
                    action = "远程上传后发送个人微信下载链接"
                elif backend == "desktop_wechat" and not use_remote:
                    action = "本机微信直接发送文件"
                else:
                    action = "远程上传后发送下载链接" if use_remote else "企业微信文件直发"
            else:
                action = "仅发送完成消息，不发送文件或链接"
            plan["actions"].append(action)
            if (
                settings.send_file
                and backend == "wecom_webhook"
                and not use_remote
            ):
                if remote_fallback_available:
                    fallback_action = "文件直发失败时自动降级为远程下载链接"
                    if settings.remote_upload_verify_url:
                        fallback_action += "，并验证下载链接"
                    plan["actions"].append(fallback_action)
                else:
                    warnings.append(
                        "企业微信文件直发没有远程上传兜底；建议配置 remote_upload_url "
                        "或改为 file_delivery=remote_link"
                    )
            if not settings.send_file:
                errors.append("已指定测试文件，但 delivery.send_file=false，不会发送文件或链接")
            if settings.send_file and use_remote and not remote_upload_configured:
                errors.append("远程链接模式需要配置 remote_upload_url 或 workshopUploadUrl")
            if settings.send_file and use_remote:
                if settings.remote_upload_verify_url:
                    plan["actions"].append("上传后验证下载链接可访问")
                else:
                    warnings.append("远程下载链接验证已关闭；可能发送不可访问的链接")
    else:
        plan["actions"].append("仅发送测试消息")
        if not settings.send_message:
            errors.append("未指定文件且 delivery.send_message=false，无可发送内容")

    if errors:
        plan["ok"] = False
        plan["errors"] = errors
    if warnings:
        plan["warnings"] = warnings
    return plan


def assemble(settings: AppSettings) -> AppContext:
    """根据配置装配各模块（DI 在此集中完成）。"""
    setup_logging(
        settings.log.file_dir,
        persist_to_file=settings.log.persist_to_file,
        console_level=settings.log.console_level,
        file_level=settings.log.file_level,
        retention_days=settings.log.retention_days,
    )

    state_store = TaskStateStore(settings.storage.sqlite_path, settings.storage.enable_wal)
    landing = LocalLanding(settings.storage.data_dir)
    scheduler = TaskScheduler()

    if settings.modules.alert_gateway:
        alert: IAlertGateway = build_alert_gateway(settings, state_store)
        logger.info("告警网关已启用")
    else:
        alert = NullAlertGateway()
        logger.info("告警网关禁用，使用空实现兜底")

    endpoints = load_endpoints(settings.endpoints_file)
    logger.info("加载端点配置 {} 个：{}", len(endpoints), settings.endpoints_file)

    ctx = AppContext(
        settings=settings,
        state_store=state_store,
        landing=landing,
        scheduler=scheduler,
        alert=alert,
        agent=None,
        endpoints=endpoints,
    )

    # 控制服务：REST API 与 Agent handler 共用
    from spider_gd.gateway.control_service import ControlService

    ctx.control = ControlService(ctx)

    if settings.modules.agent and settings.agent.enabled:
        from spider_gd.gateway.agent.agent_adapter import AgentAdapter
        from spider_gd.gateway.agent.command_executor import CommandExecutor
        from spider_gd.gateway.agent.handlers import build_default_handlers
        from spider_gd.gateway.agent.permission_guard import PermissionGuard
        from spider_gd.gateway.agent.stream_client import make_webhook_responder

        handlers = build_default_handlers(ctx.control)
        executor = CommandExecutor(
            handlers=handlers, guard=PermissionGuard(settings.agent.max_permission)
        )
        responder = None
        if settings.alert.dingtalk_webhook:
            responder = make_webhook_responder(
                settings.alert.dingtalk_webhook, settings.alert.dingtalk_secret
            )
        ctx.agent = AgentAdapter(executor, responder=responder)
        logger.info("远程 Agent 已加载（权限上限 {}，handlers={}）",
                    settings.agent.max_permission, list(handlers))
    else:
        logger.info("远程 Agent 未加载")

    logger.info("装配完成：mode={} modules={}", settings.deploy_mode.value, settings.modules)
    return ctx


def build_orchestrator(ctx: AppContext, landing=None):
    """构建真实采集编排器（含 Playwright 浏览器栈）。

    返回 ``(orchestrator, runner)``，调用方负责在结束后 ``await runner.close()``。
    landing 为 None 时使用 ctx.landing（默认数据目录）。
    """
    from spider_gd.core.crawler.api_fetcher import ApiFetcher
    from spider_gd.core.crawler.captcha_solver import CaptchaSolver
    from spider_gd.core.crawler.crawl_orchestrator import CrawlOrchestrator
    from spider_gd.core.crawler.login_flow import LoginFlow
    from spider_gd.core.crawler.playwright_runner import PlaywrightRunner
    from spider_gd.core.crawler.rpa_fetcher import RpaFetcher
    from spider_gd.core.crawler.session_keeper import SessionKeeper
    from spider_gd.core.crawler.session_provider import BrowserSessionProvider
    from spider_gd.core.pipeline.data_processor import DataProcessor

    data_dir = str(landing._data_dir if landing else ctx.settings.storage.data_dir)
    def browser_event(event):
        if event.get("event") == "error_pause":
            asyncio.create_task(ctx.alert.send_alert(
                AlertLevel.ERROR, "浏览器任务已暂停：" + str(event.get("error", "等待人工处理")),
                event_type="crawl"))

    runner = PlaywrightRunner(
        ctx.settings.platform.login_url,
        headless=ctx.settings.crawler.headless,
        user_data_dir=ctx.settings.crawler.user_data_dir,
        download_dir=data_dir,
        on_event=browser_event,
    )
    session_provider = BrowserSessionProvider(_RunnerPage(runner))
    login_flow = LoginFlow(
        runner,
        CaptchaSolver(),
        session_provider,
        ctx.settings.crawler.username,
        ctx.settings.crawler.password,
    )
    keeper = SessionKeeper(login_flow, session_provider)

    async def _alert_hook(title: str, message: str) -> None:
        from spider_gd.interfaces.alert import AlertLevel

        await ctx.alert.send_alert(AlertLevel.ERROR, f"{title}：{message}", event_type="crawl")

    # RPA 兜底：API 失败时回退到浏览器点击导出
    rpa_pages = ctx.settings.rpa.endpoint_pages
    rpa = RpaFetcher(runner, rpa_pages)

    orchestrator = CrawlOrchestrator(
        session_keeper=keeper,
        api_fetcher=ApiFetcher(),
        state_store=ctx.state_store,
        landing=landing or ctx.landing,
        processor=DataProcessor().process,
        rpa_fetcher=rpa.fetch,
        lock_dir=data_dir + "/.locks",
        alert_hook=_alert_hook,
        delivery_hook=_build_reliable_delivery_hook(ctx),
        cancel_requested=lambda: runner.cancel_requested,
    )
    return orchestrator, runner


def run_crawl(
    settings: AppSettings,
    *,
    target_date: str,
    task_name: str = "采集任务",
    task_id: str | None = None,
    dry_run: bool = False,
) -> int:
    """CLI ``crawl`` 入口：装配并执行一次采集编排。"""
    ctx = assemble(settings)
    if not settings.modules.crawler:
        logger.error("采集模块未启用（modules.crawler=false），中止。")
        ctx.state_store.close()
        return 2
    if not ctx.endpoints:
        logger.error("未加载到任何端点配置（{}），中止。", settings.endpoints_file)
        ctx.state_store.close()
        return 2

    if dry_run:
        logger.info(
            "dry-run：将对 {} 个端点执行采集 target_date={}（不实际启动浏览器）",
            len(ctx.endpoints),
            target_date,
        )
        for ep in ctx.endpoints:
            logger.info("  - {} {} {}", ep.name, ep.method, ep.url)
        ctx.state_store.close()
        return 0

    async def _main() -> dict[str, object]:
        orchestrator, runner = build_orchestrator(ctx)
        try:
            return await orchestrator.run(  # type: ignore[attr-defined]
                task_name=task_name,
                endpoints=ctx.endpoints,
                target_date=target_date,
                task_id=task_id,
            )
        finally:
            await runner.close()  # type: ignore[attr-defined]

    try:
        result = asyncio.run(_main())
        logger.success("采集完成：{}", result)
        return 0 if result.get("status") != "failed" else 1
    except Exception as exc:
        logger.exception("采集执行失败：{}", exc)
        return 1
    finally:
        ctx.state_store.close()


async def run_offline_pipeline(
    ctx: AppContext,
    *,
    task_name: str,
    indicator: str,
    target_date: str,
    data: bytes,
) -> dict[str, object]:
    """离线落地链路一次完整执行：触发→落地→台账→zip→SQLite 状态。

    用真实数据 bytes（采集结果或样本）走通端到端，不依赖网络。
    """
    exec_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"T{exec_time}"
    task_dir = ctx.landing.create_task_dir(task_name, exec_time)

    ctx.state_store.create_task(
        TaskRecord(task_id, task_name, "running", task_dir, created_at=datetime.now().isoformat())
    )
    ctx.state_store.create_item(TaskItemRecord(task_id, indicator, target_date, "pending"))

    file_path = ctx.landing.save_data_file(task_dir, indicator, target_date, data)
    ctx.state_store.update_item_status(
        task_id, indicator, target_date, "success", file_path=file_path, file_size=len(data)
    )

    items = ctx.state_store.get_items(task_id)
    ledger = ctx.landing.generate_ledger(task_dir, items)
    zip_path = ctx.landing.compress_task(task_dir)
    ctx.state_store.update_task_status(task_id, "completed")
    delivery_hook = _build_reliable_delivery_hook(ctx)
    if delivery_hook is not None and zip_path is not None:
        try:
            summary_for_delivery = ctx.state_store.get_task_summary(task_id)
            await delivery_hook(task_id, task_dir, zip_path, summary_for_delivery)
        except Exception as exc:
            logger.warning("离线链路产物发送失败但不影响任务结果：task={} error={}", task_id, exc)

    summary = ctx.state_store.get_task_summary(task_id)
    logger.success("离线链路完成：{} -> {}", task_id, zip_path)
    return {
        "task_id": task_id,
        "task_dir": task_dir,
        "ledger": ledger,
        "zip": zip_path,
        "summary": summary,
    }


def _yesterday() -> str:
    from datetime import timedelta

    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _default_crawl_job(ctx: AppContext):
    """构造定时采集 job handler：每次新建浏览器栈跑一遍配置端点（昨日）。"""

    async def _handler(payload: dict[str, object]) -> None:
        task_name = str(payload.get("task_name", ctx.settings.schedule.task_name))
        target_date = str(payload.get("target_date") or _yesterday())
        orchestrator, runner = build_orchestrator(ctx)
        try:
            result = await orchestrator.run(  # type: ignore[attr-defined]
                task_name=task_name, endpoints=ctx.endpoints, target_date=target_date
            )
            if result.get("zip") is None:
                logger.info(
                    "定时采集未生成压缩包：task={} status={}",
                    result.get("task_id"),
                    result.get("status"),
                )
        finally:
            await runner.close()  # type: ignore[attr-defined]

    return _handler


def _task_runner_job(ctx: AppContext, task_id: str):
    """构造任务执行 handler：从 SQLite 读取任务配置，按 TaskRunner 流程执行。"""

    async def _handler(payload: dict[str, object]) -> None:
        import time as _time
        import uuid as _uuid

        logger.info("⏰ 定时触发: {} at {}", task_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        # 从 SQLite 读取任务配置
        cfg = ctx.state_store.get_task_config(task_id)
        if not cfg:
            logger.warning("定时任务 {} 在 SQLite 中不存在，尝试 YAML 回退", task_id)
            cfg = _load_task_config_from_yaml(task_id)
        if not cfg:
            logger.error("定时任务 {} 找不到配置", task_id)
            return

        if not cfg.get("indicators"):
            logger.warning("定时任务 {} 没有配置指标，跳过", task_id)
            return

        # 创建执行日志记录
        now_ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        exec_id = f"sch_{_uuid.uuid4().hex[:12]}"
        task_name = cfg.get("name", task_id)
        ctx.state_store.create_execution_log(
            ExecutionLogRecord(
                exec_id=exec_id, name=task_name,
                type="scheduled_auto", target_id=task_id,
                status="running", reason="",
                created_at=now_ts, updated_at=now_ts,
            )
        )

        def _fmt_event(data: dict) -> str:
            evt = data.get("event", "")
            if evt == "task_step":
                phase = data.get("phase", "")
                step = data.get("step", "?")
                total = data.get("total", "?")
                nm = data.get("name", "")
                status = data.get("status", "")
                msg = data.get("message", "")
                icon = "🔑" if phase == "login" else "📊"
                label = "登录" if phase == "login" else nm
                if phase != "login" and data.get("target_date"):
                    label = f"{label}({data.get('target_date')})"
                status_messages = {
                    "running": f"{icon} [{step}/{total}] {label}: {msg}",
                    "done": f"✅ [{step}/{total}] {label} 完成",
                    "error": f"❌ [{step}/{total}] {label} 失败: {msg}",
                    "skip": f"⏭ [{step}/{total}] {label}: 跳过",
                }
                if status in status_messages:
                    return status_messages[status]
                return f"{icon} [{step}/{total}] {label}: {status}"
            elif evt == "task_window":
                return f"📅 日期窗口 {data.get('index', '?')}/{data.get('total', '?')}: {data.get('date', '')}"
            elif evt == "task_done":
                return f"🏁 任务完成: {data.get('name', task_name)}"
            elif evt == "task_error":
                err = data.get("error", "")
                return f"💥 任务异常: {err}"
            elif evt == "aborted":
                msg = data.get("message", "")
                return f"⛔ 任务中止: {msg}"
            elif evt == "browser_start":
                return "▶ 浏览器启动"
            elif evt == "step_start":
                s, t = data.get("step", "?"), data.get("total", "?")
                act = data.get("action", "")
                summary = data.get("summary", "")
                return f"  ⏳ 步骤 {s}/{t}: {act} → {summary}"
            elif evt == "step_done":
                s = data.get("step", "?")
                return f"  ✅ 步骤 {s} 完成"
            elif evt == "step_error":
                s = data.get("step", "?")
                err = data.get("error", "")
                return f"  ❌ 步骤 {s} 失败: {err}"
            elif evt == "workflow_done":
                return f"  🏁 工作流完成: {data.get('name', '?')}"
            elif evt == "site_unreachable":
                msg = data.get("message", "")
                return f"  🌐 站点不可达: {msg}"
            elif evt == "captcha_failed":
                msg = data.get("error", "")
                shot = data.get("screenshot", "")
                return f"  🧩 验证码失败: {msg}" + (f" 截图: {shot}" if shot else "")
            elif evt == "api_fallback":
                status = data.get("status", "")
                url = data.get("url", "")
                path = data.get("path", "")
                err = data.get("error", "")
                if status == "success":
                    return f"  🔁 API降级成功: {path}"
                if status == "error":
                    return f"  🔁 API降级失败: {url} {err}"
                return f"  🔁 API降级尝试: {url}"
            elif evt == "error_pause":
                msg = data.get("message", "") or data.get("error", "")
                return f"  ⛔ 错误暂停: {msg}"
            return ""

        net_ok, net_message, net_details = ensure_dual_nic_ready(ctx)
        net_label = network_preflight_label(net_message, net_details)
        try:
            ctx.state_store.append_execution_log_detail(exec_id, f"🌐 {net_label}: {net_message}")
            for line in net_details[:10]:
                ctx.state_store.append_execution_log_detail(exec_id, f"  {line}")
        except Exception:
            pass
        if not net_ok:
            reason = (net_message or f"{net_label}失败")[:200]
            logger.error("❌ 定时任务 {} {}失败: {}", task_id, net_label, reason)
            ctx.state_store.update_execution_log(
                exec_id, status="failed", reason=reason,
                updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            try:
                await ctx.alert.send_alert(
                    AlertLevel.ERROR,
                    f"spider-gd {net_label}失败\n任务：{task_name}\n原因：{reason}\n"
                    + "\n".join(net_details[:5]),
                    event_type="error",
                )
            except Exception:
                logger.warning("双网卡预检失败告警发送异常", exc_info=True)
            return

        # 确保 tools 在 path
        import sys as _sys
        _tools = Path(__file__).resolve().parent.parent.parent / "tools"
        if str(_tools) not in _sys.path:
            _sys.path.insert(0, str(_tools))

        try:
            from task_runner import TaskRunner
        except Exception as e:
            logger.error("定时任务 {} 加载 TaskRunner 失败: {}", task_id, e)
            ctx.state_store.update_execution_log(
                exec_id, status="failed", reason=f"加载失败: {e}",
                updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            return

        # 构造 TaskRunner 需要的配置格式
        task_cfg = {
            "name": task_name,
            "login": cfg.get("login", ""),
            "indicators": cfg.get("indicators", []),
            "stop_on_error": cfg.get("stop_on_error", True),
            "start_page": cfg.get("start_page", "https://pm.gd.csg.cn/portal/#/home"),
            "backfill_days": cfg.get("backfill_days", 1),
            "cover_days": cfg.get("cover_days", 0),
            "indicator_windows": cfg.get("indicator_windows") or {},
            "output_windows": cfg.get("output_windows") or {},
            "path_template": cfg.get("path_template", ""),
            "path_folders": cfg.get("path_folders") or [],
            "path_structure": cfg.get("path_structure", ""),
            "custom_filenames": cfg.get("custom_filenames") or {},
        }

        # 使用任务配置的下载目录，否则用系统默认；任务可自定义日期分层模板
        dl_base = (
            _read_state_setting(ctx, "downloadDir")
            or cfg.get("download_dir")
            or ctx.settings.storage.data_dir
        )
        wifi_name = ""
        try:
            from spider_gd.infrastructure.network import WifiConnector

            wifi_name = WifiConnector().current_ssid()
        except Exception:
            wifi_name = ""
        if not wifi_name:
            wifi_name = str(ctx.state_store.get_setting("wifiSsid") or "")

        structure = str(cfg.get("path_structure") or "").strip()
        task_indicators = [str(item) for item in (cfg.get("indicators") or [])]
        output_dirs: dict[str, dict[str, str]] = {}
        if structure:
            from spider_gd.core.path_structure import (
                build_structure_dirs,
                resolve_structure,
                structure_indicator_scope,
                structure_warnings,
            )
            from spider_gd.core.workflow_outputs import output_specs_or_fallback

            task_indicators = structure_indicator_scope(
                task_indicators, cfg.get("_structure_indicators"),
            )
            outputs_map = {}
            for indicator in task_indicators:
                wf = ctx.state_store.get_workflow_config(indicator)
                parsed = {}
                if wf and wf.get("yaml_text"):
                    try:
                        import yaml as _yaml3
                        parsed = _yaml3.safe_load(wf["yaml_text"]) or {}
                    except Exception:
                        parsed = {}
                outputs_map[indicator] = output_specs_or_fallback(parsed, indicator)

            try:
                result = resolve_structure(
                    structure,
                    task_indicators,
                    outputs=outputs_map,
                    ssid=wifi_name,
                    when=datetime.now().date(),
                )
                dl_dir, flat_dirs = build_structure_dirs(dl_base, result)
                for canonical, directory in flat_dirs.items():
                    indicator, _, output_key = canonical.partition("::")
                    output_dirs.setdefault(indicator, {})[output_key] = directory
            except ValueError as exc:
                reason = f"任务存储结构配置错误: {exc}"
                logger.error("{}（任务 {}）", reason, task_id)
                ctx.state_store.update_execution_log(
                    exec_id, status="failed", reason=reason[:200],
                    updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                return
            task_cfg["path_folders"] = []
            structure_issues = structure_warnings(
                result, outputs_map, indicators=task_indicators,
            )
            for line in structure_issues:
                logger.warning("定时任务 {}：{}", task_id, line)
                try:
                    ctx.state_store.append_execution_log_detail(exec_id, f"⚠️ {line}")
                except Exception:
                    pass
            if structure_issues:
                try:
                    await ctx.alert.send_alert(
                        AlertLevel.WARN,
                        f"spider-gd 存储结构需要处理\n任务：{task_name}\n"
                        + "\n".join(structure_issues[:5]),
                        event_type="error",
                    )
                except Exception:
                    logger.warning("存储结构告警发送异常", exc_info=True)
        else:
            template = str(cfg.get("path_template") or "").strip()
            if template:
                from spider_gd.core.output_paths import resolve_task_output_dir

                dl_dir = str(resolve_task_output_dir(
                    dl_base, template, datetime.now().date(), ssid=wifi_name,
                ))
            else:
                today = datetime.now()
                dl_dir = str(Path(dl_base) / today.strftime("%Y-%m") / today.strftime("%Y-%m-%d"))
        Path(dl_dir).mkdir(parents=True, exist_ok=True)
        download_snapshot = _snapshot_download_files(dl_dir)

        # 构造 DB 工作流加载器（避免定时任务找不到 SQLite 中已迁移的工作流）
        def _db_wf_loader(wf_id: str):
            wf_cfg = ctx.state_store.get_workflow_config(wf_id)
            if wf_cfg and wf_cfg.get("yaml_text"):
                import yaml as _yaml2
                return _yaml2.safe_load(wf_cfg["yaml_text"]) or {}
            return None

        cert_password = (
            _read_state_setting(ctx, "certPassword")
            or ctx.settings.crawler.cert_password
            or ""
        ).strip()
        account = (
            _read_state_setting(ctx, "account")
            or ctx.settings.crawler.username
            or ""
        ).strip()
        account_password = (
            _read_state_setting(ctx, "accountPassword")
            or ctx.settings.crawler.password
            or ""
        ).strip()

        station_queue: list[dict[str, str]] = []
        if _bool_setting(_read_state_setting(ctx, "multiStationEnabled") or "", False):
            from spider_gd.core.station_picker import StationPickError, choose_task_stations
            from spider_gd.core.station_profile import parse_station_profiles
            from spider_gd.infrastructure.network.usb_devices import list_usb_devices

            try:
                saved_profiles = parse_station_profiles(
                    _read_state_setting(ctx, "stationProfiles") or ""
                )
            except ValueError as exc:
                saved_profiles = []
                logger.warning("场站设置无法读取，定时任务按原证书密码继续: {}", exc)
            try:
                usb_devices = list_usb_devices()
            except Exception as exc:
                logger.warning("读取 UKey 失败，定时任务按原证书密码继续: {}", exc)
                usb_devices = []
            plugged_ukeys = [
                (item.device_id, item.label) for item in usb_devices if item.ukey_candidate
            ]
            try:
                chosen = choose_task_stations(
                    saved_profiles,
                    plugged_ukeys,
                    fallback_cert_password=cert_password,
                )
            except StationPickError as exc:
                reason = str(exc)
                logger.error("{}（任务 {}）", reason, task_id)
                ctx.state_store.update_execution_log(
                    exec_id, status="failed", reason=reason[:200],
                    updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                return
            if chosen is None:
                reason = "已取消选择场站"
                logger.info("{}（任务 {}）", reason, task_id)
                ctx.state_store.update_execution_log(
                    exec_id, status="failed", reason=reason,
                    updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                return
            station_queue = chosen
        from task_retry import remember_run_plan

        task_cfg["_exec_id"] = exec_id
        remember_run_plan(ctx.state_store, exec_id, task_cfg)
        runner = TaskRunner(task_cfg, download_dir=dl_dir, headless=False, minimized=True,
                           workflow_loader=_db_wf_loader,
                           cert_password=cert_password,
                           account=account,
                           account_password=account_password,
                           output_dirs=output_dirs,
                           station_queue=station_queue,
                           exec_id=exec_id,
                           slot_store=ctx.state_store)

        errors: list[str] = []
        pause_alerts: set[str] = set()

        def _capture_events(data: dict):
            event = data.get("event", "")
            if event in ("error_pause", "waiting_input"):
                reason = data.get("error") or data.get("message") or "等待人工处理"
                ctx.state_store.update_execution_log(exec_id, status="paused", reason=reason,
                    updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"))
                if event == "error_pause" and reason not in pause_alerts:
                    pause_alerts.add(reason)
                    import asyncio
                    if ctx.alert is not None:
                        asyncio.create_task(ctx.alert.send_alert(AlertLevel.ERROR,
                            f"定时任务 {task_name} 已暂停：{reason}", event_type="error"))
            elif event == "resumed":
                ctx.state_store.update_execution_log(exec_id, status="running", reason="",
                    updated_at=_time.strftime("%Y-%m-%d %H:%M:%S"))
            if event in (
                "task_error",
                "step_error",
                "aborted",
                "site_unreachable",
                "captcha_failed",
            ):
                msg = data.get("error") or data.get("message", "")
                if msg:
                    errors.append(str(msg)[:200])
            if event == "task_step" and data.get("status") == "error":
                errors.append(f"[{data.get('name', '?')}] {data.get('message', '')}")
            # 写入执行日志
            try:
                line = _fmt_event(data)
                if line:
                    ctx.state_store.append_execution_log_detail(exec_id, line)
            except Exception:
                pass

        final_status = "success"
        final_reason = ""
        try:
            await runner.run(on_event=_capture_events)
            if errors:
                final_status = "failed"
                final_reason = "; ".join(errors[:5])
                logger.warning("⚠️ 定时任务 {} 部分失败: {}", task_id, final_reason)
            else:
                logger.info("✅ 定时任务完成: {}", task_id)
        except Exception as e:
            final_status = "failed"
            final_reason = str(e)[:200]
            logger.error("❌ 定时任务异常: {} - {}", task_id, str(e)[:500])
        finally:
            zip_path = None
            try:
                zip_path = await deliver_task_runner_downloads(
                    ctx,
                    exec_id=exec_id,
                    task_config_id=task_id,
                    task_name=str(task_name),
                    download_dir=dl_dir,
                    before=download_snapshot,
                    task_status=final_status,
                    reason=final_reason,
                )
                if zip_path:
                    for line in delivery_execution_log_lines(ctx, exec_id, zip_path=zip_path):
                        ctx.state_store.append_execution_log_detail(exec_id, line)
            except Exception as delivery_exc:
                logger.warning(
                    "定时任务产物发送失败但不影响任务结果：exec={} error={}",
                    exec_id,
                    delivery_exc,
                )
                ctx.state_store.append_execution_log_detail(
                    exec_id,
                    f"⚠️ 稳定发送失败，已进入补发队列: {delivery_exc}",
                )
                for line in delivery_execution_log_lines(ctx, exec_id):
                    ctx.state_store.append_execution_log_detail(exec_id, line)
            try:
                from spider_gd.gateway.data_upload_flow import maybe_upload_after_run

                await maybe_upload_after_run(
                    ctx.state_store,
                    profile_id=str(cfg.get("upload_profile_id") or ""),
                    auto_upload=bool(cfg.get("auto_upload")),
                    exec_id=exec_id,
                    download_dir=str(dl_dir),
                    before=download_snapshot,
                    interface_name=str(ctx.state_store.get_setting("wifiInterfaceName") or ""),
                )
            except Exception as upload_exc:
                logger.warning("定时任务自动上传失败但不影响任务结果：{}", upload_exc)
                ctx.state_store.append_execution_log_detail(
                    exec_id,
                    f"☁️ 上传异常: {upload_exc}",
                )
            if final_status == "failed" and not zip_path:
                try:
                    alert = getattr(ctx, "alert", None)
                    send_alert = getattr(alert, "send_alert", None)
                    if callable(send_alert):
                        now_alert = _time.strftime("%Y-%m-%d %H:%M:%S")
                        await send_alert(
                            AlertLevel.ERROR,
                            "## ⚠️ spider-gd 定时任务失败\n\n"
                            f"- 任务: {task_name}\n"
                            f"- 任务ID: {task_id}\n"
                            f"- 执行ID: {exec_id}\n"
                            f"- 错误: {(final_reason or '任务执行失败')[:800]}\n"
                            f"- 时间: {now_alert}",
                            event_type="task_failed",
                        )
                except Exception as alert_exc:
                    logger.warning("定时任务失败告警发送异常：exec={} error={}", exec_id, alert_exc)
            now_ts2 = _time.strftime("%Y-%m-%d %H:%M:%S")
            ctx.state_store.update_execution_log(
                exec_id, status=final_status, reason=final_reason, updated_at=now_ts2,
            )

    return _handler


def _load_task_config_from_yaml(task_name: str) -> dict | None:
    """从 YAML 文件加载单个任务配置（兼容旧数据）。"""
    import yaml as _yaml

    from spider_gd.config.user_home import tasks_dir

    yaml_path = tasks_dir() / f"{task_name}.yml"
    if not yaml_path.exists():
        return None
    try:
        cfg = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        cfg["id"] = task_name
        cfg["enabled"] = cfg.get("enabled", True)
        cfg["repeat"] = cfg.get("repeat", "daily")
        cfg["time"] = cfg.get("time", "09:00")
        return cfg
    except Exception:
        return None


def _load_task_configs(ctx: AppContext) -> list[dict]:
    """从 SQLite 读取所有启用定时调度的任务配置。"""
    try:
        all_configs = ctx.state_store.list_task_configs()
    except Exception:
        logger.warning("无法从 SQLite 读取任务配置，回退到 YAML")
        return _load_task_configs_from_yaml()

    enabled = [c for c in all_configs if c.get("enabled") and c.get("repeat") and c.get("time")]
    return enabled


def _load_task_configs_from_yaml(tasks_directory: str = "") -> list[dict]:
    """从 YAML 文件读取任务配置（兼容旧数据）。"""
    import yaml as _yaml

    from spider_gd.config.user_home import tasks_dir

    dir_path = Path(tasks_directory) if tasks_directory else tasks_dir()
    if not dir_path.is_absolute():
        dir_path = Path.cwd() / dir_path
    if not dir_path.exists():
        return []

    tasks = []
    for f in sorted(dir_path.glob("*.yml")):
        try:
            cfg = _yaml.safe_load(f.read_text(encoding="utf-8"))
            if cfg.get("enabled") and cfg.get("repeat") and cfg.get("time"):
                tasks.append(cfg)
        except Exception:
            logger.warning("无法解析任务文件: {}", f)
    return tasks


def _repeat_to_cron(task: dict) -> str | None:
    """把任务的 repeat/time/weekdays 转换为 cron 表达式。"""
    import re as _re

    repeat = task.get("repeat", "")
    time_str = task.get("time", "")
    if not repeat or not time_str:
        return None

    match = _re.match(r"(\d{1,2}):(\d{2})", time_str)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    minute = max(0, min(59, minute))
    hour = max(0, min(23, hour))

    if repeat == "daily":
        return f"{minute} {hour} * * *"

    if repeat == "weekly":
        weekdays = task.get("weekdays", [])
        if not weekdays:
            return f"{minute} {hour} * * *"  # fallback to daily
        days = ",".join(str(d) for d in sorted(weekdays))
        return f"{minute} {hour} * * {days}"

    return None


def _default_backfill_job(ctx: AppContext):
    """构造补漏 job handler：检测缺口 → 防雪崩限额 → 逐日回补（低优先级）。"""
    import time as _time

    from spider_gd.core.scheduler.backfill import BackfillPolicy, plan_backfill

    policy = BackfillPolicy(
        max_items_per_run=ctx.settings.schedule.backfill_max_items,
        min_interval_sec=ctx.settings.schedule.backfill_min_interval_sec,
    )
    last_run: dict[str, float] = {}

    async def _handler(_payload: dict[str, object]) -> None:
        from datetime import timedelta

        end = _yesterday()
        start = (
            datetime.strptime(end, "%Y-%m-%d")
            - timedelta(days=ctx.settings.schedule.backfill_lookback_days)
        ).strftime("%Y-%m-%d")

        to_backfill: dict[str, list[str]] = {}
        for ep in ctx.endpoints:
            present = ctx.state_store.get_success_dates(ep.name)
            plan = plan_backfill(
                indicator=ep.name,
                present_dates=present,
                start=start,
                end=end,
                policy=policy,
                last_run_ts=last_run.get(ep.name),
                now_ts=_time.time(),
            )
            if plan.dates:
                to_backfill[ep.name] = plan.dates
                last_run[ep.name] = _time.time()

        if not to_backfill:
            logger.info("补漏检查：无缺口需回补")
            return

        # 逐缺口日期回补（每个 endpoint 仅补自己的缺口）
        dates = sorted({d for ds in to_backfill.values() for d in ds})
        orchestrator, runner = build_orchestrator(ctx)
        try:
            for d in dates:
                eps = [ep for ep in ctx.endpoints if d in to_backfill.get(ep.name, [])]
                result = await orchestrator.run(  # type: ignore[attr-defined]
                    task_name=f"补漏_{ctx.settings.schedule.task_name}",
                    endpoints=eps,
                    target_date=d,
                )
                if result.get("zip") is None:
                    logger.info(
                        "补漏未生成压缩包：date={} task={} status={}",
                        d,
                        result.get("task_id"),
                        result.get("status"),
                    )
        finally:
            await runner.close()  # type: ignore[attr-defined]

    return _handler


def _default_retention_job(ctx: AppContext):
    """构造保留期清理 job handler：清理过期数据 + 日志。"""
    from spider_gd.config.logging import cleanup_task_logs
    from spider_gd.infrastructure.storage.retention import cleanup_old_data

    async def _handler(_payload: dict[str, object]) -> None:
        cleanup_old_data(
            ctx.settings.storage.data_dir,
            retention_days=ctx.settings.schedule.retention_days,
        )
        cleanup_task_logs(
            ctx.settings.log.file_dir,
            retention_days=ctx.settings.schedule.retention_days,
        )

    return _handler


def _cleanup_execution_logs_job(ctx: AppContext):
    """构造执行日志清理 job handler：每天清理 30 天前的日志。"""

    async def _handler(_payload: dict[str, object]) -> None:
        deleted = ctx.state_store.cleanup_old_execution_logs(retention_days=30)
        if deleted > 0:
            logger.info("已清理 {} 条过期执行日志", deleted)

    return _handler


def _alert_outbox_retry_job(ctx: AppContext):
    """构造告警 outbox 补发 job handler。"""

    async def _handler(_payload: dict[str, object]) -> None:
        retry = getattr(ctx.alert, "retry_outbox", None)
        if not callable(retry):
            return
        try:
            sent = await retry()
            if sent:
                logger.info("告警 outbox 已补发 {} 条", sent)
        except Exception as exc:
            logger.warning("告警 outbox 补发失败: {}", exc)

    return _handler


# 调度器对象 → 上次成功注册时的任务指纹。同一进程里连着保存相同调度时不再拆装。
_TASK_SCHEDULE_SIGNATURES: dict[int, tuple] = {}


def _task_schedule_signature(tasks: list[dict]) -> tuple:
    """把会影响 cron 的字段收成可比较的指纹。

    Args:
        tasks: 已过滤出的启用任务。顺序不影响结果。

    Returns:
        tuple: 排序后的 ``(id, 名称, cron)``。

    Examples:
        >>> _task_schedule_signature([
        ...     {"id": "b", "name": "乙", "repeat": "daily", "time": "08:00"},
        ...     {"id": "a", "name": "甲", "repeat": "daily", "time": "09:30"},
        ... ])
        (('a', '甲', '30 9 * * *'), ('b', '乙', '0 8 * * *'))
    """
    rows = []
    for task in tasks:
        task_id = str(task.get("id") or task.get("name") or "")
        rows.append((
            task_id,
            str(task.get("name") or task_id),
            _repeat_to_cron(task) or "",
        ))
    return tuple(sorted(rows))


def _remember_task_schedule(ctx: AppContext, tasks: list[dict]) -> None:
    """记下这次注册对应的调度指纹。

    Args:
        ctx: 提供调度器对象，用来区分不同进程内的调度器。
        tasks: 刚刚注册进去的启用任务。

    Returns:
        None: 指纹写在模块级表里。

    Examples:
        >>> _task_schedule_signature([])
        ()
    """
    _TASK_SCHEDULE_SIGNATURES[id(ctx.scheduler)] = _task_schedule_signature(tasks)


async def reload_task_jobs_if_changed(ctx: AppContext) -> bool:
    """任务表没改到调度时，跳过拆除并重新注册。

    保存界面会把每一条任务各 PUT 一次，每次都拆掉全部 ``task_*`` 再注册。
    指纹相同就直接返回，避免同一秒里把调度器拆装多遍。

    Args:
        ctx: 应用上下文。

    Returns:
        bool: 真的重新注册了为 True。

    Examples:
        >>> _task_schedule_signature([
        ...     {"id": "a", "name": "甲", "repeat": "daily", "time": "8:05"},
        ... ])
        (('a', '甲', '5 8 * * *'),)
    """
    tasks = _load_task_configs(ctx)
    signature = _task_schedule_signature(tasks)
    key = id(ctx.scheduler)
    if _TASK_SCHEDULE_SIGNATURES.get(key) == signature:
        logger.info("定时任务调度未变化，跳过重复注册")
        return False
    existing = await ctx.scheduler.list_jobs()
    for job in existing:
        if str(getattr(job, "job_id", "")).startswith("task_"):
            await ctx.scheduler.remove_job(job.job_id)
    await register_jobs(ctx)
    _TASK_SCHEDULE_SIGNATURES[key] = signature
    return True


async def register_jobs(
    ctx: AppContext,
    *,
    crawl_handler=None,
    backfill_handler=None,
    retention_handler=None,
) -> list[str]:
    """注册所有调度任务：YAML crawl_cron + tasks/ 目录下的启用任务 + 补漏 + 清理。"""
    from spider_gd.interfaces.scheduler import JobSpec

    crawl_handler = crawl_handler or _default_crawl_job(ctx)
    backfill_handler = backfill_handler or _default_backfill_job(ctx)
    retention_handler = retention_handler or _default_retention_job(ctx)

    job_ids: list[str] = []

    # 1. 从 SQLite 注册定时任务
    task_configs = _load_task_configs(ctx)
    if task_configs:
        logger.info("扫描到 {} 个启用的定时任务", len(task_configs))
    for task in task_configs:
        cron = _repeat_to_cron(task)
        if not cron:
            logger.warning("任务 {} 无法生成 cron，跳过", task.get("name", "?"))
            continue
        task_id = task.get("id", task.get("name", "?"))
        job_id = f"task_{task_id}"
        spec = JobSpec(
            job_id=job_id,
            cron=cron,
            handler_name=f"task:{task.get('name', task_id)}",
            payload={"task_id": task_id},
        )
        handler = _task_runner_job(ctx, task_id)
        job_ids.append(await ctx.scheduler.add_job(spec, handler))
        logger.info("  注册: {} → cron={}", task.get("name", task_id), cron)

    # 2. YAML 配置的 crawl_cron
    for i, cron in enumerate(ctx.settings.schedule.crawl_cron):
        spec = JobSpec(
            job_id=f"crawl_daily_{i}",
            cron=cron,
            handler_name="crawl",
            payload={"task_name": ctx.settings.schedule.task_name},
        )
        job_ids.append(await ctx.scheduler.add_job(spec, crawl_handler))

    if ctx.settings.schedule.backfill_cron:
        spec = JobSpec(
            job_id="backfill",
            cron=ctx.settings.schedule.backfill_cron,
            handler_name="backfill",
        )
        job_ids.append(await ctx.scheduler.add_job(spec, backfill_handler))

    if ctx.settings.schedule.retention_cron:
        spec = JobSpec(
            job_id="retention",
            cron=ctx.settings.schedule.retention_cron,
            handler_name="retention",
        )
        job_ids.append(await ctx.scheduler.add_job(spec, retention_handler))

    # 执行日志清理：每天凌晨 3:00
    cleanup_logs_handler = _cleanup_execution_logs_job(ctx)
    spec = JobSpec(
        job_id="cleanup_exec_logs",
        cron="0 3 * * *",
        handler_name="cleanup_execution_logs",
    )
    job_ids.append(await ctx.scheduler.add_job(spec, cleanup_logs_handler))

    # 任务产物发送补偿：始终注册。运行时若尚未启用/配置 delivery 会安静跳过；
    # 用户在设置页开启后，无需重启也能进入自动补发。
    delivery_retry_handler = _delivery_retry_job(ctx)
    spec = JobSpec(
        job_id="delivery_outbox_retry",
        cron="*/5 * * * *",
        handler_name="delivery_outbox_retry",
    )
    job_ids.append(await ctx.scheduler.add_job(spec, delivery_retry_handler))

    # 告警补偿：Server酱/企业微信等公网通道失败后写入 alert_outbox.jsonl，
    # 网络恢复时无需等待下一次告警，调度器会主动补发。
    alert_retry_handler = _alert_outbox_retry_job(ctx)
    spec = JobSpec(
        job_id="alert_outbox_retry",
        cron="*/5 * * * *",
        handler_name="alert_outbox_retry",
    )
    job_ids.append(await ctx.scheduler.add_job(spec, alert_retry_handler))

    logger.info("已注册调度任务：{}", job_ids)
    _remember_task_schedule(ctx, task_configs)
    return job_ids


async def _serve_web(ctx: AppContext) -> None:
    """在当前事件循环内启动 REST/Web UI 服务（uvicorn）。"""
    import socket

    import uvicorn

    from spider_gd.gateway.api.server import create_app

    # 端口预检：旧后端还占着端口时，uvicorn 的绑定失败不会进 app.log，
    # 现场看起来“启动成功”，实际仍是旧进程在跑旧代码。
    host, port = ctx.settings.web.host, int(ctx.settings.web.port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        logger.error(
            "控制接口 {}:{} 已被占用：{}。已有后端实例在运行，本次启动不会生效；"
            "请先结束旧后端（或整个应用）再启动。",
            host,
            port,
            exc,
        )
        raise SystemExit(1) from exc
    finally:
        probe.close()

    app = create_app(
        ctx.control,  # type: ignore[arg-type]
        enable_web_ui=ctx.settings.modules.web_ui,
        enable_mcp=ctx.settings.web.enable_mcp,
    )
    config = uvicorn.Config(
        app, host=ctx.settings.web.host, port=ctx.settings.web.port, log_level="info"
    )
    server = uvicorn.Server(config)
    logger.info("控制接口/Web UI 启动于 http://{}:{}", ctx.settings.web.host, ctx.settings.web.port)
    await server.serve()


def run_app(settings: AppSettings) -> int:
    """CLI ``run`` 入口：装配、注册调度任务并启动调度器常驻（可选 Web UI）。"""
    ctx = assemble(settings)

    async def _main() -> None:
        try:
            retry_result = await retry_pending_deliveries(
                ctx,
                include_recoverable_failed=True,
            )
            if int(retry_result.get("retried", 0)):
                logger.info("启动时已处理待补发任务产物：{}", retry_result)
        except Exception as exc:
            logger.warning("启动时处理待补发任务产物失败：{}", exc)

        if settings.modules.scheduler:
            await register_jobs(ctx)
        await ctx.scheduler.start()

        # 首次启动：YAML 迁移到 SQLite（已废弃：出厂不预置数据，用户自行导入）
        # n = ctx.state_store.migrate_workflows_from_yaml()
        # if n:
        #     logger.info("已从 YAML 迁移 {} 个工作流到 SQLite", n)

        tasks = []
        if settings.modules.web_ui:
            tasks.append(asyncio.create_task(_serve_web(ctx)))

        logger.info("系统已就绪，等待调度任务……（Ctrl+C 退出）")
        if tasks:
            await asyncio.gather(*tasks)
        else:
            await asyncio.Event().wait()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("收到中断，退出")
    finally:
        ctx.state_store.close()
    return 0
