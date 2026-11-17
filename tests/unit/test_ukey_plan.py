"""任务选钥匙：编辑看全部场站，执行时才看插没插。"""

from __future__ import annotations

import json

import pytest

from spider_gd.core.station_profile import StationProfile
from spider_gd.core.ukey_plan import (
    KnownCert,
    editor_ukey_options,
    kept_task_ukeys,
    plan_task_execution,
    select_available_cert,
)
from spider_gd.infrastructure.network.usb_devices import parse_pnp_devices
from spider_gd.infrastructure.network.wifi_wait import wait_for_saved_ssid
from spider_gd.infrastructure.storage.database import TaskStateStore
from spider_gd.infrastructure.windows.cert_dialog import CertDialogRow, confirm_certificate, match_dialog_row


def _stations() -> list[StationProfile]:
    return [
        StationProfile("yunyang", "蕴阳海风场", "YY", device_id="SN-YY", org_name="蕴阳"),
        StationProfile("yangjiang", "阳江海风", "YJ", device_id="SN-YJ", org_name="阳江"),
    ]


def test_editor_lists_saved_stations_even_when_nothing_is_plugged():
    options = editor_ukey_options(_stations())
    assert [item["id"] for item in options] == ["yunyang", "yangjiang"]
    assert options[0]["orgName"] == "蕴阳"
    assert "SN-YY" not in json.dumps(options, ensure_ascii=False)
    assert kept_task_ukeys(["yunyang", "yangjiang"], _stations()) == ["yunyang", "yangjiang"]


def test_saved_selection_is_not_dropped_because_a_key_is_unplugged():
    saved = ["yunyang", "yangjiang"]
    assert kept_task_ukeys(saved, _stations()) == saved


def test_selected_missing_key_is_skipped_and_not_replaced():
    certs = [KnownCert("aa", "蕴阳海风发电有限公司", True)]
    plan = plan_task_execution(
        _stations(),
        selected_ids=["yunyang", "yangjiang"],
        present_device_ids=["SN-YY"],
        certs=certs,
        mode="manual",
        opener=lambda *_args: pytest.fail("已选场站不应再弹窗"),
    )
    assert plan.started is True
    assert [item["id"] for item in plan.runnable] == ["yunyang"]
    assert plan.skipped[0].name == "阳江海风"
    assert plan.runnable[0]["org_name"] == "蕴阳"


def test_selected_but_none_match_does_not_start():
    plan = plan_task_execution(
        _stations(),
        selected_ids=["yunyang", "yangjiang"],
        present_device_ids=[],
        certs=[KnownCert("aa", "别的公司", True)],
        mode="scheduled",
    )
    assert plan.started is False
    assert plan.runnable == []
    assert len(plan.skipped) == 2


def test_manual_without_selection_opens_only_for_multiple_available():
    certs = [
        KnownCert("aa", "蕴阳公司", True),
        KnownCert("bb", "阳江公司", True),
    ]
    seen: list[str] = []

    def opener(rows, _timeout):
        seen.extend(item.id for item in rows)
        return ["yangjiang"]

    plan = plan_task_execution(
        _stations(),
        selected_ids=[],
        present_device_ids=["SN-YY", "SN-YJ"],
        certs=certs,
        mode="manual",
        opener=opener,
    )
    assert seen == ["yunyang", "yangjiang"]
    assert [item["id"] for item in plan.runnable] == ["yangjiang"]


def test_manual_without_selection_runs_the_only_available_key():
    plan = plan_task_execution(
        _stations(),
        selected_ids=[],
        present_device_ids=["SN-YY"],
        certs=[KnownCert("aa", "蕴阳公司", True)],
        mode="manual",
        opener=lambda *_args: pytest.fail("只有一把可跑时不应弹窗"),
    )
    assert [item["name"] for item in plan.runnable] == ["蕴阳海风场"]


def test_scheduled_without_selection_runs_all_available_without_a_dialog():
    plan = plan_task_execution(
        _stations(),
        selected_ids=[],
        present_device_ids=["SN-YY", "SN-YJ"],
        certs=[
            KnownCert("aa", "蕴阳公司", True),
            KnownCert("bb", "阳江公司", True),
        ],
        mode="scheduled",
        opener=lambda *_args: pytest.fail("定时不能弹窗"),
    )
    assert [item["name"] for item in plan.runnable] == ["蕴阳海风场", "阳江海风"]


def test_unselected_and_nothing_ready_keeps_the_original_run():
    plan = plan_task_execution(
        _stations(),
        selected_ids=[],
        present_device_ids=[],
        certs=[],
        mode="scheduled",
        certs_checked=False,
    )
    assert plan.started is True
    assert plan.runnable == []


