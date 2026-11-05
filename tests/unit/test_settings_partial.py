"""设置只更新提交的键，掩码不能覆盖真密码。"""

from __future__ import annotations

from pathlib import Path

import pytest

from spider_gd.gateway.settings_update import filter_settings_update
from spider_gd.infrastructure.storage.database import TaskStateStore


@pytest.mark.unit
def test_filter_drops_mask_but_keeps_explicit_clear():
    kept = filter_settings_update({
        "siteUrl": "https://pm.gd.csg.cn",
        "certPassword": "******",
        "accountPassword": "",
        "wifiPassword": "new-wifi",
    })
    assert kept == {
        "siteUrl": "https://pm.gd.csg.cn",
        "accountPassword": "",
        "wifiPassword": "new-wifi",
    }


@pytest.mark.unit
def test_save_settings_updates_only_provided_keys(tmp_path: Path):
    store = TaskStateStore(tmp_path / "spider.db")
    store.save_settings({
        "siteUrl": "https://old.example",
        "certPassword": "real-pin",
        "downloadDir": r"D:\data",
    })
    patch = filter_settings_update({
        "siteUrl": "https://pm.gd.csg.cn",
        "certPassword": "******",
    })
    store.save_settings(patch)
    assert store.get_setting("siteUrl") == "https://pm.gd.csg.cn"
    assert store.get_setting("certPassword") == "real-pin"
    assert store.get_setting("downloadDir") == r"D:\data"
    store.close()
