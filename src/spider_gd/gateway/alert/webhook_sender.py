"""Webhook 发送（钉钉群机器人 / 企业微信机器人 / Server酱 / PushPlus）。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from loguru import logger

# ============================================================
#  钉钉群机器人（HMAC-SHA256 加签）
# ============================================================

def build_dingtalk_signed_url(webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
    """根据 secret 计算钉钉加签 URL（timestamp + sign）。"""
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={sign}"


async def send_dingtalk(
    webhook: str,
    message: str,
    *,
    secret: str = "",
    title: str = "spider-gd 告警",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """向钉钉群机器人发送 Markdown 消息，返回是否成功。"""
    url = build_dingtalk_signed_url(webhook, secret) if secret else webhook
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": message},
    }
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        resp = await client.post(url, json=payload)
        ok = resp.status_code == 200 and resp.json().get("errcode", 0) == 0
        if not ok:
            logger.warning("钉钉发送失败：HTTP {} {}", resp.status_code, resp.text[:200])
        return ok
    except Exception as exc:
        logger.warning("钉钉发送异常：{}", exc)
        return False
    finally:
        if owns:
            await client.aclose()


# ============================================================
#  企业微信机器人 Webhook
# ============================================================

def _attempt_count(retry_attempts: int) -> int:
    return max(1, int(retry_attempts))


async def _sleep_before_retry(attempt: int, retry_attempts: int, retry_delay_sec: float) -> None:
    if attempt >= retry_attempts or retry_delay_sec <= 0:
        return
    await asyncio.sleep(retry_delay_sec)


def _response_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        return {"raw": resp.text}
    return data if isinstance(data, dict) else {"raw": data}


def _code_matches(value: object, success_codes: set[int]) -> bool:
    if isinstance(value, bool):
        return int(value) in success_codes
    if isinstance(value, int):
        return value in success_codes
    if isinstance(value, float):
        return value.is_integer() and int(value) in success_codes
    if isinstance(value, str):
        try:
            return int(value.strip()) in success_codes
        except ValueError:
            return False
    return False


def _response_code_ok(
    resp: httpx.Response,
    data: dict[str, Any],
    key: str,
    success_codes: set[int],
    *,
    default: object | None = None,
) -> bool:
    if resp.status_code < 200 or resp.status_code >= 300:
        return False
    return _code_matches(data.get(key, default), success_codes)


async def send_wecom(
    webhook: str,
    message: str,
    *,
    title: str = "spider-gd 告警",
    client: httpx.AsyncClient | None = None,
    retry_attempts: int = 2,
    retry_delay_sec: float = 0.5,
) -> bool:
    """向企业微信群机器人发送 Markdown 消息。

    webhook 格式: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    企业微信机器人无需加签（也可可选加签，当前未实现）。
    """
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"## {title}\n{message}"},
    }
    attempts = _attempt_count(retry_attempts)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.post(webhook, json=payload)
                data = _response_json(resp)
                ok = _response_code_ok(resp, data, "errcode", {0}, default=0)
                if ok:
                    return True
                logger.warning(
                    "企微发送失败：HTTP {} {} (attempt {}/{})",
                    resp.status_code,
                    str(data)[:200],
                    attempt,
                    attempts,
                )
            except Exception as exc:
                logger.warning("企微发送异常：{} (attempt {}/{})", exc, attempt, attempts)
            await _sleep_before_retry(attempt, attempts, retry_delay_sec)
        return False
    finally:
        if owns:
            await client.aclose()


def _wecom_robot_key(webhook: str) -> str:
    key = parse_qs(urlsplit(webhook).query).get("key", [""])[0]
    if not key:
        raise ValueError("企业微信机器人 webhook 缺少 key 参数")
    return key


def _wecom_robot_upload_url(webhook: str) -> str:
    parsed = urlsplit(webhook)
    base = urlunsplit((parsed.scheme, parsed.netloc, "/cgi-bin/webhook/upload_media", "", ""))
    return f"{base}?key={_wecom_robot_key(webhook)}&type=file"


async def upload_wecom_robot_file(
    webhook: str,
    file_path: str | Path,
    *,
    client: httpx.AsyncClient | None = None,
    retry_attempts: int = 2,
    retry_delay_sec: float = 0.5,
) -> str:
    """上传企业微信机器人文件素材并返回 media_id。"""
    path = Path(file_path)
    size = path.stat().st_size
    if size <= 5:
        raise ValueError("企业微信机器人文件必须大于 5 字节")
    if size > 20 * 1024 * 1024:
        raise ValueError("企业微信机器人普通文件大小不能超过 20MB")

    attempts = _attempt_count(retry_attempts)
    last_error = "企业微信机器人上传文件失败"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        for attempt in range(1, attempts + 1):
            try:
                with path.open("rb") as fp:
                    files = {"media": (path.name, fp, "application/octet-stream")}
                    resp = await client.post(_wecom_robot_upload_url(webhook), files=files)
                data = _response_json(resp)
                media_id = data.get("media_id", "")
                if _response_code_ok(resp, data, "errcode", {0}, default=0) and media_id:
                    return str(media_id)
                last_error = f"企业微信机器人上传文件失败: HTTP {resp.status_code} {data}"
                logger.warning("{} (attempt {}/{})", last_error, attempt, attempts)
            except Exception as exc:
                last_error = f"企业微信机器人上传文件异常: {exc}"
                logger.warning("{} (attempt {}/{})", last_error, attempt, attempts)
            await _sleep_before_retry(attempt, attempts, retry_delay_sec)
        raise RuntimeError(last_error)
    finally:
        if owns:
            await client.aclose()


async def send_wecom_file(
    webhook: str,
    file_path: str | Path,
    *,
    client: httpx.AsyncClient | None = None,
    retry_attempts: int = 2,
    retry_delay_sec: float = 0.5,
) -> bool:
    """向企业微信群机器人发送文件消息。"""
    attempts = _attempt_count(retry_attempts)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        media_id = await upload_wecom_robot_file(
            webhook,
            file_path,
            client=client,
            retry_attempts=retry_attempts,
            retry_delay_sec=retry_delay_sec,
        )
        payload = {"msgtype": "file", "file": {"media_id": media_id}}
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.post(webhook, json=payload)
                data = _response_json(resp)
                ok = _response_code_ok(resp, data, "errcode", {0}, default=0)
                if ok:
                    return True
                logger.warning(
                    "企微文件发送失败：HTTP {} {} (attempt {}/{})",
                    resp.status_code,
                    data,
                    attempt,
                    attempts,
                )
            except Exception as exc:
                logger.warning("企微文件发送异常：{} (attempt {}/{})", exc, attempt, attempts)
            await _sleep_before_retry(attempt, attempts, retry_delay_sec)
        return False
    except Exception as exc:
        logger.warning("企微文件发送异常：{}", exc)
        return False
    finally:
        if owns:
            await client.aclose()


# ============================================================
#  Server酱 (ServerChan) —— 推送到个人微信
# ============================================================

async def send_serverchan(
    send_key: str,
    message: str,
    *,
    title: str = "spider-gd 告警",
    channel: str = "",
    openid: str = "",
    client: httpx.AsyncClient | None = None,
    retry_attempts: int = 2,
    retry_delay_sec: float = 0.5,
) -> bool:
    """通过 Server酱 推送到个人微信。

    免费版每日 5 条，Turbo 版需充值。
    send_key 在 https://sct.ftqq.com/ 注册获取。

    channel 可选，指定本次推送使用的消息通道：
        9   = 方糖服务号（默认，推送到个人微信）
        66  = 企业微信应用消息
        1   = 企业微信群机器人
        2   = 钉钉群机器人
        3   = 飞书群机器人
        8   = Bark iOS
        18  = PushDeer
        98  = 官方Android版
        0   = 测试号
        多个通道用 | 分隔，如 "9|66"
    openid 可选，消息抄送的 openid（仅测试号和企业微信应用通道有效），
        多个用 , 分隔。
    """
    attempts = _attempt_count(retry_attempts)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        data: dict = {"title": title, "desp": message}
        if channel:
            data["channel"] = channel
        if openid:
            data["openid"] = openid
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.post(
                    f"https://sctapi.ftqq.com/{send_key}.send",
                    data=data,
                )
                result = _response_json(resp)
                ok = _response_code_ok(resp, result, "code", {0})
                if ok:
                    return True
                logger.warning("Server酱发送失败：{} (attempt {}/{})", result, attempt, attempts)
            except Exception as exc:
                logger.warning("Server酱发送异常：{} (attempt {}/{})", exc, attempt, attempts)
            await _sleep_before_retry(attempt, attempts, retry_delay_sec)
        return False
    finally:
        if owns:
            await client.aclose()


# ============================================================
#  PushPlus —— 多渠道推送
# ============================================================

async def send_pushplus(
    token: str,
    message: str,
    *,
    title: str = "spider-gd 告警",
    template: str = "markdown",
    channel: str = "",
    client: httpx.AsyncClient | None = None,
    retry_attempts: int = 2,
    retry_delay_sec: float = 0.5,
) -> bool:
    """通过 PushPlus 推送消息。

    免费版每日 200 条，支持微信/企微/钉钉/飞书/邮件/短信等。
    token 在 https://pushplus.plus/ 注册获取。

    channel 可选，指定推送渠道：
        wechat = 微信公众号（默认）
        webhook = 企业微信/钉钉/飞书 webhook
        sms = 短信（需充值积分）
        mail = 邮件
    """
    attempts = _attempt_count(retry_attempts)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        body: dict = {"token": token, "title": title, "content": message, "template": template}
        if channel:
            body["channel"] = channel
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.post(
                    "https://www.pushplus.plus/send",
                    json=body,
                )
                data = _response_json(resp)
                ok = _response_code_ok(resp, data, "code", {200})
                if ok:
                    return True
                logger.warning("PushPlus发送失败：{} (attempt {}/{})", data, attempt, attempts)
            except Exception as exc:
                logger.warning("PushPlus发送异常：{} (attempt {}/{})", exc, attempt, attempts)
            await _sleep_before_retry(attempt, attempts, retry_delay_sec)
        return False
    finally:
        if owns:
            await client.aclose()


# ============================================================
#  微信模板消息（直连微信 API，不走 Server酱）
# ============================================================

# access_token 缓存
_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}


async def _get_wechat_access_token(appid: str, appsecret: str) -> str:
    """获取微信 access_token，自动缓存到过期前 5 分钟。"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 300:
        return _token_cache["token"]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": appid,
                "secret": appsecret,
            },
        )
        data = resp.json()
        token = data.get("access_token", "")
        expires_in = data.get("expires_in", 7200)
        if not token:
            logger.error("获取微信 access_token 失败: {}", data)
            raise RuntimeError(f"access_token 获取失败: {data.get('errmsg', '')}")

        _token_cache["token"] = token
        _token_cache["expires_at"] = now + expires_in
        logger.info("微信 access_token 已刷新，有效期 {}s", expires_in)
        return token