def test_public_task_uses_the_same_rule():
    plan = plan_task_execution(
        _stations(),
        selected_ids=["yunyang", "yangjiang"],
        present_device_ids=["SN-YY", "SN-YJ"],
        certs=[
            KnownCert("aa", "蕴阳公司", True),
            KnownCert("bb", "阳江公司", True),
        ],
        mode="scheduled",
    )
    assert plan.started is True
    assert len(plan.runnable) == 2


def test_cached_certificate_is_not_available():
    cert, reason = select_available_cert(
        [KnownCert("aa", "蕴阳公司", False)],
        "蕴阳",
    )
    assert cert is None
    assert "不在当前钥匙" in reason


def test_same_org_without_thumbprint_is_ambiguous():
    cert, reason = select_available_cert(
        [
            KnownCert("aa", "蕴阳公司", True),
            KnownCert("bb", "蕴阳公司", True),
        ],
        "蕴阳",
    )
    assert cert is None
    assert "多张" in reason
    chosen, ok = select_available_cert(
        [
            KnownCert("aa", "蕴阳公司", True),
            KnownCert("bb", "蕴阳公司", True),
        ],
        "蕴阳",
        "bb",
    )
    assert ok == ""
    assert chosen is not None and chosen.thumbprint == "bb"


def test_expired_or_unmatched_dialog_does_not_pick_a_row():
    rows = [
        CertDialogRow("甲", "三峡新能源阳江发电有限公司", "2026-01-01", "NETCA"),
        CertDialogRow("乙", "三峡新能源发电（韶关浈江）有...", "2027-01-01", "NETCA"),
    ]
    assert match_dialog_row(rows, "浈江").row_index == 1
    assert match_dialog_row(rows, "新能源").row_index is None
    assert match_dialog_row(rows, "不存在").row_index is None
    assert confirm_certificate("阳江").needs_windows is True


def test_usb_and_hid_of_one_key_collapse_but_two_keys_stay_two():
    raw = json.dumps([
        {
            "FriendlyName": "USB 输入设备",
            "InstanceId": r"USB\VID_096E&PID_0317\6&11111111&0&1",
            "Parent": r"USB\ROOT_HUB30\4",
        },
        {
            "FriendlyName": "符合 HID 标准的供应商定义设备",
            "InstanceId": r"HID\VID_096E&PID_0317\7&22222222&0&2",
            "Class": "HIDClass",
            "Parent": r"USB\VID_096E&PID_0317\6&11111111&0&1",
        },
        {
            "FriendlyName": "USB 输入设备",
            "InstanceId": r"USB\VID_096E&PID_0317\6&33333333&0&3",
            "Parent": r"USB\ROOT_HUB30\4",
        },
        {
            "FriendlyName": "符合 HID 标准的供应商定义设备",
            "InstanceId": r"HID\VID_096E&PID_0317\7&44444444&0&4",
            "Class": "HIDClass",
            "Parent": r"USB\VID_096E&PID_0317\6&33333333&0&3",
        },
    ])
    ukeys = [item for item in parse_pnp_devices(raw) if item.ukey_candidate]
    assert len(ukeys) == 2


def test_wifi_wait_backs_off_and_stops_at_the_deadline():
    now = {"t": 0.0}
    sleeps: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    result = wait_for_saved_ssid(
        "YY",
        visible=lambda: [],
        connect=lambda: False,
        timeout_sec=5,
        clock=clock,
        sleeper=sleeper,
    )
    assert result.ok is False
    assert result.status == "missing"
    assert sleeps
    assert sleeps[-1] <= 8
    assert now["t"] <= 5.1

    now["t"] = 0
    result = wait_for_saved_ssid(
        "YY",
        visible=lambda: [],
        connect=lambda: False,
        timeout_sec=30,
        clock=clock,
        sleeper=sleeper,
        cancelled=lambda: True,
    )
    assert result.status == "cancelled"


def test_old_task_without_ukey_field_still_loads(tmp_path):
    store = TaskStateStore(tmp_path / "state.db")
    store.save_task_config({"id": "old", "name": "旧任务", "enabled": True})
    loaded = store.get_task_config("old")
    assert loaded is not None
    assert loaded["ukey_station_ids"] == []
    store.save_task_config({
        "id": "old",
        "name": "旧任务",
        "ukey_station_ids": ["yunyang", "yangjiang"],
    })
    again = store.get_task_config("old")
    assert again is not None
    assert again["ukey_station_ids"] == ["yunyang", "yangjiang"]
    store.close()
