"""数据总览热力矩阵、Excel 多 Sheet/多列完整度体检与按需打包单元测试。"""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from spider_gd.core.overview_matrix import (
    build_overview_matrix,
    inspect_file_completeness,
    iter_pack_overview_files,
    pack_overview_files,
    preview_overview_file,
)
from spider_gd.infrastructure.storage.database import TaskStateStore


def _make_xlsx(path: Path, sheets: dict[str, list[list[object]]]) -> Path:
    wb = openpyxl.Workbook()
    first = True
    for name, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet(title=name)
        ws.title = name
        first = False
        for row in rows:
            ws.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    wb.close()
    return path


@pytest.mark.unit
def test_inspect_xlsx_detects_empty_partial_and_complete(tmp_path: Path) -> None:
    # 1. 纯表头空表
    f_empty = _make_xlsx(tmp_path / "empty.xlsx", {"Sheet1": [["时刻", "电量", "电价"]]})
    assert inspect_file_completeness(f_empty)["status"] == "empty"

    # 2. 单 Sheet 双指标：日前有值、实时全空/全 0 -> partial
    f_part_col = _make_xlsx(
        tmp_path / "part_col.xlsx",
        {
            "现货分日": [
                ["日期", "日前均价", "实时均价", "实时电量"],
                ["2026-11-25", 384.33, None, 0],
                ["2026-11-25", 390.10, "", "0"],
            ]
        },
    )
    res_col = inspect_file_completeness(f_part_col)
    assert res_col["status"] == "partial"
    assert "实时均价" in res_col["inspect"][0]["detail"]

    # 3. 多 Sheet 中部分 Sheet 为空 -> partial
    f_part_sheet = _make_xlsx(
        tmp_path / "part_sheet.xlsx",
        {
            "负荷预测": [["时刻", "负荷"], ["00:00", 52000]],
            "阻塞断面": [["时刻", "断面", "越限"]],
        },
    )
    res_sheet = inspect_file_completeness(f_part_sheet)
    assert res_sheet["status"] == "partial"
    assert "1/2 个 Sheet 为空" in res_sheet["summary"]

    # 4. 完整有值 -> ok
    f_ok = _make_xlsx(
        tmp_path / "ok.xlsx",
        {"节点电价": [["节点", "日前电价", "实时电价"], ["水城变", 380.5, 372.1]]},
    )
    assert inspect_file_completeness(f_ok)["status"] == "ok"


@pytest.mark.unit
def test_build_overview_matrix_supports_positive_d_plus_2_and_packing(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path / "state.db")
    dl = tmp_path / "downloads"
    try:
        store.save_settings({
            "downloadDir": str(dl),
            "stationProfiles": '[{"id":"st1","name":"阳江明阳","wifiSsid":"YJ01"}]',
        })
        store.save_task_config({
            "id": "t_pub",
            "name": "公有",
            "indicators": ["日前交易预测"],
            "station_mode": "any_one",
            "indicator_windows": {"日前交易预测": {"start": -2, "end": 2}},
            "enabled": True,
        })
        # D=2026-11-26，该指标支持 D+2 (2026-11-28)
        f_d2 = _make_xlsx(
            dl / "日前交易预测(2026-11-28).xlsx",
            {"预测": [["时段", "电价"], ["01:00", 412.5]]},
        )
        res = build_overview_matrix(
            store,
            default_download_dir=str(dl),
            start_date=date(2026, 11, 25),
            end_date=date(2026, 12, 2),
            today=date(2026, 11, 26),
        )
        assert res["has_real_config"] is True
        row = res["data"]["public"]["rows"][0]
        assert row["lag"] == 2
        assert row["cells"]["2026-11-28"]["status"] == "ok"
        assert row["cells"]["2026-11-29"]["status"] == "future"
        assert row["cells"]["2026-11-27"]["status"] == "miss"

        # 测试打包导出 ZIP
        out_zip = tmp_path / "out.zip"
        pack = pack_overview_files(
            [{"owner": "公有数据", "indicator": "日前交易预测", "date": "2026-11-28", "file_path": str(f_d2)}],
            target_path=out_zip,
            mode="zip",
            layout="station_indicator",
        )
        assert pack["ok"] is True
        assert pack["count"] == 1
        with zipfile.ZipFile(out_zip) as zf:
            assert any("日前交易预测(2026-11-28).xlsx" in n for n in zf.namelist())
        # 测试传目录自动生成 ZIP + 流式逐文件进度
        export_dir = tmp_path / "picked_dir"
        events = list(
            iter_pack_overview_files(
                [{"owner": "公有数据", "indicator": "日前交易预测", "date": "2026-11-28", "file_path": str(f_d2)}],
                target_path=export_dir,
                mode="zip",
                filename="取数包.zip",
            )
        )
        assert [e["event"] for e in events] == ["start", "progress", "done"]
        assert events[1]["percent"] == 100
        assert Path(events[2]["output"]) == export_dir / "取数包.zip"
        assert (export_dir / "取数包.zip").exists()
    finally:
        store.close()


