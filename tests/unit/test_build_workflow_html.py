"""build_workflow HTML 元数据清洗测试。"""

import json
import os
import sys
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import build_workflow as workshop  # noqa: E402
from build_workflow import _clean_steps  # noqa: E402


def test_clean_steps_preserves_html_metadata():
    raw = [{
        "action": "click",
        "selector": "button:has-text('导出')",
        "_text": "导出",
        "_tag": "button",
        "_html": "<button class='exportBtnColor'>导出</button>",
        "_html_section": "<div class='style-box-head'>...</div>",
        "_html_path": [
            {"tag": "button", "class": "exportBtnColor", "data-v-f28a72d4": ""},
            {"tag": "div", "class": "style-box-head", "data-v-f28a72d4": ""},
        ],
    }]
    steps = _clean_steps(raw, start_url="https://example.com")
    click = next(s for s in steps if s["action"] == "click")
    assert click["_html"].startswith("<button")
    assert "style-box-head" in click["_html_section"]
    assert len(click["_html_path"]) == 2


def test_wait_step_has_no_html_fields():
    raw = [{"action": "wait", "ms": 2000}]
    steps = _clean_steps(raw, start_url="https://example.com")
    wait = next(s for s in steps if s["action"] == "wait")
    assert "_html" not in wait
    assert "_html_section" not in wait
    assert "_html_path" not in wait


@pytest.fixture
def export_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {
        "SPIDER_GD_HOME": str(home),
        "SPIDER_GD_CHANNEL": "beta",
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    return home


@pytest.fixture
def recorded_ui(monkeypatch):
    """Replace only the external recorder/Playwright; keep all file writers real."""
    class Page:
        url = "https://example.invalid/recorded"

        async def goto(self, url, **kwargs):
            self.url = url

        async def title(self):
            return "Recorded page"

        async def evaluate(self, script):
            if script == workshop._BUILDER_JS:
                return None
            if "steps : []" in script:
                return [{"action": "click", "selector": "button#export", "_text": "导出"}]
            if script.startswith("() => { if") or "__spider_show_saved__" in script:
                return None
            if any(key in script for key in (
                "__spider_sidebar__", "_exportRequested", "_finishRequested",
            )):
                return True
            raise AssertionError(f"Unexpected recorder query: {script}")

    async def no_wait(*args, **kwargs):
        pass

    page = Page()
    context = SimpleNamespace(pages=[page], on=lambda *args: None, close=no_wait)
    browser = SimpleNamespace(contexts=[context], on=lambda *args: None, close=no_wait)
    checks = iter([True, True, False])
    browser.is_connected = lambda: next(checks, False)

    @asynccontextmanager
    async def fake_playwright():
        yield object()

    async def connect(_playwright, *, headless):
        return browser, None, None

    api = ModuleType("playwright.async_api")
    api.async_playwright = fake_playwright
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    monkeypatch.setattr(workshop, "connect_or_launch", connect)
    monkeypatch.setattr(workshop.asyncio, "sleep", no_wait)


def _tree_snapshot(root: Path):
    return {
        str(path.relative_to(root)): (
            ("link", str(path.readlink())) if path.is_symlink()
            else ("dir",) if path.is_dir()
            else ("file", path.read_bytes())
        )
        for path in root.rglob("*")
    }


@pytest.mark.unit
@pytest.mark.parametrize("escape", [
    "external-directory", "directory-link", "leaf-link", "dangling-directory",
    "dangling-leaf", "foreign-parent-home-leaf",
])
async def test_build_workflow_beta_rejects_export_escape_before_zip_write(
    tmp_path: Path, export_home: Path, recorded_ui, escape: str,
):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "sentinel").write_bytes(b"foreign sentinel must stay unchanged")
    export_dir = export_home / "exports"
    links = []
    home_leaf = export_home / "existing.zip"
    if escape == "external-directory":
        export_dir = foreign / "new"
    elif escape in {"directory-link", "dangling-directory", "foreign-parent-home-leaf"}:
        link = export_home / "linked"
        destination = foreign / "missing" if escape == "dangling-directory" else foreign
        link.symlink_to(destination, target_is_directory=True)
        links.append((link, link.readlink()))
        export_dir = link if escape == "foreign-parent-home-leaf" else link / "new"
        if escape == "foreign-parent-home-leaf":
            home_leaf.write_bytes(b"existing home ZIP must not be truncated")
            (foreign / "recorded.zip").symlink_to(home_leaf)
    else:
        export_dir.mkdir()
        destination = foreign / "recorded.zip"
        if escape == "leaf-link":
            destination.write_bytes(b"existing foreign ZIP must not be truncated")
        link = export_dir / "recorded.zip"
        link.symlink_to(destination)
        links.append((link, link.readlink()))
    before = _tree_snapshot(foreign)

    with pytest.raises(ValueError, match="测试版"):
        await workshop.build_workflow(
            "https://example.invalid/recorded", export_home / "workflows" / "recorded.yml",
            "recorded", "无需登录", export_dir=export_dir,
        )

    assert _tree_snapshot(foreign) == before
    for link, target in links:
        assert link.is_symlink()
        assert link.readlink() == target
    if escape == "foreign-parent-home-leaf":
        assert home_leaf.read_bytes() == b"existing home ZIP must not be truncated"


