"""场站选择：指定场站时不弹窗，多个场站才交给窗口。"""

from __future__ import annotations

import json

import pytest

from spider_gd.core.station_picker import (
    StationPickError,
    _dialog_payload,
    choose_task_stations,
    pick_station,
    pick_stations,
)
from spider_gd.core.station_profile import (
    StationProfile,
    demo_station_profiles,
    rows_for_inserted_ukeys,
)
from spider_gd.ui.station_picker_dialog import main


@pytest.mark.unit
def test_named_choice_skips_the_window():
    picked = pick_station(demo_station_profiles(), choice="阳西海上风电", opener=_fail_if_called)
    assert picked is not None
    assert picked.wifi_ssid == "YX-UKEY"


@pytest.mark.unit
def test_single_station_skips_the_window_until_forced():
    only = [demo_station_profiles()[0]]
    picked = pick_station(only, opener=_fail_if_called)
    assert picked is not None
    assert picked.id == "demo-yangjiang"

    def opener(profiles: list[StationProfile], timeout_sec: float) -> str | None:
        assert len(profiles) == 1
        assert timeout_sec == 5
        return profiles[0].id

    forced = pick_station(only, opener=opener, force_window=True, timeout_sec=5)
    assert forced is not None
    assert forced.id == "demo-yangjiang"


@pytest.mark.unit
def test_multiple_stations_use_the_window_and_can_cancel():
    def opener(profiles: list[StationProfile], timeout_sec: float) -> str | None:
        assert [item.name for item in profiles] == ["阳江明阳海上风电", "阳西海上风电"]
        assert timeout_sec == 9
        return "demo-yangxi"

    picked = pick_station(demo_station_profiles(), opener=opener, timeout_sec=9)
    assert picked is not None
    assert picked.id == "demo-yangxi"
    assert pick_station(demo_station_profiles(), opener=lambda _profiles, _timeout: None) is None


@pytest.mark.unit
def test_unknown_choice_raises_without_opening_a_window():
    with pytest.raises(StationPickError, match="不存在"):
        pick_station(demo_station_profiles(), choice="不存在", opener=_fail_if_called)


@pytest.mark.unit
def test_dialog_payload_omits_passwords_and_choice_flag_writes_result(tmp_path):
    payload = _dialog_payload(demo_station_profiles())
    assert "demo-yj-pin" not in json.dumps(payload, ensure_ascii=False)
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    code = main(["--input", str(request), "--output", str(result), "--choice", "阳西海上风电"])
    assert code == 0
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "id": "demo-yangxi",
        "cancelled": False,
    }


@pytest.mark.unit
def test_two_ukeys_use_configured_station_names_and_checkbox_order(tmp_path):
    bound = StationProfile("yj", "阳江明阳", "YJ-UKEY", "wifi", "pin", "SN-YJ")
    rows, popup = rows_for_inserted_ukeys(
        [bound],
        [("SN-YJ", "USB 输入设备"), ("SN-OTHER", "USB 输入设备")],
    )
    assert popup is True
    assert rows[0].name == "阳江明阳"
    assert rows[1].id.startswith("usb:")

    def opener(profiles: list[StationProfile], timeout_sec: float) -> list[str] | None:
        assert timeout_sec == 180
        return [profiles[1].id, profiles[0].id]

    plan = choose_task_stations(
        [bound],
        [("SN-YJ", "USB 输入设备"), ("SN-OTHER", "USB 设备")],
        fallback_cert_password="global",
        opener=opener,
    )
    assert plan is not None
    assert [item["name"] for item in plan] == ["USB 设备", "阳江明阳"]
    assert plan[1]["cert_password"] == "pin"
    assert plan[0]["cert_password"] == "global"
    cancelled = choose_task_stations(
        [bound],
        [("SN-YJ", "USB 输入设备"), ("SN-OTHER", "USB 设备")],
        opener=lambda _profiles, _timeout: None,
    )
    assert cancelled is None

    payload = _dialog_payload(rows, multiple=True)
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    code = main([
        "--input", str(request),
        "--output", str(result),
        "--choice", "阳江明阳,USB 输入设备",
    ])
    assert code == 0
    saved = json.loads(result.read_text(encoding="utf-8"))
    assert saved["ids"] == ["yj", rows[1].id]


@pytest.mark.unit
def test_one_bound_ukey_skips_the_window():
    bound = StationProfile("yj", "阳江明阳", "YJ-UKEY", "", "pin", "SN-YJ")
    plan = choose_task_stations(
        [bound],
        [("SN-YJ", "USB 输入设备")],
        fallback_cert_password="global",
        opener=_fail_multi,
    )
    assert plan == [{
        "id": "yj",
        "name": "阳江明阳",
        "wifi_ssid": "YJ-UKEY",
        "wifi_password": "",
        "cert_password": "pin",
        "device_id": "SN-YJ",
        "org_name": "",
    }]
    ordered = pick_stations(
        demo_station_profiles(),
        choice="阳西海上风电,阳江明阳海上风电",
    )
    assert ordered is not None
    assert ordered[0].id == "demo-yangxi"


def _fail_multi(profiles: list[StationProfile], timeout_sec: float) -> list[str] | None:
    raise AssertionError(f"不应打开窗口：{len(profiles)} 个场站，等待 {timeout_sec} 秒")


def _fail_if_called(profiles: list[StationProfile], timeout_sec: float) -> str | None:
    raise AssertionError(f"不应打开窗口：{len(profiles)} 个场站，等待 {timeout_sec} 秒")
