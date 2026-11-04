"""场站绑定：JSON、密码遮盖和示例窗口数据。"""

from __future__ import annotations

import json

import pytest

from spider_gd.core.station_profile import (
    StationProfile,
    dump_station_profiles,
    find_station,
    mask_station_profiles,
    merge_station_profiles,
    parse_station_profiles,
    profiles_for_window,
    public_station,
)


@pytest.mark.unit
def test_parse_drops_blank_names_and_rejects_duplicates():
    profiles = parse_station_profiles(
        json.dumps(
            [
                {"name": "  "},
                {"id": "a", "name": "阳江", "wifi_ssid": "YJ", "cert_password": "pin"},
            ],
            ensure_ascii=False,
        )
    )
    assert [(item.id, item.name, item.wifi_ssid, item.cert_password) for item in profiles] == [
        ("a", "阳江", "YJ", "pin")
    ]
    with pytest.raises(ValueError, match="重复"):
        parse_station_profiles(
            '[{"name":"阳江"},{"name":"阳江","wifiSsid":"YJ-2"}]'
        )


@pytest.mark.unit
def test_merge_keeps_untyped_passwords_and_drops_removed_rows():
    saved = dump_station_profiles(
        [
            StationProfile("a", "阳江", "YJ", "wifi", "pin"),
            StationProfile("b", "阳西", "YX", "wifi-2", "pin-2"),
        ]
    )
    edited = json.dumps(
        [
            {
                "id": "a",
                "name": "阳江一场",
                "wifiSsid": "YJ-2",
                "wifiPassword": "******",
                "certPassword": "",
            }
        ],
        ensure_ascii=False,
    )
    merged = parse_station_profiles(merge_station_profiles(edited, saved))
    assert len(merged) == 1
    assert merged[0].name == "阳江一场"
    assert merged[0].wifi_ssid == "YJ-2"
    assert merged[0].wifi_password == "wifi"
    assert merged[0].cert_password == "pin"


@pytest.mark.unit
def test_mask_hides_secrets_from_public_view():
    raw = dump_station_profiles([StationProfile("a", "阳江", "YJ", "wifi-secret", "pin-secret")])
    masked = mask_station_profiles(raw)
    assert "wifi-secret" not in masked
    assert "pin-secret" not in masked
    assert public_station(parse_station_profiles(raw)[0]) == {
        "id": "a",
        "name": "阳江",
        "wifiSsid": "YJ",
    }
    assert find_station(parse_station_profiles(raw), "阳江").wifi_ssid == "YJ"  # type: ignore[union-attr]


@pytest.mark.unit
def test_window_preview_uses_demo_until_two_stations_exist():
    shown, used_demo = profiles_for_window(
        [StationProfile("a", "只有一个", "ONE")],
        force_window=True,
    )
    assert used_demo is True
    assert [item.name for item in shown] == ["阳江明阳海上风电", "阳西海上风电"]
    kept, used_demo = profiles_for_window(shown, force_window=True)
    assert used_demo is False
    assert kept == shown
