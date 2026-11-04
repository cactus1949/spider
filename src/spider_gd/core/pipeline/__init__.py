"""数据处理管道与本地落地（链路3）。"""

from __future__ import annotations

from spider_gd.core.pipeline.data_processor import (
    DataProcessor,
    DataQualityError,
    deduplicate,
    detect_gaps,
)
from spider_gd.core.pipeline.local_landing import LocalLanding

__all__ = [
    "DataProcessor",
    "DataQualityError",
    "LocalLanding",
    "deduplicate",
    "detect_gaps",
]
