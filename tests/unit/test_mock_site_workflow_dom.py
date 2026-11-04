from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient


def _load_mock_app():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "mock_site" / "main.py"
    spec = importlib.util.spec_from_file_location("spider_gd_mock_site_main", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def test_portal_mock_matches_login_workflow_selectors():
    client = TestClient(_load_mock_app())
    resp = client.get("/portal/")
    html = resp.text

    assert resp.status_code == 200
    assert "登 录" in html
    assert "证书登录" in html
    assert "证书密码 / PIN" in html
    assert "validateOnly_verifyCode" in html
    assert "立即登录" in html
    assert "/pfxh/stc/check?code=mock" in html
    assert "/pfjs/stc/check?code=mock" in html
    assert "/pfjy/calendar?code=mock" in html
    assert ">中长期交易</a>" in html
    assert "中长期交易（灾备）" in html


def test_spot_mock_matches_verified_workflow_selectors():
    client = TestClient(_load_mock_app())
    resp = client.get("/pfxh/stc/check?code=mock")
    html = resp.text

    assert resp.status_code == 200
    for needle in [
        "el-submenu__title",
        "组织中心",
        "披露中心",
        "日前节点电价查询",
        "实时节点电价查询",
        "style-box-head",
        "exportBtnColor",
        "dateBox",
        "headBtn",
        "head-item",
        "全部导出",
        "elx-table--body",
        "el-date-picker__header-label",
        "el-year-table",
        "el-month-table",
    ]:
        assert needle in html


def test_settlement_mock_matches_verified_workflow_selectors():
    client = TestClient(_load_mock_app())
    resp = client.get("/pfjs/stc/check?code=mock")
    html = resp.text

    assert resp.status_code == 200
    for needle in [
        "账单中心",
        "日清算概览",
        "el-submenu__title",
        "el-menu-item",
        "el-date-editor",
        "el-input__inner",
        "查询",
        "导出",
        "exportBtnColor",
        "el-date-picker__header-label",
    ]:
        assert needle in html


def test_trading_calendar_mock_embeds_captured_week_and_picker_contract():
    client = TestClient(_load_mock_app())
    resp = client.get("/pfjy/calendar")
    html = resp.text

    assert resp.status_code == 200
    for needle in [
        "calendar-box",
        "calendar-head-box",
        "el-radio-button",
        ">周</span>",
        "el-date-editor--week",
        "fc-dayGridWeek-view",
        "fc-daygrid-day",
        "calendar-week",
        "titleOmit",
        "typeSizeFormal",
        "标的时间",
        "申报时间",
        "legend-icon",
        "el-picker-panel",
        "el-date-table",
        "is-week-mode",
        "2026年10月周双边协商交易(2026-10-26)（灾备）",
        "10-26 14:00 ~ 10-26 15:30",
    ]:
        assert needle in html, needle


def test_mock_site_exposes_verified_workflow_api_fallbacks():
    client = TestClient(_load_mock_app())

    spot = client.get("/pfxh/qctc-pm-trade-market-out-plzx/DaMarkjsc/homeXhMarketAllInfo")
    assert spot.status_code == 200
    assert spot.json()["data"]

    settlement = client.post(
        "/pfjs/qctc-pm-jsgl-electricity-month-query/sbs/calDayStatQuery/exportItemsWithoutCalVersion"
    )
    assert settlement.status_code == 200
    assert "spreadsheet" in settlement.headers["content-type"]