@pytest.mark.unit
@pytest.mark.parametrize("channel", ["beta", "stable"])
async def test_build_workflow_exports_readable_real_zip(
    tmp_path: Path, export_home: Path, recorded_ui, monkeypatch, channel: str,
):
    monkeypatch.setenv("SPIDER_GD_CHANNEL", channel)
    export_dir = (export_home if channel == "beta" else tmp_path / "foreign") / "exports"
    output = export_home / "workflows" / "recorded.yml"

    result = await workshop.build_workflow(
        "https://example.invalid/recorded", output, "recorded", "无需登录", export_dir=export_dir,
    )

    target = export_dir / "recorded.zip"
    assert result == [str(target)]
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["recorded.yml", "recorded.min.yml", "recorded.raw.json"]
        assert archive.read("recorded.yml") == output.read_bytes()
        assert archive.read("recorded.min.yml") == output.with_suffix(".min.yml").read_bytes()
        assert archive.read("recorded.raw.json") == output.with_suffix(".raw.json").read_bytes()
        workflow = yaml.safe_load(archive.read("recorded.min.yml"))
        assert workflow["name"] == "recorded"
        assert workflow["needs_login"] is False
        assert any(step.get("selector") == "button#export" for step in workflow["steps"])
        raw = json.loads(archive.read("recorded.raw.json"))
        assert raw["raw_steps"][0]["selector"] == "button#export"


@pytest.mark.unit
@pytest.mark.parametrize("channel", ["beta", "stable"])
@pytest.mark.parametrize("linked", [False, True], ids=["foreign-file", "foreign-symlink"])
def test_pack_export_zip_keeps_external_sources_read_only(
    tmp_path: Path, export_home: Path, monkeypatch, channel: str, linked: bool,
):
    monkeypatch.setenv("SPIDER_GD_CHANNEL", channel)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    source = foreign / "source.yml"
    original = "name: 外部只读输入\n".encode()
    source.write_bytes(original)
    input_path = export_home / "source.yml" if linked else source
    if linked:
        input_path.symlink_to(source)
    before = _tree_snapshot(foreign)
    output_root = export_home if channel == "beta" else tmp_path / "stable-output"
    target = output_root / "new" / "export.zip"

    assert workshop._pack_export_zip(target, [input_path]) == target

    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["source.yml"]
        assert archive.read("source.yml") == original
    assert _tree_snapshot(foreign) == before
    if linked:
        assert input_path.is_symlink()
        assert input_path.readlink() == source
