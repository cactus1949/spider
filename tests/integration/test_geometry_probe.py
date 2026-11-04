"""用真实 Chromium 验证隔离探针，不连接业务网站。"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from geometry_probe import CASES, apply_probe, diagnostic_driver, main, run_round, safe_url
from playwright.async_api import async_playwright


@pytest.fixture
def geometry_site():
    state = {"count": 0, "fail_at": 0, "network_failure": False, "resets": 0, "reset_failed": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["resets"] += 1
            body = b'{"status":"error"}' if state["reset_failed"] else b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/verify/captcha":
                state["count"] += 1
                if state["network_failure"]:
                    self.connection.close()
                    return
                failed = state["count"] == state["fail_at"]
                body = b"\r\n\r\n\r\n" if failed else b'{"code":0}'
                self.send_response(400 if failed else 200)
                self.send_header("Content-Type", "text/html" if failed else "application/json")
            else:
                body = b"""<!doctype html><button id="target">Geometry target</button>
<img id="captcha" width="100" height="50" onclick="fetch('/verify/captcha')"
src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1sAAAAASUVORK5CYII=">"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


@pytest.fixture
async def geometry_browser(geometry_site):
    url, state = geometry_site
    args = SimpleNamespace(
        selector="#target",
        index=0,
        captcha_selector="#captcha",
        response_path="/verify/captcha",
        timeout=3,
        settle=0,
        interval=0,
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url)
            session = await page.context.new_cdp_session(page)
            await session.send("Network.enable")
            yield page, session, args, state
        finally:
            await browser.close()


@pytest.mark.integration
async def test_all_geometry_cases_use_same_successful_captcha_control(geometry_browser):
    page, session, args, state = geometry_browser
    for case in CASES:
        result = await run_round(page, session, case, args)
        assert result["outcome"] == "200_after_probe", result
        assert result["baseline"]["status"] == 200
        assert result["after"]["body_size"] == len(b'{"code":0}')
        assert result["loader_id"]
    assert state["count"] == len(CASES) * 2


@pytest.mark.integration
async def test_failed_baseline_never_applies_probe(geometry_browser, monkeypatch):
    page, session, args, state = geometry_browser
    state["fail_at"] = 1

    async def forbidden(*args):
        raise AssertionError("A failed baseline must not run the experiment")

    monkeypatch.setattr("geometry_probe.apply_probe", forbidden)
    result = await run_round(page, session, "rawbox", args)
    assert result["stop"] == "baseline_unavailable"
    assert result["outcome"] == "inconclusive"
    assert result["baseline"]["body_blank"] is True
    assert state["count"] == 1


@pytest.mark.integration
async def test_exact_400_shape_is_distinct_from_transport_failure(geometry_browser):
    page, session, args, state = geometry_browser
    state["fail_at"] = 2
    result = await run_round(page, session, "rawbox", args)
    assert result["outcome"] == "observed_400_shape", result
    assert result["after"]["body_size"] == 6
    assert result["methods"] == ["DOM.getDocument", "DOM.querySelectorAll", "DOM.getBoxModel"]

    state["network_failure"] = True
    result = await run_round(page, session, "rawbox", args)
    assert result["stop"] == "baseline_unavailable"
    assert result["baseline"]["state"] in ("network_error", "timeout")
    assert result["outcome"] == "inconclusive"


def test_result_redacts_page_url():
    assert (
        safe_url("https://example.test/login?token=secret#secret") == "https://example.test/login"
    )


async def test_probe_rejects_unknown_case():
    with pytest.raises(ValueError, match="Unknown case"):
        await apply_probe(None, None, "unknown", "#target", 0)


@pytest.mark.integration
async def test_failed_baseline_stops_entire_batch(geometry_browser, monkeypatch, tmp_path):
    page, _session, args, state = geometry_browser
    state["fail_at"] = 1

    async def connect(*_args, **_kwargs):
        return page.context.browser

    @asynccontextmanager
    async def borrowed_runtime():
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

    monkeypatch.setattr("geometry_probe.async_playwright", borrowed_runtime)
    args.endpoint = "unused-local-test"
    args.host = page.url.split("/")[2]
    args.output = tmp_path / "results.jsonl"
    args.repeat = 3
    args.cases = ["noop", "rawbox"]
    assert await main(args) == 2
    assert len(args.output.read_text(encoding="utf-8").splitlines()) == 1
    assert state["count"] == 1
    assert not page.is_closed()


@pytest.mark.integration
async def test_explicit_reset_and_failed_reset_gate_input(geometry_browser):
    page, session, args, state = geometry_browser
    args.reset_url = page.url + "reset"
    result = await run_round(page, session, "noop", args)
    assert result["outcome"] == "200_after_probe"
    assert result["reset"] == "cookies_and_reload"
    assert state["resets"] == 1
    assert state["count"] == 2

    state["reset_failed"] = True
    result = await run_round(page, session, "rawbox", args)
    assert result["stop"] == "reset_failed"
    assert state["count"] == 2


@pytest.mark.integration
async def test_observed_400_makes_batch_exit_unsuccessfully(
    geometry_browser, monkeypatch, tmp_path
):
    page, _session, args, state = geometry_browser
    state["fail_at"] = 2

    async def connect(*_args, **_kwargs):
        return page.context.browser

    @asynccontextmanager
    async def borrowed_runtime():
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

    monkeypatch.setattr("geometry_probe.async_playwright", borrowed_runtime)
    args.endpoint = "unused-local-test"
    args.host = page.url.split("/")[2]
    args.output = tmp_path / "results.jsonl"
    args.repeat = 1
    args.cases = ["rawbox"]
    assert await main(args) == 1
    assert state["count"] == 2


def test_diagnostic_driver_does_not_modify_installed_bundle():
    from pathlib import Path

    from playwright._impl import _transport
    from playwright._impl._driver import compute_driver_executable

    original = compute_driver_executable()
    bundle = Path(original[1]).parent / "lib/coreBundle.js"
    before = bundle.read_bytes()
    with diagnostic_driver(True) as variant:
        assert variant == "temporary_copy_without_element_preview"
        temporary = _transport.compute_driver_executable()
        assert temporary != original
        temporary_bundle = Path(temporary[1]).parent / "lib/coreBundle.js"
        assert b"/* diagnostic: skip element preview */" in temporary_bundle.read_bytes()
        assert bundle.read_bytes() == before
    assert _transport.compute_driver_executable() == original
    assert not temporary_bundle.exists()
