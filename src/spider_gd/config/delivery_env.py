"""Helpers for loading stable-delivery credentials from env files."""

from __future__ import annotations

from pathlib import Path


class DeliveryEnvFileError(RuntimeError):
    """Raised when a delivery env file cannot be read."""


DELIVERY_ENV_KEYS: tuple[str, ...] = (
    "SPIDER_GD_DELIVERY_BACKEND",
    "SPIDER_GD_WECOM_WEBHOOK",
    "SPIDER_GD_SERVERCHAN_SEND_KEY",
    "SPIDER_GD_SERVERCHAN_CHANNEL",
    "SPIDER_GD_SERVERCHAN_OPENID",
    "SPIDER_GD_PUSHPLUS_TOKEN",
    "SPIDER_GD_PUSHPLUS_CHANNEL",
    "SPIDER_GD_DESKTOP_WECHAT_TARGET",
    "SPIDER_GD_DESKTOP_WECHAT_BACKEND",
    "SPIDER_GD_DESKTOP_WECHAT_SELECT_MODE",
    "SPIDER_GD_DESKTOP_WECHAT_PROFILE_PATH",
    "SPIDER_GD_DESKTOP_WECHAT_APP_NAME",
    "SPIDER_GD_DESKTOP_WECHAT_PAUSE_SEC",
    "SPIDER_GD_DESKTOP_WECHAT_HASH_THRESHOLD",
    "SPIDER_GD_DESKTOP_WECHAT_ALLOW_UNSTABLE_SEARCH",
    "SPIDER_GD_FILE_DELIVERY",
    "SPIDER_GD_REMOTE_UPLOAD_URL",
    "SPIDER_GD_REMOTE_UPLOAD_TOKEN",
    "SPIDER_GD_REMOTE_UPLOAD_RELATIVE_PATH",
)


def read_delivery_env_file(path_text: str) -> dict[str, str]:
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise DeliveryEnvFileError(f"env 文件不存在: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DeliveryEnvFileError(f"env 文件无法读取: {path}: {exc}") from exc
    except UnicodeError as exc:
        raise DeliveryEnvFileError(f"env 文件必须是 UTF-8 文本: {path}: {exc}") from exc

    values: dict[str, str] = {}
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[len("export "):].strip()
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def delivery_env_values_to_overrides(values: dict[str, str]) -> dict[str, str]:
    overrides: dict[str, str] = {}

    backend = values.get("SPIDER_GD_DELIVERY_BACKEND", "").strip().replace("-", "_")
    webhook = values.get("SPIDER_GD_WECOM_WEBHOOK", "").strip()
    serverchan_send_key = values.get("SPIDER_GD_SERVERCHAN_SEND_KEY", "").strip()
    pushplus_token = values.get("SPIDER_GD_PUSHPLUS_TOKEN", "").strip()
    desktop_wechat_target = values.get("SPIDER_GD_DESKTOP_WECHAT_TARGET", "").strip()
    if not backend:
        if serverchan_send_key:
            backend = "serverchan"
        elif pushplus_token:
            backend = "pushplus"
        elif desktop_wechat_target:
            backend = "desktop_wechat"
        elif webhook:
            backend = "wecom_webhook"
    if backend:
        overrides["deliveryEnabled"] = "true"
        overrides["deliveryBackend"] = backend
    if webhook:
        overrides["deliveryWecomWebhook"] = webhook
    if serverchan_send_key:
        overrides["deliveryServerchanSendKey"] = serverchan_send_key
    if channel := values.get("SPIDER_GD_SERVERCHAN_CHANNEL", "").strip():
        overrides["deliveryServerchanChannel"] = channel
    if openid := values.get("SPIDER_GD_SERVERCHAN_OPENID", "").strip():
        overrides["deliveryServerchanOpenid"] = openid
    if pushplus_token:
        overrides["deliveryPushplusToken"] = pushplus_token
    if channel := values.get("SPIDER_GD_PUSHPLUS_CHANNEL", "").strip():
        overrides["deliveryPushplusChannel"] = channel
    if desktop_wechat_target:
        overrides["deliveryDesktopWechatTarget"] = desktop_wechat_target
    if backend_name := values.get("SPIDER_GD_DESKTOP_WECHAT_BACKEND", "").strip():
        overrides["deliveryDesktopWechatBackend"] = backend_name
    if select_mode := values.get("SPIDER_GD_DESKTOP_WECHAT_SELECT_MODE", "").strip():
        overrides["deliveryDesktopWechatSelectMode"] = select_mode
    if profile_path := values.get("SPIDER_GD_DESKTOP_WECHAT_PROFILE_PATH", "").strip():
        overrides["deliveryDesktopWechatProfilePath"] = profile_path
    if app_name := values.get("SPIDER_GD_DESKTOP_WECHAT_APP_NAME", "").strip():
        overrides["deliveryDesktopWechatAppName"] = app_name
    if pause_sec := values.get("SPIDER_GD_DESKTOP_WECHAT_PAUSE_SEC", "").strip():
        overrides["deliveryDesktopWechatPauseSec"] = pause_sec
    if hash_threshold := values.get("SPIDER_GD_DESKTOP_WECHAT_HASH_THRESHOLD", "").strip():
        overrides["deliveryDesktopWechatHashThreshold"] = hash_threshold
    if allow_unstable := values.get("SPIDER_GD_DESKTOP_WECHAT_ALLOW_UNSTABLE_SEARCH", "").strip():
        overrides["deliveryDesktopWechatAllowUnstableSearch"] = allow_unstable
    if file_delivery := values.get("SPIDER_GD_FILE_DELIVERY", "").strip():
        overrides["deliveryFileDelivery"] = file_delivery
    if url := values.get("SPIDER_GD_REMOTE_UPLOAD_URL", "").strip():
        overrides["deliveryRemoteUploadUrl"] = url
    if token := values.get("SPIDER_GD_REMOTE_UPLOAD_TOKEN", "").strip():
        overrides["deliveryRemoteUploadToken"] = token
    if relative_path := values.get("SPIDER_GD_REMOTE_UPLOAD_RELATIVE_PATH", "").strip():
        overrides["deliveryRemoteUploadRelativePath"] = relative_path
    return overrides


def read_delivery_env_file_overrides(path_text: str) -> dict[str, str]:
    return delivery_env_values_to_overrides(read_delivery_env_file(path_text))