@pytest.mark.unit
def test_monthly_matrix_and_excel_preview(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path / "state_m.db")
    dl = tmp_path / "downloads"
    try:
        store.save_settings({"downloadDir": str(dl)})
        store.save_workflow_config({
            "id": "wf_month",
            "name": "月结算概况",
            "yaml_text": "name: 月结算概况\ndate_kind: month\ndate_window:\n  start: -1\n  end: -1\n",
        })
        store.save_task_config({
            "id": "t_pub",
            "name": "公有",
            "indicators": ["月结算概况"],
            "station_mode": "any_one",
            "enabled": True,
        })
        f_oct = _make_xlsx(
            dl / "月结算概况(2026-10).xlsx",
            {
                "月度总览": [
                    ["项目", "结算电量", "偏差考核"],
                    ["发电侧汇总", 128500.5, 0],
                    ["现货偏差", 3200.25, None],
                ],
                "空附表": [["时段", "数值"]],
            },
        )
        res = build_overview_matrix(
            store,
            default_download_dir=str(dl),
            start_date=date(2026, 10, 15),
            end_date=date(2026, 12, 10),
            today=date(2026, 11, 26),
        )
        row = res["data"]["public"]["rows"][0]
        assert row["date_kind"] == "month"
        assert "2025-01" in res["months"]
        assert "2026-10" in res["months"]
        assert "2027-12" in res["months"]
        assert row["cells"]["2026-10"]["status"] == "partial"
        assert row["cells"]["2026-11"]["status"] == "future"
        assert row["cells"]["2026-12"]["status"] == "future"

        prev = preview_overview_file(f_oct, sheet="月度总览", max_rows=20)
        assert prev["ok"] is True
        assert prev["kind"] == "table"
        assert prev["active_sheet"] == "月度总览"
        assert len(prev["sheets"]) == 2
        assert prev["sheets"][0]["status"] == "partial"
        assert prev["sheets"][1]["status"] == "empty"
        assert prev["headers"] == ["项目", "结算电量", "偏差考核"]
        assert prev["empty_col_indices"] == [2]
        assert prev["rows"][0][1] == "128500.5"
    finally:
        store.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hardware_lease_queue_prioritizes_manual_over_auto() -> None:
    import asyncio
    from spider_gd.infrastructure.hardware_lease import HardwareLease, acquire_hardware_lease_async

    holder = HardwareLease.try_acquire()
    assert holder is not None
    order: list[str] = []
    waited: list[str] = []

    async def _run_auto() -> None:
        lease = await acquire_hardware_lease_async(
            priority=1,
            timeout_seconds=5.0,
            poll_interval=0.05,
            on_wait=lambda _pos: waited.append("auto"),
        )
        assert lease is not None
        try:
            order.append("auto")
            await asyncio.sleep(0.05)
        finally:
            lease.close()

    async def _run_manual() -> None:
        await asyncio.sleep(0.08)
        lease = await acquire_hardware_lease_async(
            priority=0,
            timeout_seconds=5.0,
            poll_interval=0.05,
            on_wait=lambda _pos: waited.append("manual"),
        )
        assert lease is not None
        try:
            order.append("manual")
            await asyncio.sleep(0.05)
        finally:
            lease.close()

    t_auto = asyncio.create_task(_run_auto())
    t_manual = asyncio.create_task(_run_manual())
    await asyncio.sleep(0.22)
    holder.close()
    await asyncio.gather(t_auto, t_manual)

    assert "auto" in waited
    assert order == ["manual", "auto"]
