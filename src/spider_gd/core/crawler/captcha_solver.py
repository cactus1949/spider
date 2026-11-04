"""验证码识别（字母 + 数字，基于 ddddocr）。

- ddddocr 延迟加载、实例复用。
- 不做图片预处理（实测预处理反而降低识别率）。
- 识别结果过滤为字母数字，并校验长度区间。
- ``ocr_fn`` 可注入，便于单元测试不依赖模型。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from loguru import logger

from spider_gd.core.crawler.exceptions import CaptchaError

_MIN_LEN: Final = 4
_MAX_LEN: Final = 4   # 该站点验证码固定 4 位数字+字母

_OcrFn = Callable[[bytes], str]
_ocr_instance: Any = None


def _default_ocr(image_bytes: bytes) -> str:
    """使用 ddddocr 进行 OCR（延迟加载、单例复用）。"""
    global _ocr_instance
    if _ocr_instance is None:
        import ddddocr

        _ocr_instance = ddddocr.DdddOcr(show_ad=False, beta=True)
    return str(_ocr_instance.classification(image_bytes))


class CaptchaSolver:
    """实现 :class:`spider_gd.interfaces.crawler.ICaptchaSolver`。"""

    def __init__(
        self,
        ocr_fn: _OcrFn | None = None,
        min_len: int = _MIN_LEN,
        max_len: int = _MAX_LEN,
    ) -> None:
        self._ocr = ocr_fn or _default_ocr
        self._min_len = min_len
        self._max_len = max_len

    def recognize(self, image_bytes: bytes) -> str:
        if not image_bytes:
            logger.warning("验证码图片为空")
            raise CaptchaError("图片为空")

        raw = self._ocr(image_bytes)
        cleaned = "".join(c for c in raw if c.isalnum())
        if not self._min_len <= len(cleaned) <= self._max_len:
            logger.error("验证码长度异常: raw={!r} cleaned={!r}", raw, cleaned)
            raise CaptchaError(f"识别结果长度异常: {raw!r}")

        logger.debug("验证码识别: {} (raw={!r})", cleaned, raw)
        return cleaned
