"""多场站执行：证书窗口、补数场站、落盘隔离和失败后续跑。"""

from __future__ import annotations

import time

from task_runner import TaskRunner, apply_catchup_storage

from spider_gd.core.output_paths import station_scoped_path
from spider_gd.core.ukey_plan import (
    acquire_ukey_run,
    login_page_can_be_skipped,
    release_ukey_run,
    should_reuse_login,
    station_followup,
)
from spider_gd.infrastructure.windows.cert_availability import probe_script
from spider_gd.infrastructure.windows.cert_dialog import (
    CertDialogResult,
    CertDialogRow,
    CertificateWatch,
    _read_rows,
    apply_dialog_choice,
    assemble_certificate_rows,
)


class _Node:
    def __init__(
        self, kind: str, name: str, children: list | None = None, selectable: bool = False,
    ):
        self.ControlTypeName = kind
        self.Name = name
        self._children = children or []
        self.selectable = selectable
        self.selected = False

    def GetChildren(self):
        return self._children

    def GetSelectionItemPattern(self):
        if not self.selectable:
            raise RuntimeError("不是可选行")
        return self

    def Select(self) -> None:
        self.selected = True

    @property
    def IsSelected(self) -> bool:
        return self.selected


def test_dialog_reads_list_rows_and_only_clicks_ok_after_selection_sticks():
    headers = [
        _Node("HeaderItemControl", "颁发给"),
        _Node("HeaderItemControl", "单位"),
        _Node("HeaderItemControl", "截止日期"),
        _Node("HeaderItemControl", "颁发者"),
    ]
    first = _Node("ListItemControl", "甲", [
        _Node("TextControl", "单位甲公司"),
        _Node("TextControl", "2031-01-01"),
        _Node("TextControl", "NETCA"),
    ], selectable=True)
    second = _Node("ListItemControl", "乙", [
        _Node("TextControl", "单位乙公司"),
        _Node("TextControl", "2031-06-01"),
        _Node("TextControl", "NETCA"),
    ], selectable=True)
    dialog = _Node("WindowControl", "证书选择对话框", [*headers, first, second])
    rows = _read_rows(dialog)
    assert [row.org_name for row in rows] == ["单位甲公司", "单位乙公司"]
    calls = {"ok": 0, "cancel": 0}

    def selected(index: int) -> bool:
        return False

    result = apply_dialog_choice(
        rows, "单位甲",
        lambda index: None,
        selected,
        lambda: calls.__setitem__("ok", calls["ok"] + 1),
        lambda: calls.__setitem__("cancel", calls["cancel"] + 1),
    )
    assert result.ok is False
    assert calls["ok"] == 0
    assert calls["cancel"] == 1
    assert "选中" in result.reason


def test_ambiguous_dialog_cancels_instead_of_confirming():
    rows = assemble_certificate_rows(
        ["颁发给", "单位", "截止日期", "颁发者"],
        [
            ["甲", "单位甲公司", "2031-01-01", "NETCA"],
            ["乙", "单位乙公司", "2031-01-01", "NETCA"],
        ],
    )
    calls = {"ok": 0, "cancel": 0}
    result = apply_dialog_choice(
        rows, "公司",
        lambda index: None,
        lambda index: True,
        lambda: calls.__setitem__("ok", 1),
        lambda: calls.__setitem__("cancel", 1),
    )
    assert result.ok is False
    assert calls == {"ok": 0, "cancel": 1}


