"""数据处理：Excel 解析 + 去重 + 时间序列缺口检测（纯逻辑，基于 pandas）。

阶段 2 范围（决策三）：只做与平台格式无关的部分——
- Excel 二进制解析为 ``{sheet: DataFrame}``；
- 解析校验（能否打开、是否空表）；
- 原始 bytes 落地（不改写内容）。
字段标准化 / 质量校验 / 时序对齐推迟到阶段 2.5（拿到真实平台样本后再做），
避免基于 mock 随机数据写出会被推翻的标准化逻辑。
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import datetime, timedelta

import pandas as pd
from loguru import logger


class DataQualityError(ValueError):
    """解析后数据未通过基础质量校验（如完全为空）。"""


class DataProcessor:
    """采集结果数据处理器（阶段 2：解析 + 校验 + 原始落地）。

    用作 :class:`CrawlOrchestrator` 的 processor 钩子：``process(data, indicator) -> bytes``。
    当前实现解析校验后返回**原始 bytes**用于落地；阶段 2.5 再在此追加标准化输出。
    """

    def __init__(self, *, require_non_empty: bool = True) -> None:
        self._require_non_empty = require_non_empty

    def parse_excel(self, data: bytes) -> dict[str, pd.DataFrame]:
        """把 Excel 二进制解析为 ``{sheet_name: DataFrame}``。"""
        try:
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
        except Exception as exc:  # 非法/损坏的 Excel
            raise DataQualityError(f"Excel 解析失败：{exc}") from exc
        logger.debug("解析 Excel：{} 个 sheet", len(sheets))
        return sheets

    def validate(self, sheets: dict[str, pd.DataFrame]) -> None:
        """基础质量校验：至少一个非空 sheet。"""
        if not self._require_non_empty:
            return
        total_rows = sum(len(df) for df in sheets.values())
        if total_rows == 0:
            raise DataQualityError("解析结果为空（无数据行）")

    def process(self, data: bytes, indicator: str) -> bytes:
        """解析校验后返回原始 bytes 供落地。校验失败抛 :class:`DataQualityError`。"""
        sheets = self.parse_excel(data)
        self.validate(sheets)
        rows = sum(len(df) for df in sheets.values())
        logger.info("数据处理 {}：{} sheet / {} 行（原始落地）", indicator, len(sheets), rows)
        return data


def deduplicate(df: pd.DataFrame, keys: Sequence[str]) -> pd.DataFrame:
    """按主键去重，保留最后一条。"""
    before = len(df)
    result = df.drop_duplicates(subset=list(keys), keep="last").reset_index(drop=True)
    logger.debug("去重 {} -> {} 行 (keys={})", before, len(result), list(keys))
    return result


def detect_gaps(present_dates: Sequence[str], start: str, end: str) -> list[str]:
    """检测 [start, end] 闭区间内缺失的日期（YYYY-MM-DD）。

    返回缺失日期列表（升序）。
    """
    present = set(present_dates)
    cur = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    missing: list[str] = []
    while cur <= end_dt:
        day = cur.strftime("%Y-%m-%d")
        if day not in present:
            missing.append(day)
        cur += timedelta(days=1)
    if missing:
        logger.info("检测到 {} 个缺口日期", len(missing))
    return missing
