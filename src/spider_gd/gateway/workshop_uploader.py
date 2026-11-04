"""文件远程上传能力。

当前复用现场已有的广东数据服务器上传协议：
``POST {base}/guangdong/data/upload?relativePath=/upload/publicData``，multipart 字段名为
``file``，可选 ``Authorization: Bearer <token>``。默认上传目录为
``/upload/publicData``，录制工坊 ZIP 使用 ``/upload/workflowRawData``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from loguru import logger


DEFAULT_UPLOAD_RELATIVE_PATH = "/upload/publicData"
WORKSHOP_UPLOAD_RELATIVE_PATH = "/upload/workflowRawData"


@dataclass
class UploadResult:
    ok: bool
    uploaded: bool = False
    local_only: bool = False
    remote_url: str = ""
    remote_url_verified: bool = False
    verification_message: str = ""
    message: str = ""
    response: dict[str, Any] | None = None


def _normalize_bearer_token(upload_token: str) -> str:
    token = (upload_token or "").strip()
    if not token:
        return ""
    if token.lower().startswith("bearer "):
        return token
    return f"Bearer {token}"


def _build_upload_url(upload_base: str, relative_path: str) -> str:
    path = quote(relative_path or DEFAULT_UPLOAD_RELATIVE_PATH, safe="/")
    return f"{upload_base.rstrip('/')}/guangdong/data/upload?relativePath={path}"


def _guess_remote_url(upload_base: str, relative_path: str, file_name: str) -> str:
    rel = relative_path.strip() or DEFAULT_UPLOAD_RELATIVE_PATH
    if not rel.startswith("/"):
        rel = "/" + rel
    public_path = rel if Path(rel).suffix else f"{rel.rstrip('/')}/{file_name}"
    return _join_upload_base(upload_base, public_path)


def _encode_share_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return quote(url, safe="/%")
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe="/%"),
            quote(parts.query, safe="=&%/:?+,@"),
            quote(parts.fragment, safe="=&%/:?+,@"),
        )
    )


def _join_upload_base(upload_base: str, path: str) -> str:
    text = path.strip()
    if not text.startswith("/"):
        text = "/" + text
    return _encode_share_url(f"{upload_base.rstrip('/')}{text}")


def _coerce_remote_url(value: object, upload_base: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if text.startswith(("http://", "https://")):
        return _encode_share_url(text)
    if text.startswith("/"):
        return _join_upload_base(upload_base, text)
    if _looks_like_relative_url(text):
        return _join_upload_base(upload_base, text)
    return ""


def _looks_like_relative_url(text: str) -> bool:
    if not text or len(text) > 2048:
        return False
    if any(ch in text for ch in ('\n', '\r', '\t', '<', '>', '"', "'", "{", "}")):
        return False
    if text.startswith((".", "#", "?")):
        return False
    return "/" in text or bool(Path(text).suffix)


def _extract_remote_url(data: object, upload_base: str) -> str:
    if isinstance(data, dict):
        preferred_keys = (
            "remote_url",
            "remoteUrl",
            "file_url",
            "fileUrl",
            "download_url",
            "downloadUrl",
            "public_url",
            "publicUrl",
            "url",
            "href",
            "link",
            "path",
            "file_path",
            "filePath",
            "filepath",
        )
        for key in preferred_keys:
            value = data.get(key)
            url = _coerce_remote_url(value, upload_base)
            if url:
                return url
        nested = data.get("data")
        url = _extract_remote_url(nested, upload_base)
        if url:
            return url
        ignored_keys = {
            "raw",
            "msg",
            "message",
            "error",
            "code",
            "errcode",
            "status",
            "success",
            "ok",
        }
        for key, value in data.items():
            if key in ignored_keys:
                continue
            url = _extract_remote_url(value, upload_base)
            if url:
                return url
    elif isinstance(data, list):
        for item in data:
            url = _extract_remote_url(item, upload_base)
            if url:
                return url
    return _coerce_remote_url(data, upload_base)


def _success_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value in {0, 200}
    if isinstance(value, float):
        return value.is_integer() and int(value) in {0, 200}
    if isinstance(value, str):
        return value.strip().lower() in {
            "0",
            "200",
            "ok",
            "success",
            "succeed",
            "succeeded",
            "true",
        }
    return False


def _upload_response_ok(resp: httpx.Response, data: dict[str, Any]) -> bool:
    if resp.status_code < 200 or resp.status_code >= 300:
        return False
    for key in ("code", "errcode", "status", "success", "ok"):
        if key in data:
            return _success_value(data[key])
    return True


def _failure_message(resp: httpx.Response, data: dict[str, Any]) -> str:
    reason = data.get("msg") or data.get("message") or data.get("error") or resp.text
    return f"上传失败: {str(reason)[:200]}"


def _attempt_count(retry_attempts: int) -> int:
    return max(1, int(retry_attempts))


async def _sleep_before_retry(attempt: int, retry_attempts: int, retry_delay_sec: float) -> None:
    if attempt >= retry_attempts or retry_delay_sec <= 0:
        return
    await asyncio.sleep(retry_delay_sec)


async def verify_remote_file_url(
    remote_url: str,
    *,
    client: httpx.AsyncClient | None = None,
    retry_attempts: int = 1,
    retry_delay_sec: float = 0.0,
) -> tuple[bool, str]:
    """Best-effort verify that a public download link is reachable without downloading it."""
    url = (remote_url or "").strip()
    if not url:
        return False, "下载链接为空"
    if not url.startswith(("http://", "https://")):
        return False, f"下载链接不是 HTTP(S): {url}"

    attempts = _attempt_count(retry_attempts)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    last_message = "下载链接验证失败"
    try:
        for attempt in range(1, attempts + 1):
            try:
                head = await client.head(url, follow_redirects=True)
                if 200 <= head.status_code < 400:
                    return True, f"HEAD {head.status_code}"
                last_message = f"HEAD {head.status_code}"
            except Exception as exc:
                last_message = f"HEAD 异常: {exc}"

            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={"Range": "bytes=0-0"},
                    follow_redirects=True,
                ) as resp:
                    if 200 <= resp.status_code < 400:
                        return True, f"GET {resp.status_code}"
                    last_message = f"{last_message}; GET {resp.status_code}"
            except Exception as exc:
                last_message = f"{last_message}; GET 异常: {exc}"
            await _sleep_before_retry(attempt, attempts, retry_delay_sec)
        return False, last_message
    finally:
        if owns:
            await client.aclose()


async def upload_file_to_cloud(
    upload_url: str,
    upload_token: str,
    file_path: str | Path,
    *,
    relative_path: str = DEFAULT_UPLOAD_RELATIVE_PATH,
    client: httpx.AsyncClient | None = None,
    retry_attempts: int = 2,
    retry_delay_sec: float = 0.5,
    verify_remote_url: bool = False,
) -> UploadResult:
    """上传本地文件到远程服务器，返回可分享链接（若服务器返回或可推断）。"""
    url = (upload_url or "").strip()
    path = Path(file_path)
    if not url:
        return UploadResult(
            ok=True,
            uploaded=False,
            local_only=True,
            message="未配置上传地址，已仅标记本地提交",
        )
    if not path.exists():
        return UploadResult(ok=False, message=f"文件不存在: {path}")
    if path.is_dir():
        return UploadResult(ok=False, message="不支持上传文件夹")

    upload_endpoint = _build_upload_url(url, relative_path)
    auth = _normalize_bearer_token(upload_token)
    headers = {"Authorization": auth} if auth else {}
    attempts = _attempt_count(retry_attempts)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    logger.info("上传文件: {} -> {} (relativePath={})", path.name, url, relative_path)

    try:
        result = UploadResult(ok=False, message="上传失败")
        for attempt in range(1, attempts + 1):
            try:
                with path.open("rb") as fp:
                    files = {"file": (path.name, fp, "application/octet-stream")}
                    resp = await client.post(upload_endpoint, files=files, headers=headers)
                try:
                    data: dict[str, Any] = resp.json()
                except Exception:
                    data = {"raw": resp.text}
                ok = _upload_response_ok(resp, data)
                if ok:
                    remote_url = _extract_remote_url(data, url) or _guess_remote_url(
                        url,
                        relative_path,
                        path.name,
                    )
                    verified = False
                    verification_message = ""
                    if verify_remote_url:
                        verified, verification_message = await verify_remote_file_url(
                            remote_url,
                            client=client,
                            retry_attempts=retry_attempts,
                            retry_delay_sec=retry_delay_sec,
                        )
                        if not verified:
                            message = (
                                "上传成功但下载链接验证失败: "
                                f"{verification_message}"
                            )
                            result = UploadResult(
                                ok=False,
                                message=message,
                                remote_url=remote_url,
                                remote_url_verified=False,
                                verification_message=verification_message,
                                response=data,
                            )
                            logger.warning("{} (attempt {}/{})", message, attempt, attempts)
                            await _sleep_before_retry(attempt, attempts, retry_delay_sec)
                            continue
                    logger.success("上传成功: {} -> {}", path.name, remote_url)
                    return UploadResult(
                        ok=True,
                        uploaded=True,
                        remote_url=remote_url,
                        remote_url_verified=verified,
                        verification_message=verification_message,
                        message="上传成功",
                        response=data,
                    )

                message = _failure_message(resp, data)
                result = UploadResult(ok=False, message=message, response=data)
                logger.warning(
                    "上传失败: status={} body={} (attempt {}/{})",
                    resp.status_code,
                    data,
                    attempt,
                    attempts,
                )
            except Exception as exc:
                result = UploadResult(ok=False, message=str(exc))
                logger.warning("上传异常: {} (attempt {}/{})", exc, attempt, attempts)
            await _sleep_before_retry(attempt, attempts, retry_delay_sec)
        logger.error("上传最终失败: {}", result.message)
        return result
    finally:
        if owns:
            await client.aclose()


async def upload_workshop_zip(
    upload_url: str,
    upload_token: str,
    zip_path: Path,
    name: str = "",
) -> UploadResult:
    """上传录制工坊 ZIP 到远程服务器。"""
    _ = name
    return await upload_file_to_cloud(
        upload_url,
        upload_token,
        zip_path,
        relative_path=WORKSHOP_UPLOAD_RELATIVE_PATH,
    )