def test_certificate_watch_unblocks_a_click_that_is_waiting_on_the_dialog():
    dialog = {"open": False, "ok": False}
    rows = [
        CertDialogRow("甲", "单位甲公司", "2031-01-01", "NETCA"),
        CertDialogRow("乙", "单位乙公司", "2031-01-01", "NETCA"),
    ]
    chosen = {"index": None}

    def probe():
        if not dialog["open"]:
            return CertDialogResult(False, "", absent=True)
        return apply_dialog_choice(
            rows, "单位甲",
            lambda index: chosen.__setitem__("index", index),
            lambda index: chosen["index"] == index,
            lambda: dialog.__setitem__("ok", True),
            lambda: None,
        )

    watch = CertificateWatch("单位甲", probe=probe, interval=0.05, timeout=2, after_click=0.2)
    watch.start()
    try:
        dialog["open"] = True
        deadline = time.monotonic() + 2
        while not dialog["ok"]:
            if time.monotonic() > deadline:
                raise TimeoutError("登录点击一直被证书窗口挡住")
            time.sleep(0.02)
        watch.mark_click_finished()
        result = watch.finish(2)
    finally:
        watch.stop()
    assert result.ok is True
    assert chosen["index"] == 0


def test_watch_stops_immediately_when_this_machine_cannot_see_the_dialog():
    sleeps: list[float] = []
    watch = CertificateWatch(
        "单位甲",
        probe=lambda: CertDialogResult(False, "需要现场验收", needs_windows=True, absent=True),
        sleeper=lambda seconds: sleeps.append(seconds),
        timeout=20,
    )
    watch.start()
    result = watch.finish(2)
    assert result.needs_windows is True
    assert result.absent is True
    assert sleeps == []


def test_watch_poll_interval_is_not_a_tight_loop():
    now = {"t": 0.0}
    sleeps: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    def probe():
        if now["t"] < 1.6:
            return CertDialogResult(False, "", absent=True)
        return CertDialogResult(True, "", 0)

    watch = CertificateWatch(
        "单位甲", probe=probe, interval=0.8, timeout=5, clock=clock, sleeper=sleeper,
    )
    watch.start()
    result = watch.finish(2)
    assert result.ok is True
    assert sleeps
    assert min(sleeps) >= 0.8


def test_private_key_probe_is_silent():
    script = probe_script()
    assert "CryptAcquireCertificatePrivateKey" in script
    assert "0x40" in script
    assert "CryptUI" not in script
    assert "CredUI" not in script
    assert "MessageBox" not in script


def test_catchup_keeps_the_matched_task_ukeys_when_storage_is_overridden():
    stored = apply_catchup_storage(
        {"indicators": ["日前"]},
        [{
            "indicators": ["日前"],
            "enabled": True,
            "created_at": 1,
            "ukey_station_ids": ["station-a", "station-b"],
            "path_structure": "- [日前]",
        }],
        path_structure="",
        storage_overridden=True,
    )
    assert stored["ukey_station_ids"] == ["station-a", "station-b"]
    assert stored["path_structure"] == ""


def test_station_directories_do_not_share_a_file_and_same_station_stays_stable():
    one = station_scoped_path("/data/task", "场站甲", "/data/task/2026/日前.xlsx")
    two = station_scoped_path("/data/task", "场站乙", "/data/task/2026/日前.xlsx")
    again = station_scoped_path("/data/task", "场站甲", "/data/task/2026/日前.xlsx")
    assert one != two
    assert one == again
    assert one.endswith("场站甲/2026/日前.xlsx")


def test_one_station_failure_does_not_stop_the_rest_unless_the_user_cancels():
    continued = station_followup(
        user_cancelled=False, station_failed=True, more_stations=True,
    )
    stopped = station_followup(
        user_cancelled=True, station_failed=True, more_stations=True,
    )
    assert continued == "continue"
    assert stopped == "stop"
    assert should_reuse_login(station_count=2, auth_state="authenticated") is False
    assert login_page_can_be_skipped(True, "authenticated") is False
    runner = TaskRunner({"name": "t"})
    runner._record_failure("登录失败")
    assert runner._failed is True
    assert runner._stopped is False


def test_ukey_run_lock_is_exclusive():
    assert acquire_ukey_run() is True
    try:
        assert acquire_ukey_run() is False
    finally:
        release_ukey_run()
    assert acquire_ukey_run() is True
    release_ukey_run()
