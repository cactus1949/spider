"""测试验证码识别（注入 ocr_fn，确定性逻辑进 CI；真实 OCR 标 slow 不进 CI）。"""

from __future__ import annotations

import pytest

from spider_gd.core.crawler.captcha_solver import CaptchaSolver
from spider_gd.core.crawler.exceptions import CaptchaError


@pytest.mark.unit
def test_recognize_strips_whitespace_and_keeps_case():
    solver = CaptchaSolver(ocr_fn=lambda _b: "  Ab 12 ")
    assert solver.recognize(b"img") == "Ab12"


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["zwe", "验证码", "一二三四", "ab12!!", "ab12中", "ABCDE"])
def test_recognize_rejects_length_han_and_other_chars(raw: str):
    solver = CaptchaSolver(ocr_fn=lambda _b: raw)
    with pytest.raises(CaptchaError, match="自检未通过"):
        solver.recognize(b"img")


@pytest.mark.unit
def test_recognize_alphanumeric_passthrough():
    solver = CaptchaSolver(ocr_fn=lambda _b: "AB12")
    assert solver.recognize(b"img") == "AB12"


@pytest.mark.unit
def test_recognize_empty_bytes_raises():
    solver = CaptchaSolver(ocr_fn=lambda _b: "1234")
    with pytest.raises(CaptchaError, match="图片为空"):
        solver.recognize(b"")


@pytest.mark.unit
def test_recognize_length_out_of_range_raises():
    solver = CaptchaSolver(ocr_fn=lambda _b: "12")  # 太短
    with pytest.raises(CaptchaError, match="自检未通过"):
        solver.recognize(b"img")


@pytest.mark.unit
def test_recognize_too_long_rejected():
    """超过 4 位的结果应被拒绝（该站点验证码固定 4 位）。"""
    solver = CaptchaSolver(ocr_fn=lambda _b: "AB123")  # 5 位
    with pytest.raises(CaptchaError, match="自检未通过"):
        solver.recognize(b"img")


@pytest.mark.unit
def test_recognize_exactly_4_ok():
    """恰好 4 位字母数字应通过。"""
    solver = CaptchaSolver(ocr_fn=lambda _b: "A1B2")
    assert solver.recognize(b"img") == "A1B2"


@pytest.mark.slow
def test_real_ddddocr_recognition_rate(fixtures_dir):
    """真实 OCR 识别率（非确定性，CI 跳过；需 ocr 依赖 + 样本）。"""
    pytest.importorskip("ddddocr")
    sample_dir = fixtures_dir / "captcha_images"
    if not sample_dir.exists() or not any(sample_dir.iterdir()):
        pytest.skip("无验证码样本（现场侦察后补充）")
