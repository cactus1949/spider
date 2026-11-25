"""Beta runner artifact outputs stay inside the effective home using real local IO."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.dom_probe_fakes import DomProbeFakePage
from workflow_runner import WorkflowExecutionError, WorkflowRunner


@pytest.fixture
def beta_home(tmp_path, monkeypatch):
    home = tmp_path / "beta"
    home.mkdir()
    monkeypatch.setenv("SPIDER_GD_CHANNEL", "beta")
    monkeypatch.setenv("SPIDER_GD_HOME", str(home))
    return home


class ArtifactPage(DomProbeFakePage):
    url = "https://example.invalid/report"

    async def screenshot(self, *, path, **kwargs):
        Path(path).write_bytes(b"screenshot")

    async def content(self):
        return "<html>diagnostic</html>"

    async def title(self):
        return "Report"

    async def evaluate(self, script, arg=None):
        if arg == "table":
            return {"rows": [["price"], [42]], "maxCols": 1}
        if "localStorage" in script:
            return [] if "out.push" in script else ""
        return await super().evaluate(script, arg)


def workflow(home, **kwargs):
    runner = WorkflowRunner({"name": "artifact", "steps": []}, download_dir=str(home), **kwargs)
    runner.pages = [ArtifactPage()]
    runner._executing_step_index = 0
    runner._pointer_settle_s = 0
    return runner


@pytest.mark.parametrize("linked", [False, True])
def test_beta_workflow_constructor_rejects_external_download_directory(
    beta_home, tmp_path, linked,
):
    outside = tmp_path / "outside"
    download_dir = outside / "downloads"
    link = beta_home / "linked"
    if linked:
        link.symlink_to(outside, target_is_directory=True)
        download_dir = link / "downloads"
    with pytest.raises(ValueError, match="测试版"):
        WorkflowRunner({"steps": []}, download_dir=str(download_dir))
    assert not outside.exists()
    if linked:
        assert link.is_symlink()


@pytest.mark.parametrize("linked", [False, True])
async def test_beta_extract_rejects_external_output_directory_before_mkdir(
    beta_home, tmp_path, linked,
):
    runner = workflow(beta_home)
    outside = tmp_path / "outside"
    output = outside / "new"
    if linked:
        link = beta_home / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output = link / "new"
    runner._active_output_dir = str(output)
    with pytest.raises(ValueError, match="测试版"):
        await runner.exec_step({"action": "extract", "save_as": "report.json"})
    assert not outside.exists()


@pytest.mark.parametrize("custom", [False, True])
async def test_beta_extract_saves_readable_versioned_or_overwritten_output(beta_home, custom):
    runner = workflow(beta_home, custom_filenames={"s0": "custom"} if custom else {})
    await runner.exec_step({"action": "extract", "save_as": "report.json"})
    target = beta_home / ("custom.json" if custom else "report.json")
    assert json.loads(target.read_text()) == {}
    assert (beta_home / ".file_versions.json").exists() == (not custom)


@pytest.mark.parametrize("custom", [False, True])
async def test_beta_extract_rejects_final_leaf_symlink(beta_home, tmp_path, custom):
    runner = workflow(beta_home, custom_filenames={"s0": "custom"} if custom else {})
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"keep")
    target = beta_home / ("custom.json" if custom else "report.json")
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="测试版"):
        await runner.exec_step({"action": "extract", "save_as": "report.json"})
    assert outside.read_bytes() == b"keep"
    assert target.is_symlink()


@pytest.mark.parametrize("fmt", ["csv", "json"])
@pytest.mark.parametrize("custom", [False, True])
async def test_beta_table_rechecks_suffix_changed_final_leaf(beta_home, tmp_path, fmt, custom):
    runner = workflow(beta_home, custom_filenames={"s0": "custom"} if custom else {})
    outside = tmp_path / f"outside.{fmt}"
    outside.write_bytes(b"keep")
    target = beta_home / f"{'custom' if custom else 'report'}.{fmt}"
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="测试版"):
        await runner.exec_step({
            "action": "extract_table", "selector": "table", "format": fmt,
            "save_as": "report.txt",
        })
    assert outside.read_bytes() == b"keep"
    assert target.is_symlink()
    assert sorted(p.name for p in beta_home.iterdir()) == [target.name]


@pytest.mark.parametrize("fmt", ["csv", "json"])
async def test_beta_table_saves_readable_suffix_changed_outputs(beta_home, fmt):
    runner = workflow(beta_home)
    await runner.exec_step({
        "action": "extract_table", "selector": "table", "format": fmt,
        "save_as": "report.txt",
    })
    target = beta_home / f"report.{fmt}"
    if fmt == "json":
        assert json.loads(target.read_text()) == [["price"], [42]]
    else:
        assert target.read_text(encoding="utf-8-sig") == "price\n42\n"
    manifest = json.loads((beta_home / ".file_versions.json").read_text())
    assert manifest["files"][target.name]["current_sha256"]


@pytest.mark.parametrize("custom", [False, True])
def test_beta_workbook_rejects_random_staging_symlink(beta_home, tmp_path, monkeypatch, custom):
    monkeypatch.setattr("workflow_runner.time.time_ns", lambda: 123)
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"keep")
    tmp = beta_home / f".{'custom' if custom else 'report'}.123.xlsx"
    tmp.symlink_to(outside)
    with pytest.raises(ValueError, match="测试版"):
        WorkflowRunner._save_data_as_xlsx(
            [{"price": 42}], beta_home / f"{'custom' if custom else 'report'}.xlsx",
            overwrite=custom,
        )
    assert outside.read_bytes() == b"keep"
    assert tmp.is_symlink()
    assert sorted(p.name for p in beta_home.iterdir()) == [tmp.name]


@pytest.mark.parametrize("writer", ["text", "workbook", "api"])
def test_beta_generated_overwrite_rejects_external_final_output(beta_home, tmp_path, writer):
    runner = workflow(beta_home)
    runner._overwrite_current = True
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    with pytest.raises(ValueError, match="测试版"):
        if writer == "text":
            WorkflowRunner._save_versioned_text(outside, "new", overwrite=True)
        elif writer == "workbook":
            WorkflowRunner._save_data_as_xlsx([{"price": 42}], outside, overwrite=True)
        else:
            runner._save_api_fallback_result({
                "base64": "bmV3", "contentType": "application/octet-stream",
            }, outside)
    assert outside.read_bytes() == b"keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["beta", "outside.txt"]


@pytest.mark.parametrize("overwrite", [False, True])
def test_beta_workbook_rejects_final_leaf_before_staging(beta_home, tmp_path, overwrite):
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"keep")
    target = beta_home / "report.xlsx"
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="测试版"):
        WorkflowRunner._save_data_as_xlsx([{"price": 42}], target, overwrite=overwrite)
    assert outside.read_bytes() == b"keep"
    assert sorted(p.name for p in beta_home.iterdir()) == [target.name]
    assert target.is_symlink()


@pytest.mark.parametrize("overwrite", [False, True])
def test_beta_workbook_saves_readable_output(beta_home, overwrite):
    from openpyxl import load_workbook

    target = beta_home / "data" / "report.xlsx"
    WorkflowRunner._save_data_as_xlsx([{"price": 42}], target, overwrite=overwrite)
    wb = load_workbook(target)
    assert list(wb.active.values) == [("price",), (42,)]
    wb.close()
    assert (target.parent / ".file_versions.json").exists() == (not overwrite)


async def save_diagnostic(runner, kind):
    if kind == "screenshot":
        await runner.exec_step({"action": "screenshot", "name": "capture.png"})
    elif kind == "failure_screenshot":
        return await runner._capture_failure_screenshot("failed")
    elif kind == "trace":
        runner._trace_browser_actions = True
        runner._write_action_trace(kind="test", phase="capture")
    elif kind == "html":
        return await runner._save_page_html("page")
    elif kind == "page_log":
        return await runner._dump_page_diagnostics(1)
    else:
        return await runner._dump_login_diagnostics("test failure")


DIAGNOSTICS = {
    "screenshot": "capture.png",
    "failure_screenshot": "_failures/failed_fixed.png",
    "trace": "_diagnostics/browser_actions_123.jsonl",
    "html": "_failures/page.html",
    "page_log": "_failures/diagnostics_fixed.log",
    "login_log": "_failures/login_diagnostics_fixed.log",
}


@pytest.mark.parametrize("kind", DIAGNOSTICS)
@pytest.mark.parametrize("escape", ["leaf", "directory"])
async def test_beta_diagnostics_reject_symlinks_before_direct_writers(
    beta_home, tmp_path, monkeypatch, kind, escape,
):
    monkeypatch.setattr("workflow_runner.time.strftime", lambda *args: "fixed")
    monkeypatch.setattr("workflow_runner.time.time_ns", lambda: 123)
    runner = workflow(beta_home)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    target = beta_home / DIAGNOSTICS[kind]
    if escape == "leaf":
        target.parent.mkdir(parents=True, exist_ok=True)
        link = target
        link.symlink_to(sentinel)
    else:
        if target.parent == beta_home:
            runner.download_dir = beta_home / "linked"
            link = runner.download_dir
        else:
            link = target.parent
        link.symlink_to(outside, target_is_directory=True)
    # Best-effort diagnostics may return an empty path; neither outcome may write outside.
    try:
        result = await save_diagnostic(runner, kind)
    except ValueError as exc:
        assert "测试版" in str(exc)
    else:
        if kind == "trace":
            assert result is None
        else:
            assert result == ""
    assert sentinel.read_bytes() == b"keep"
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel"]
    assert link.is_symlink()


@pytest.mark.parametrize("kind", DIAGNOSTICS)
async def test_beta_diagnostics_remain_readable_inside_home(beta_home, monkeypatch, kind):
    monkeypatch.setattr("workflow_runner.time.strftime", lambda *args: "fixed")
    monkeypatch.setattr("workflow_runner.time.time_ns", lambda: 123)
    runner = workflow(beta_home)
    await save_diagnostic(runner, kind)
    target = beta_home / DIAGNOSTICS[kind]
    data = target.read_bytes()
    assert data
    if "screenshot" in kind:
        assert data == b"screenshot"
    elif kind == "trace":
        assert json.loads(data)["kind"] == "test"
    elif kind == "html":
        assert data == b"<html>diagnostic</html>"


@pytest.mark.parametrize("name_kind", ["absolute", "traversal"])
async def test_beta_screenshot_rejects_external_names(beta_home, tmp_path, name_kind):
    runner = workflow(beta_home)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    name = str(outside) if name_kind == "absolute" else "../outside.png"
    with pytest.raises(ValueError, match="测试版"):
        await runner.exec_step({"action": "screenshot", "name": name})
    assert outside.read_bytes() == b"keep"


class LocalDownload:
    def __init__(self, filename):
        self.suggested_filename = filename

    async def save_as(self, path):
        Path(path).write_bytes(b"download")


class DownloadEvent:
    def __init__(self, download):
        self.value = asyncio.get_running_loop().create_future()
        self.value.set_result(download)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class AttachmentButtons:
    async def count(self):
        return 1

    def nth(self, index):
        return self

    async def click(self):
        pass


class DownloadPage(ArtifactPage):
    def __init__(self, filename):
        super().__init__()
        self.download = LocalDownload(filename)

    def expect_download(self, **kwargs):
        return DownloadEvent(self.download)

    def probe_visible(self, selector, index=None):
        return True

    def locator(self, selector):
        return AttachmentButtons()


EXPORT_STEP = {"action": "export", "query_settle_ms": 0, "timeout": 1000}


@pytest.mark.parametrize("naming", ["site", "configured", "extension", "custom"])
async def test_beta_export_rejects_final_leaf_before_staging(beta_home, tmp_path, naming):
    custom = {"s0": "custom"} if naming == "custom" else {}
    runner = workflow(beta_home, custom_filenames=custom)
    original = "site.zip" if naming == "extension" else "site.xlsx"
    runner.pages = [DownloadPage(original)]
    save_as = "" if naming == "site" else "report.xlsx"
    leaf = {
        "site": "site.xlsx", "configured": "report.xlsx",
        "extension": "report.zip", "custom": "custom.xlsx",
    }[naming]
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    target = beta_home / leaf
    target.symlink_to(outside)
    with pytest.raises(WorkflowExecutionError, match="测试版"):
        await runner.exec_step({**EXPORT_STEP, "save_as": save_as})
    assert outside.read_bytes() == b"keep"
    assert target.is_symlink()
    assert sorted(p.name for p in beta_home.iterdir()) == [leaf]


async def test_beta_export_rejects_random_staging_symlink(beta_home, tmp_path, monkeypatch):
    monkeypatch.setattr("workflow_runner.time.time_ns", lambda: 123)
    runner = workflow(beta_home)
    runner.pages = [DownloadPage("site.xlsx")]
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    tmp = beta_home / ".report.xlsx.123.tmp"
    tmp.symlink_to(outside)
    with pytest.raises(WorkflowExecutionError, match="测试版"):
        await runner.exec_step({**EXPORT_STEP, "save_as": "report.xlsx"})
    assert outside.read_bytes() == b"keep"
    assert tmp.is_symlink()
    assert sorted(p.name for p in beta_home.iterdir()) == [tmp.name]


@pytest.mark.parametrize("custom", [False, True])
async def test_beta_export_saves_local_download_and_metadata(beta_home, custom):
    runner = workflow(beta_home, custom_filenames={"s0": "custom"} if custom else {})
    runner.pages = [DownloadPage("site.xlsx")]
    await runner.exec_step({**EXPORT_STEP, "save_as": "report.xlsx"})
    target = beta_home / ("custom.xlsx" if custom else "report.xlsx")
    assert target.read_bytes() == b"download"
    assert (beta_home / ".file_versions.json").exists() == (not custom)
    assert not list(beta_home.glob("*.tmp"))


@pytest.mark.parametrize("escape", ["base", "leaf", "staging"])
async def test_beta_attachment_rejects_escape_before_download_writer(
    beta_home, tmp_path, monkeypatch, escape,
):
    monkeypatch.setattr("workflow_runner.time.time_ns", lambda: 123)
    runner = workflow(beta_home)
    runner.pages = [DownloadPage("report.pdf")]
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    base = outside if escape == "base" else beta_home
    if escape != "base":
        name = "report.pdf" if escape == "leaf" else ".report.pdf.123.tmp"
        link = beta_home / name
        link.symlink_to(sentinel)
    with pytest.raises(ValueError, match="测试版"):
        await runner._save_attachment_buttons(base, 1000)
    assert sentinel.read_bytes() == b"keep"
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel"]
    if escape != "base":
        assert link.is_symlink()
        assert sorted(p.name for p in beta_home.iterdir()) == [name]


async def test_beta_attachment_saves_readable_download_and_metadata(beta_home):
    runner = workflow(beta_home)
    runner.pages = [DownloadPage("report.pdf")]
    assert await runner._save_attachment_buttons(beta_home, 1000) == 1
    assert (beta_home / "report.pdf").read_bytes() == b"download"
    manifest = json.loads((beta_home / ".file_versions.json").read_text())
    assert manifest["files"]["report.pdf"]["current_sha256"]


async def test_beta_notice_download_rejects_output_base_before_mkdir(beta_home, tmp_path):
    runner = workflow(beta_home)
    outside = tmp_path / "outside"
    runner._active_output_dir = str(outside / "new")
    with pytest.raises(ValueError, match="测试版"):
        await runner.exec_step({"action": "download_notice_reports"})
    assert not outside.exists()


def _api_workbook_bytes():
    import io
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["price"])
    wb.active.append([42])
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    return output.getvalue()


class ApiPage(ArtifactPage):
    def __init__(self, result):
        super().__init__()
        self.result = result

    async def evaluate(self, script, arg=None):
        if isinstance(arg, dict) and "jsonBody" in arg:
            return {"ok": True, "status": 200, **self.result}
        return await super().evaluate(script, arg)


@pytest.mark.parametrize("custom", [False, True])
async def test_beta_api_fallback_rechecks_suffix_changed_leaf(beta_home, tmp_path, custom):
    runner = workflow(beta_home, custom_filenames={"s0": "custom"} if custom else {})
    runner.config["api_fallback"] = {"url": "/local-fake"}
    import base64

    raw = _api_workbook_bytes()
    runner.pages = [ApiPage({"contentType": "application/octet-stream",
                             "base64": base64.b64encode(raw).decode()})]
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"keep")
    target = beta_home / ("custom.xlsx" if custom else "report.xlsx")
    target.symlink_to(outside)
    assert not await runner._try_api_fallback("report.txt", RuntimeError("no download"))
    assert outside.read_bytes() == b"keep"
    assert target.is_symlink()
    assert sorted(p.name for p in beta_home.iterdir()) == [target.name]


@pytest.mark.parametrize("custom", [False, True])
async def test_beta_api_fallback_saves_readable_binary_output(beta_home, custom):
    runner = workflow(beta_home, custom_filenames={"s0": "custom"} if custom else {})
    runner.config["api_fallback"] = {"url": "/local-fake"}
    import base64

    raw = _api_workbook_bytes()
    runner.pages = [ApiPage({"contentType": "application/octet-stream",
                             "base64": base64.b64encode(raw).decode()})]
    assert await runner._try_api_fallback("report.txt", RuntimeError("no download"))
    target = beta_home / ("custom.xlsx" if custom else "report.xlsx")
    assert target.read_bytes() == raw
    assert (beta_home / ".file_versions.json").exists() == (not custom)
