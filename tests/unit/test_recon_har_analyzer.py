"""测试 HAR 分析与认证提取（侦察工具核心）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from recon_tool.har_analyzer import analyze_har, extract_auth
from recon_tool.packer import pack_dir
from recon_tool.report import build_summary_report


@pytest.fixture
def har_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sample_session.har"


@pytest.mark.unit
def test_analyze_har_detects_export_endpoints(har_path: Path):
    result = analyze_har(har_path)
    export_paths = {e["path"] for e in result["exports"]}
    assert any("exportItemsWithoutCalVersion" in p for p in export_paths)
    assert any("exportDayAhead" in p for p in export_paths)
    # 静态资源与 json 不计入导出
    assert all(e["category"] == "export" for e in result["exports"])


@pytest.mark.unit
def test_analyze_har_classifies_and_dedupes(har_path: Path):
    result = analyze_har(har_path)
    categories = {a["category"] for a in result["raw"]}
    assert {"export", "data", "static"}.issubset(categories)
    assert len(result["deduped"]) == 4


@pytest.mark.unit
def test_extract_auth_finds_tokens_cookies_hdkgccbl(har_path: Path):
    auth = extract_auth(har_path)
    assert "Bearer 11111111-2222-3333" in auth["bearer_tokens"]
    assert auth["cookies"]["SESSION_ID"]
    assert set(auth["special_params"]["HdKGccbL"]) == {"abc123", "def456"}


@pytest.mark.unit
def test_build_summary_report_contains_sections(har_path: Path):
    report = build_summary_report(analyze_har(har_path), extract_auth(har_path))
    assert "# 现场侦察报告" in report
    assert "API 端点" in report
    assert "认证机制" in report
    assert "exportItemsWithoutCalVersion" in report


@pytest.mark.unit
def test_pack_dir_creates_zip(tmp_path: Path):
    src = tmp_path / "recon_test"
    (src / "network").mkdir(parents=True)
    (src / "network" / "info.json").write_text("{}", encoding="utf-8")
    (src / "summary_report.md").write_text("# r", encoding="utf-8")
    zip_path = pack_dir(src)
    assert zip_path.exists()
    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("info.json" in n for n in names)
