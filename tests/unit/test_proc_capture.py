"""子进程输出解码：保证 PYTHONUTF8=1 时也不会把 netsh 输出吞成 None。"""

from __future__ import annotations

import codecs
import subprocess
import sys

import pytest

from spider_gd.infrastructure import proc
from spider_gd.infrastructure.proc import decode_console_bytes, ensure_text, run_capture


@pytest.mark.unit
def test_decode_utf8_bytes():
    assert decode_console_bytes("系统上有 1 个接口".encode()) == "系统上有 1 个接口"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (codecs.BOM_UTF8 + "接口".encode(), "接口"),
        (codecs.BOM_UTF16_LE + "接口".encode("utf-16-le"), "接口"),
        (codecs.BOM_UTF16_BE + "接口".encode("utf-16-be"), "接口"),
    ],
)
def test_decode_handles_bom(raw: bytes, expected: str):
    assert decode_console_bytes(raw) == expected


@pytest.mark.unit
def test_decode_explicit_console_codepage():
    """GBK 字节在 UTF-8 下必定解码失败，必须回退到控制台代码页。"""
    raw = "系统上有 1 个接口".encode("cp936")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert decode_console_bytes(raw, encodings=["utf-8", "cp936"]) == "系统上有 1 个接口"


@pytest.mark.unit
def test_decode_never_raises_on_garbage():
    assert decode_console_bytes(b"\xff\xfe\x00garbage") != ""


@pytest.mark.unit
def test_ensure_text_normalizes_none_and_bytes():
    assert ensure_text(None) == ""
    assert ensure_text("done") == "done"
    assert ensure_text("接口".encode()) == "接口"


@pytest.mark.unit
def test_run_capture_returns_str_even_for_non_utf8_output():
    """子进程写 cp936 字节时，run_capture 仍应返回 str 而不是 None。"""
    code = (
        "import sys;"
        "sys.stdout.buffer.write('系统上有 1 个接口'.encode('cp936'));"
        "sys.stdout.flush()"
    )
    result = run_capture([sys.executable, "-c", code], timeout=60)

    assert result.returncode == 0
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)
    assert result.stdout != ""


@pytest.mark.unit
def test_run_capture_keeps_empty_streams_as_empty_string():
    result = run_capture([sys.executable, "-c", "pass"], timeout=60)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.unit
def test_run_capture_reports_returncode_and_stderr():
    code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    result = run_capture([sys.executable, "-c", code], timeout=60)

    assert result.returncode == 3
    assert isinstance(result.stderr, str)
    assert "boom" in result.stderr
    assert isinstance(result, subprocess.CompletedProcess)


@pytest.mark.unit
def test_gbk_bytes_that_are_valid_utf8_are_decoded_with_console_codepage():
    """“状态”的 GBK 字节恰好是合法 UTF-8；控制台代码页必须优先。"""
    raw = "状态".encode("cp936")

    # 证明这组字节确实能通过 UTF-8 严格解码，因此“先试 UTF-8”会得到乱码。
    assert raw.decode("utf-8") == "״̬"
    assert decode_console_bytes(raw, encodings=["cp936", "utf-8"]) == "状态"


@pytest.mark.unit
def test_default_encodings_prefer_windows_console_codepage(monkeypatch):
    monkeypatch.setattr(proc.sys, "platform", "win32")
    monkeypatch.setattr(proc, "_windows_console_codepage", lambda: 936)

    encodings = proc._default_encodings()

    assert encodings[0] == "cp936"
    assert "utf-8" in encodings
    assert decode_console_bytes("状态".encode("cp936")) == "状态"


@pytest.mark.unit
def test_default_encodings_on_non_windows_use_utf8(monkeypatch):
    monkeypatch.setattr(proc.sys, "platform", "darwin")

    assert proc._default_encodings() == ("utf-8",)
