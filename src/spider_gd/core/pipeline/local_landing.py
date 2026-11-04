"""本地文件落地：分层目录 + 台账 Excel（红绿状态）+ zip 压缩。

实现 :class:`spider_gd.interfaces.storage.ILocalLanding`。
目录结构：``{data_dir}/{YYYY-MM}/{YYYY-MM-DD}/{任务名}_{执行时间}/{指标名}/*.xlsx``
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

from loguru import logger

from spider_gd.core.pipeline.file_versions import save_versioned_bytes
from spider_gd.interfaces.storage import TaskItemRecord

# 台账状态色：绿=成功 红=失败/缺失 黄=部分
_FILL_GREEN = "C6EFCE"
_FILL_RED = "FFC7CE"
_FILL_YELLOW = "FFEB9C"
_STATUS_FILL = {
    "success": _FILL_GREEN,
    "failed": _FILL_RED,
    "skipped": _FILL_YELLOW,
    "pending": _FILL_YELLOW,
}


class LocalLanding:
    def __init__(self, data_dir: str | Path = "./data") -> None:
        self._data_dir = Path(data_dir)

    def create_task_dir(self, task_name: str, exec_time: str) -> str:
        """exec_time 形如 ``20260115_083000``，据此分层为 年月/年月日。"""
        dt = datetime.strptime(exec_time, "%Y%m%d_%H%M%S")
        task_dir = (
            self._data_dir
            / dt.strftime("%Y-%m")
            / dt.strftime("%Y-%m-%d")
            / f"{task_name}_{exec_time}"
        )
        task_dir.mkdir(parents=True, exist_ok=True)
        logger.info("创建任务目录：{}", task_dir)
        return str(task_dir)

    def save_data_file(self, task_dir: str, indicator: str, date: str, data: bytes) -> str:
        indicator_dir = Path(task_dir) / indicator
        indicator_dir.mkdir(parents=True, exist_ok=True)
        file_path = indicator_dir / f"{date}.xlsx"
        result = save_versioned_bytes(file_path, data)
        if result.reused_existing:
            logger.info("落地数据文件未变化：{} sha256={}", file_path, result.sha256[:12])
        elif result.archived_path:
            logger.info(
                "落地数据文件已更新：{}，旧版本归档到 {}",
                file_path,
                result.archived_path,
            )
        else:
            logger.info("落地数据文件：{} ({} bytes)", file_path, len(data))
        return str(file_path)

    def generate_ledger(self, task_dir: str, items: list[TaskItemRecord]) -> str:
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill

        wb = Workbook()
        ws = wb.active
        ws.title = "文件清单"
        ws.append(["指标名称", "目标日期", "文件路径", "文件大小", "状态", "备注"])

        for item in items:
            row = [
                item.indicator_name,
                item.target_date,
                item.file_path or "",
                item.file_size,
                item.status,
                item.error_msg or "",
            ]
            ws.append(row)
            color = _STATUS_FILL.get(item.status, _FILL_YELLOW)
            ws.cell(row=ws.max_row, column=5).fill = PatternFill(
                start_color=color, end_color=color, fill_type="solid"
            )

        summary = wb.create_sheet("执行摘要")
        total = len(items)
        success = sum(1 for i in items if i.status == "success")
        failed = sum(1 for i in items if i.status == "failed")
        summary.append(["总数", "成功", "失败", "成功率"])
        rate = f"{success / total:.0%}" if total else "0%"
        summary.append([total, success, failed, rate])

        ledger_path = Path(task_dir) / "台账.xlsx"
        wb.save(ledger_path)
        logger.info("生成台账：{}（成功 {}/{}）", ledger_path, success, total)
        return str(ledger_path)

    def compress_task(self, task_dir: str) -> str | None:
        src = Path(task_dir)
        zip_path = src.with_suffix(".zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in src.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(src.parent))
        logger.info("压缩任务目录：{}", zip_path)
        return str(zip_path)
