"""测试数据去重、缺口检测与 Excel 解析处理器。"""

from __future__ import annotations

import io

import pandas as pd
import pytest
from openpyxl import Workbook

from spider_gd.core.pipeline.data_processor import (
    DataProcessor,
    DataQualityError,
    deduplicate,
    detect_gaps,
)


def _excel_bytes(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.unit
def test_deduplicate_by_keys():
    df = pd.DataFrame(
        {
            "pdate": ["20260618", "20260618", "20260619"],
            "item": ["a", "a", "b"],
            "val": [1, 2, 3],
        }
    )
    result = deduplicate(df, keys=["pdate", "item"])
    assert len(result) == 2
    # keep="last" 保留 val=2
    assert result[(result.pdate == "20260618") & (result.item == "a")]["val"].iloc[0] == 2


@pytest.mark.unit
def test_detect_gaps_finds_missing_dates():
    present = ["2026-01-01", "2026-01-03"]
    missing = detect_gaps(present, "2026-01-01", "2026-01-04")
    assert missing == ["2026-01-02", "2026-01-04"]


@pytest.mark.unit
def test_detect_gaps_no_missing():
    present = ["2026-01-01", "2026-01-02"]
    assert detect_gaps(present, "2026-01-01", "2026-01-02") == []


@pytest.mark.unit
def test_processor_parses_and_returns_raw_bytes():
    data = _excel_bytes([["日期", "数值"], ["2026-06-18", 100]])
    proc = DataProcessor()
    sheets = proc.parse_excel(data)
    assert len(sheets) == 1
    # 阶段 2：原样落地，返回原始 bytes
    assert proc.process(data, "日清算") == data


@pytest.mark.unit
def test_processor_rejects_invalid_excel():
    proc = DataProcessor()
    with pytest.raises(DataQualityError):
        proc.process(b"not-an-excel", "日清算")


@pytest.mark.unit
def test_processor_rejects_empty_excel():
    data = _excel_bytes([["仅表头"]])  # 只有一行表头 → 0 数据行
    proc = DataProcessor()
    with pytest.raises(DataQualityError):
        proc.process(data, "日清算")
