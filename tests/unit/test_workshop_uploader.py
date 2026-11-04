"""文件上传测试。"""

from pathlib import Path

import httpx
import pytest
import respx

from spider_gd.gateway.workshop_uploader import (
    DEFAULT_UPLOAD_RELATIVE_PATH,
    WORKSHOP_UPLOAD_RELATIVE_PATH,
    upload_file_to_cloud,
    upload_workshop_zip,
)


def _endpoint(relative_path: str = DEFAULT_UPLOAD_RELATIVE_PATH) -> str:
    return f"https://example.com/guangdong/data/upload?relativePath={relative_path}"


@pytest.mark.asyncio
async def test_upload_without_url_marks_local_only(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake")
    result = await upload_workshop_zip("", "", zpath, "a")
    assert result.ok is True
    assert result.local_only is True
    assert result.uploaded is False


@pytest.mark.asyncio
@respx.mock
async def test_upload_file_to_cloud_retries_transient_failure(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"code": 500, "msg": "busy"})
        return httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://cdn.example.com/a.zip"}},
        )

    route = respx.post(_endpoint()).mock(side_effect=handler)

    result = await upload_file_to_cloud(
        "https://example.com",
        "",
        zpath,
        retry_attempts=2,
        retry_delay_sec=0,
    )

    assert result.ok is True
    assert result.remote_url == "https://cdn.example.com/a.zip"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_upload_with_url_posts_file_and_returns_remote_url(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    route = respx.post(_endpoint(WORKSHOP_UPLOAD_RELATIVE_PATH)).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://cdn.example.com/a.zip"}},
        )
    )

    result = await upload_workshop_zip("https://example.com", "tok", zpath, "a")

    assert result.ok is True
    assert result.uploaded is True
    assert result.remote_url == "https://cdn.example.com/a.zip"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@pytest.mark.asyncio
@respx.mock
async def test_upload_file_to_cloud_keeps_explicit_upload_public_data_path(tmp_path: Path):
    zpath = tmp_path / "result.zip"
    zpath.write_bytes(b"fake data")
    route = respx.post(_endpoint("/upload/publicData")).mock(
        return_value=httpx.Response(200, json={"code": 200})
    )

    result = await upload_file_to_cloud(
        "https://example.com",
        "",
        zpath,
        relative_path="/upload/publicData",
    )

    assert result.ok is True
    assert result.remote_url == "https://example.com/upload/publicData/result.zip"
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_upload_verifies_remote_url_with_head(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://cdn.example.com/a.zip"}},
        )
    )
    head = respx.head("https://cdn.example.com/a.zip").mock(
        return_value=httpx.Response(200)
    )

    result = await upload_file_to_cloud(
        "https://example.com",
        "",
        zpath,
        verify_remote_url=True,
        retry_attempts=1,
    )

    assert result.ok is True
    assert result.remote_url_verified is True
    assert result.verification_message == "HEAD 200"
    assert head.called


@pytest.mark.asyncio
@respx.mock
async def test_upload_verifies_remote_url_with_range_get_when_head_fails(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://cdn.example.com/a.zip"}},
        )
    )
    respx.head("https://cdn.example.com/a.zip").mock(return_value=httpx.Response(405))
    get = respx.get("https://cdn.example.com/a.zip").mock(
        return_value=httpx.Response(206, content=b"x")
    )

    result = await upload_file_to_cloud(
        "https://example.com",
        "",
        zpath,
        verify_remote_url=True,
        retry_attempts=1,
    )

    assert result.ok is True
    assert result.remote_url_verified is True
    assert result.verification_message == "GET 206"
    assert get.calls.last.request.headers["Range"] == "bytes=0-0"


@pytest.mark.asyncio
@respx.mock
async def test_upload_fails_when_remote_url_verification_fails(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"url": "https://cdn.example.com/a.zip"}},
        )
    )
    respx.head("https://cdn.example.com/a.zip").mock(return_value=httpx.Response(404))
    respx.get("https://cdn.example.com/a.zip").mock(return_value=httpx.Response(404))

    result = await upload_file_to_cloud(
        "https://example.com",
        "",
        zpath,
        verify_remote_url=True,
        retry_attempts=1,
    )

    assert result.ok is False
    assert result.remote_url == "https://cdn.example.com/a.zip"
    assert result.remote_url_verified is False
    assert "下载链接验证失败" in result.message