async def send_wechat_template(
    appid: str,
    appsecret: str,
    template_id: str,
    openids: list[str],
    data: dict[str, str],
    *,
    url: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """通过微信模板消息推送到多个用户。

    appid / appsecret: 微信测试号或公众号凭证
    template_id: 模板消息 ID
    openids: 接收人 openid 列表
    data: 模板字段 dict，key 对应模板中的 {{key.DATA}}
    """
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        access_token = await _get_wechat_access_token(appid, appsecret)
        wx_data = {k: {"value": v} for k, v in data.items()}
        body: dict[str, Any] = {
            "touser": "",
            "template_id": template_id,
            "url": url,
            "data": wx_data,
        }

        ok_all = True
        for oid in openids:
            oid = oid.strip()
            if not oid:
                continue
            body["touser"] = oid
            try:
                resp = await client.post(
                    "https://api.weixin.qq.com/cgi-bin/message/template/send",
                    params={"access_token": access_token},
                    json=body,
                )
                data = resp.json()
                if data.get("errcode") != 0:
                    logger.warning("微信模板消息发送失败 openid={}: {}", oid[:10], data)
                    ok_all = False
                else:
                    logger.debug("微信模板消息已发送 openid={}", oid[:10])
            except Exception as exc:
                logger.warning("微信模板消息异常 openid={}: {}", oid[:10], exc)
                ok_all = False

        return ok_all
    except Exception as exc:
        logger.warning("微信模板消息发送异常: {}", exc)
        return False
    finally:
        if owns:
            await client.aclose()
