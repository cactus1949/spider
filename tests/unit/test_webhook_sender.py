"""测试钉钉签名与发送（respx mock）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse

import httpx
import pytest
import respx

from spider_gd.gateway.alert.webhook_sender import (
    build_dingtalk_signed_url,
    send_dingtalk,
    send_pushplus,
    send_serverchan,
    send_wecom,
    send_wecom_file,
    upload_wecom_robot_file,
)

_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=tok"
_SECRET = "SEC123"


@pytest.mark.unit
def test_build_signed_url_matches_reference():
    ts = 1700000000000
    url = build_dingtalk_signed_url(_WEBHOOK, _SECRET, timestamp_ms=ts)
    string_to_sign = f"{ts}\n{_SECRET}"
    digest = hmac.new(_SECRET.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    expected_sign = urllib.parse.quote_plus(base64.b64encode(digest))
    assert f"timestamp={ts}" in url
    assert f"sign={expected_sign}" in url


@pytest.mark.unit
@respx.mock
async def test_send_dingtalk_success():
    route = respx.post(url__regex=r"https://oapi\.dingtalk\.com/.*").mock(
        return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
    )
    ok = await send_dingtalk(_WEBHOOK, "hello", secret=_SECRET)
    assert ok is True
    assert route.called


@pytest.mark.unit
@respx.mock
async def test_send_dingtalk_failure_errcode():
    respx.post(url__regex=r"https://oapi\.dingtalk\.com/.*").mock(
        return_value=httpx.Response(200, json={"errcode": 310000, "errmsg": "sign error"})
    )
    assert await send_dingtalk(_WEBHOOK, "hi", secret=_SECRET) is False


@pytest.mark.unit
@respx.mock
async def test_send_wecom_retries_transient_failure():
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"errcode": 1, "errmsg": "busy"})
        return httpx.Response(200, json={"errcode": 0})

    route = respx.post(webhook).mock(side_effect=handler)

    ok = await send_wecom(webhook, "hello", retry_attempts=2, retry_delay_sec=0)

    assert ok is True
    assert route.call_count == 2


@pytest.mark.unit
@respx.mock
async def test_send_wecom_accepts_string_errcode_success():
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
    respx.post(webhook).mock(return_value=httpx.Response(200, json={"errcode": "0"}))

    ok = await send_wecom(webhook, "hello", retry_attempts=1, retry_delay_sec=0)

    assert ok is True


@pytest.mark.unit
@respx.mock
async def test_send_serverchan_retries_transient_failure():
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"code": 1, "message": "busy"})
        return httpx.Response(200, json={"code": 0})

    route = respx.post("https://sctapi.ftqq.com/SCT123.send").mock(side_effect=handler)

    ok = await send_serverchan("SCT123", "hello", retry_attempts=2, retry_delay_sec=0)

    assert ok is True
    assert route.call_count == 2


@pytest.mark.unit
@respx.mock
async def test_send_serverchan_accepts_string_code_success():
    respx.post("https://sctapi.ftqq.com/SCT123.send").mock(
        return_value=httpx.Response(200, json={"code": "0"})
    )

    ok = await send_serverchan("SCT123", "hello", retry_attempts=1, retry_delay_sec=0)

    assert ok is True


@pytest.mark.unit
@respx.mock
async def test_send_pushplus_retries_transient_failure():
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"code": 500, "msg": "busy"})
        return httpx.Response(200, json={"code": 200})

    route = respx.post("https://www.pushplus.plus/send").mock(side_effect=handler)

    ok = await send_pushplus("tok", "hello", retry_attempts=2, retry_delay_sec=0)

    assert ok is True
    assert route.call_count == 2


@pytest.mark.unit
@respx.mock
async def test_send_pushplus_accepts_string_code_success():
    respx.post("https://www.pushplus.plus/send").mock(
        return_value=httpx.Response(200, json={"code": "200"})
    )

    ok = await send_pushplus("tok", "hello", retry_attempts=1, retry_delay_sec=0)

    assert ok is True


@pytest.mark.unit
@respx.mock
async def test_upload_wecom_robot_file_returns_media_id(tmp_path):
    fp = tmp_path / "result.zip"
    fp.write_bytes(b"123456")
    route = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))

    media_id = await upload_wecom_robot_file(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        fp,
    )

    assert media_id == "MEDIA"
    assert route.called


@pytest.mark.unit
@respx.mock
async def test_upload_wecom_robot_file_retries_transient_failure(tmp_path):
    fp = tmp_path / "result.zip"
    fp.write_bytes(b"123456")
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"errcode": 1, "errmsg": "busy"})
        return httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"})

    route = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(side_effect=handler)

    media_id = await upload_wecom_robot_file(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        fp,
        retry_attempts=2,
        retry_delay_sec=0,
    )

    assert media_id == "MEDIA"
    assert route.call_count == 2


@pytest.mark.unit
@respx.mock
async def test_send_wecom_file_uploads_then_sends(tmp_path):
    fp = tmp_path / "result.zip"
    fp.write_bytes(b"123456")
    upload = respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA"}))
    send = respx.post("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc").mock(
        return_value=httpx.Response(200, json={"errcode": 0})
    )

    ok = await send_wecom_file(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        fp,
    )

    assert ok is True
    assert upload.called
    assert send.called
    assert send.calls.last.request.content == b'{"msgtype":"file","file":{"media_id":"MEDIA"}}'


@pytest.mark.unit
@respx.mock
async def test_send_wecom_file_accepts_string_errcode_success(tmp_path):
    fp = tmp_path / "result.zip"
    fp.write_bytes(b"123456")
    respx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=abc&type=file"
    ).mock(return_value=httpx.Response(200, json={"errcode": "0", "media_id": "MEDIA"}))
    respx.post("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc").mock(
        return_value=httpx.Response(200, json={"errcode": "0"})
    )

    ok = await send_wecom_file(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        fp,
        retry_attempts=1,
        retry_delay_sec=0,
    )

    assert ok is True