@pytest.mark.asyncio
@respx.mock
async def test_upload_guesses_remote_url_when_server_omits_it(tmp_path: Path):
    zpath = tmp_path / "result.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(200, json={"code": 200, "msg": "ok"})
    )

    result = await upload_file_to_cloud("https://example.com/", "", zpath)

    assert result.ok is True
    assert result.remote_url == "https://example.com/upload/publicData/result.zip"


@pytest.mark.asyncio
@respx.mock
async def test_upload_accepts_string_code_success(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(
            200,
            json={"code": "200", "data": {"url": "https://cdn.example.com/a.zip"}},
        )
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert result.remote_url == "https://cdn.example.com/a.zip"


@pytest.mark.asyncio
@respx.mock
async def test_upload_accepts_errcode_zero_and_relative_url(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(200, json={"errcode": 0, "url": "/publicData/a.zip"})
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert result.remote_url == "https://example.com/publicData/a.zip"


@pytest.mark.asyncio
@respx.mock
async def test_upload_accepts_success_true_without_code(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(
            201,
            json={"success": True, "data": {"downloadUrl": "https://cdn.example.com/a.zip"}},
        )
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert result.remote_url == "https://cdn.example.com/a.zip"


@pytest.mark.asyncio
@respx.mock
async def test_upload_accepts_plain_2xx_and_guesses_remote_url(tmp_path: Path):
    zpath = tmp_path / "result.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(204, json={})
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert result.remote_url == "https://example.com/upload/publicData/result.zip"


@pytest.mark.asyncio
@respx.mock
async def test_upload_encodes_guessed_url_with_chinese_file_name(tmp_path: Path):
    zpath = tmp_path / "任务 结果.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(200, json={"code": 200})
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert (
        result.remote_url
        == "https://example.com/upload/publicData/%E4%BB%BB%E5%8A%A1%20%E7%BB%93%E6%9E%9C.zip"
    )


@pytest.mark.asyncio
@respx.mock
async def test_upload_encodes_relative_url_from_server(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "url": "publicData/任务 结果.zip"},
        )
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert (
        result.remote_url
        == "https://example.com/publicData/%E4%BB%BB%E5%8A%A1%20%E7%BB%93%E6%9E%9C.zip"
    )


@pytest.mark.asyncio
@respx.mock
async def test_upload_encodes_absolute_url_from_server_without_double_encoding(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "url": "https://cdn.example.com/publicData/任务%20结果.zip",
            },
        )
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert (
        result.remote_url
        == "https://cdn.example.com/publicData/%E4%BB%BB%E5%8A%A1%20%E7%BB%93%E6%9E%9C.zip"
    )


@pytest.mark.asyncio
@respx.mock
async def test_upload_ignores_raw_html_when_guessing_remote_url(tmp_path: Path):
    zpath = tmp_path / "result.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(200, text="<html>/publicData/wrong.zip</html>")
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert result.remote_url == "https://example.com/upload/publicData/result.zip"


@pytest.mark.asyncio
@respx.mock
async def test_upload_ignores_message_text_when_guessing_remote_url(tmp_path: Path):
    zpath = tmp_path / "result.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(200, json={"code": 200, "msg": "uploaded /tmp/result.zip"})
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert result.remote_url == "https://example.com/upload/publicData/result.zip"


@pytest.mark.asyncio
@respx.mock
async def test_upload_accepts_explicit_path_field(tmp_path: Path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(b"fake data")
    respx.post(_endpoint()).mock(
        return_value=httpx.Response(200, json={"code": 200, "path": "publicData/a.zip"})
    )

    result = await upload_file_to_cloud("https://example.com", "", zpath)

    assert result.ok is True
    assert result.remote_url == "https://example.com/publicData/a.zip"
