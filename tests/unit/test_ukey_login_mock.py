"""多 UKey 假登录：页面在浏览器里，选择窗口在浏览器外。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _load_mock_app():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "mock_site" / "main.py"
    spec = importlib.util.spec_from_file_location("spider_gd_mock_ukey_login", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


@pytest.mark.unit
def test_ukey_login_mock_selects_a_station_without_showing_passwords():
    client = TestClient(_load_mock_app())
    page = client.get("/mock/ukey-login")
    assert page.status_code == 200
    assert "立即登录" in page.text
    assert "证书登录" in page.text
    assert "本机窗口" in page.text

    stations = client.get("/mock/ukey-login/stations")
    assert stations.status_code == 200
    body = stations.json()
    assert body["stations"][1]["name"] == "阳西海上风电"
    assert "demo-yx-pin" not in json.dumps(body, ensure_ascii=False)

    picked = client.post("/mock/ukey-login/pick", json={"choice": "阳西海上风电"})
    assert picked.status_code == 200
    selected = picked.json()
    assert selected["ok"] is True
    assert selected["station"] == {
        "id": "demo-yangxi",
        "name": "阳西海上风电",
        "wifiSsid": "YX-UKEY",
    }
    assert "demo-yx-pin" not in json.dumps(selected, ensure_ascii=False)
    assert "demo-yx-wifi" not in json.dumps(selected, ensure_ascii=False)

    missing = client.post("/mock/ukey-login/pick", json={"choice": "不存在的场站"})
    assert missing.status_code == 400
