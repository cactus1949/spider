"""真实本机 Chrome：接管必须保留既有页面与浏览器生命周期。"""
from __future__ import annotations

import asyncio
import socket
import subprocess

import pytest
from playwright.async_api import async_playwright

from spider_gd.core.crawler.shared_browser import SharedBrowser, _get_chrome_path


@pytest.fixture
async def external_chrome(tmp_path):
    chrome = _get_chrome_path()
    if not chrome:
        pytest.skip("需要本机 Chrome")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([
        chrome, "--headless=new", f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1", f"--user-data-dir={tmp_path / 'chrome'}",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pw = await async_playwright().start()
    try:
        for attempt in range(40):
            try:
                browser = await pw.chromium.connect_over_cdp(endpoint, timeout=1000)
                break
            except Exception:
                if proc.poll() is not None or attempt == 39:
                    raise
                await asyncio.sleep(0.1)
        yield endpoint, browser, proc
    finally:
        await pw.stop()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.integration
async def test_attach_keeps_existing_page_and_detach_keeps_chrome(external_chrome):
    endpoint, browser, proc = external_chrome
    page = browser.contexts[0].pages[0]
    await page.set_content("<title>人工已登录</title><div id='account'>企业甲</div>")
    await page.evaluate("window.liveLogin = 'original-session'")
    session = SharedBrowser()
    try:
        await session.connect(endpoint)
        pages = await session.list_pages()
        await session.select_page(pages[0]["id"])
        assert len(browser.contexts) == 1
        assert await session.page.evaluate("window.liveLogin") == "original-session"
        await session.close()  # 外部实例的 close 只能断开
        assert proc.poll() is None
        assert not page.is_closed()
        assert await page.locator("#account").inner_text() == "企业甲"
    finally:
        await session.detach()


@pytest.mark.integration
@pytest.mark.parametrize("custom_directory", [False, True])
async def test_managed_browser_download_survives_disconnect(tmp_path, monkeypatch, custom_directory):
    """专用 Chrome 的手动下载要落在持久目录，工作流仍可 save_as。"""
    if not _get_chrome_path():
        pytest.skip("需要本机 Chrome")
    monkeypatch.setenv("SPIDER_BROWSER_HEADLESS", "1")
    profile = tmp_path / "profile"
    session = SharedBrowser()
    session.configure(profile_dir=str(profile))
    destination = tmp_path / "configured-downloads" if custom_directory else profile / "downloads"
    if custom_directory:
        session.set_download_dir(str(destination))
    try:
        await session.start("about:blank")
        assert session.status()["download_dir"] == str(destination)
        page = await session.page.context.new_page()  # 现货交易会打开新的业务标签
        await page.set_content('''<button id="download">导出</button><script>
          document.querySelector('#download').onclick = () => {
            const link = document.createElement('a');
            link.href = URL.createObjectURL(new Blob(['example'], {type: 'application/octet-stream'}));
            link.download = 'c9828c80-e38d-4caa-82b2-6fd730d087a1';
            link.click();
          };
        </script>''')
        async with page.expect_download() as info:
            await page.click("#download")
        download = await info.value
        explicit = tmp_path / "workflow-export.xlsx"
        await download.save_as(str(explicit))
        native = destination / download.suggested_filename
        async with asyncio.timeout(5):
            while not native.exists():
                await asyncio.sleep(0.05)
        assert explicit.read_bytes() == native.read_bytes() == b"example"
        await session.close()
        assert native.read_bytes() == b"example"
    finally:
        if session.is_running:
            await session.close()


@pytest.mark.integration
async def test_error_modal_pauses_until_login_rechecked(external_chrome, tmp_path):
    from workflow_runner import WorkflowRunner
    endpoint, browser, _proc = external_chrome
    page = browser.contexts[0].pages[0]
    await page.route("http://business.test/**", lambda route: route.fulfill(body="<html></html>"))
    await page.goto("http://business.test/")
    await page.set_content('''<div id="logged-in">企业甲</div>
        <button id="submit" onclick="document.querySelector('.ant-modal-confirm').style.display='block'">登录</button>
        <div class="ant-modal-confirm" style="display:none">
          <div class="ant-modal-confirm-title">登录失败</div>
          <div class="ant-modal-confirm-content">数字证书连接失败</div>
          <div class="ant-modal-confirm-btns"><button>确定</button></div>
        </div><button id="next" onclick="window.nextRan=true">后续指标</button>''')
    session = SharedBrowser()
    session.configure(target_origin="http://business.test", success_selector="#logged-in", auth_policy="reuse_only")
    await session.connect(endpoint)
    events = []
    runner = WorkflowRunner({"name": "登录失败暂停", "steps": [
        {"action": "click", "selector": "#submit"},
        {"action": "dismiss_modal", "timeout": 1000, "report_error": True},
        {"action": "click", "selector": "#next"},
    ]}, download_dir=str(tmp_path))
    task = asyncio.create_task(runner.run(events.append, session=session))
    try:
        async with asyncio.timeout(10):
            while not any(e["event"] == "error_pause" for e in events):
                await asyncio.sleep(0.02)
        assert session.status()["run"]["state"] == "paused"
        assert await page.evaluate("window.nextRan") is None
        assert not task.done()
        run = dict(session.status()["run"])
        with pytest.raises(RuntimeError, match="占用"):
            await session.detach()
        session.resume(run["id"], run["pause_version"])
        async with asyncio.timeout(5):
            while session.status()["run"]["pause_version"] == run["pause_version"]:
                await asyncio.sleep(0.02)
        assert not task.done()  # 错误仍存在，不能越过
        await page.locator(".ant-modal-confirm").evaluate("el => el.remove()")
        run = dict(session.status()["run"])
        session.resume(run["id"], run["pause_version"])
        await asyncio.wait_for(task, 5)
        assert await page.evaluate("window.nextRan") is True
        assert session.status()["run"]["state"] == "succeeded"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await session.detach()


@pytest.mark.integration
async def test_workflow_runs_in_selected_page_and_session_is_exclusive(external_chrome, tmp_path):
    from workflow_runner import WorkflowRunner
    endpoint, browser, proc = external_chrome
    page = browser.contexts[0].pages[0]
    await page.set_content("<title>业务页面</title><button onclick='window.count++'>采集</button>")
    await page.evaluate("window.count = 0")
    other = await browser.contexts[0].new_page()
    await other.set_content("<title>用户自己的页面</title>")
    session = SharedBrowser()
    await session.connect(endpoint)
    pages = await session.list_pages()
    await session.select_page(next(p["id"] for p in pages if p["title"] == "业务页面"))
    events = []
    runner = WorkflowRunner({"name": "原页采集", "needs_login": False, "steps": [
        {"action": "click", "selector": "button"},
        {"action": "close_other_tabs"},
    ]}, download_dir=str(tmp_path))
    try:
        await runner.run(events.append, session=session)
        assert await page.evaluate("window.count") == 1
        assert not other.is_closed()
        assert proc.poll() is None
        assert any(e["event"] == "workflow_done" for e in events)
        assert session.status()["run"]["state"] == "succeeded"
    finally:
        await session.detach()


@pytest.mark.integration
async def test_batch_reuses_login_and_downloads_without_closing_user_pages(external_chrome, tmp_path):
    from task_runner import TaskRunner
    endpoint, browser, proc = external_chrome
    page = browser.contexts[0].pages[0]
    await page.route("http://business.test/**", lambda route: route.fulfill(body="<html></html>"))
    await page.goto("http://business.test/")
    await page.set_content('''<div id="logged-in">企业甲</div>
        <button id="increment" onclick="window.count++">计数</button><a id="export" download="report.csv">导出</a>''')
    await page.evaluate("""() => {
        window.count = 0;
        document.querySelector('#export').href = URL.createObjectURL(new Blob(['date,value\\n2026-10-27,42'], {type:'text/csv'}));
        localStorage.setItem('test-session', 'mock-token');
    }""")
    other = await browser.contexts[0].new_page()
    session = SharedBrowser()
    session.configure(target_origin="http://business.test", success_selector="#logged-in", auth_policy="reuse_only")
    await session.connect(endpoint)
    # 此时一个业务页应自动绑定，不选用户的其他空白页。
    configs = {
        "login": {"type": "login", "require_storage_keys": ["test-session"],
                  "steps": [{"action": "fill", "selector": "#must-not-login", "value": "secret"}]},
        "metric": {"needs_login": True, "steps": [
            {"action": "click", "selector": "#increment"},
            {"action": "export", "selector": "#export", "save_as": "report.csv"},
        ]},
    }
    runner = TaskRunner({"name": "批量复用", "login": "login", "indicators": ["metric", "metric"],
                         "target_dates": ["2026-10-27"]}, download_dir=str(tmp_path / "downloads"),
                        workflow_loader=configs.get)
    events = []
    try:
        await runner.run(events.append, session=session)
        assert not runner._failed, runner._failure_reason
        assert any(e["event"] == "task_done" for e in events)
        assert await page.evaluate("window.count") == 2
        assert (tmp_path / "downloads/report.csv").read_text() == "date,value\n2026-10-27,42"
        assert not other.is_closed()
        assert proc.poll() is None
    finally:
        await session.detach()


@pytest.mark.integration
async def test_browser_api_binds_existing_page_and_post_step_executes(external_chrome, tmp_path, monkeypatch):
    import httpx
    from spider_gd.app import assemble
    from spider_gd.config.settings import load_config
    from spider_gd.gateway.api.server import create_app
    from spider_gd.gateway.control_service import ControlService
    import spider_gd.core.crawler.shared_browser as module
    endpoint, browser, _proc = external_chrome
    page = browser.contexts[0].pages[0]
    await page.set_content("<title>API绑定</title><button onclick='window.clicked=true'>执行</button>")
    session = SharedBrowser(tmp_path / "session.json")
    monkeypatch.setattr(module, "_shared_browser", session)
    settings = load_config("configs/mode_d_offline.yaml")
    settings.storage.sqlite_path = str(tmp_path / "state.db")
    settings.storage.data_dir = str(tmp_path / "downloads")
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    ctx.state_store.save_workflow_config({"id": "existing", "name": "原页", "yaml_text":
        "name: original\nneeds_login: false\nsteps:\n- action: click\n  selector: button\n"})
    app = create_app(ControlService(ctx))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            reply = await client.post("/api/browser/connect", json={"endpoint": endpoint})
            assert reply.json()["status"] == "ok", reply.text
            pages = (await client.get("/api/browser/pages")).json()
            reply = await client.post("/api/browser/select", json={"page_id": pages[0]["id"]})
            assert reply.json()["status"] == "ok"
            reply = await client.post("/api/workflows/existing/step/1/run/stream", json={"mode": "single"})
            assert '"event": "workflow_done"' in reply.text, reply.text
            assert await page.evaluate("window.clicked") is True
            assert (await client.get("/api/browser/status")).json()["run"]["state"] == "succeeded"
    finally:
        await session.detach()
        ctx.state_store.close()


@pytest.mark.integration
async def test_managed_profile_survives_close_and_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_BROWSER_HEADLESS", "1")
    session = SharedBrowser(tmp_path / "connection.json")
    profile = tmp_path / "profile"
    session.configure(profile_dir=str(profile), target_origin="http://business.test")
    try:
        await session.start("about:blank")
        page = session.page
        await page.route("http://business.test/**", lambda route: route.fulfill(body="ok"))
        await page.goto("http://business.test/")
        await page.evaluate("localStorage.setItem('saved', 'persistent-login')")
        await session.close()
        assert profile.is_dir()
        await session.start("about:blank")
        pages = await session.list_pages()
        await session.select_page(next(p["id"] for p in pages if p["url"] == "about:blank"))
        page = session.page
        await page.route("http://business.test/**", lambda route: route.fulfill(body="ok"))
        await page.goto("http://business.test/")
        assert await page.evaluate("localStorage.getItem('saved')") == "persistent-login"
    finally:
        await session.close()


@pytest.mark.integration
async def test_authorized_endpoint_file_attaches_and_stale_controls_rejected(external_chrome, tmp_path):
    import httpx
    from spider_gd.core.crawler.shared_browser import read_authorized_endpoint
    endpoint, browser, proc = external_chrome
    async with httpx.AsyncClient(trust_env=False) as client:
        version = (await client.get(endpoint + '/json/version')).json()
    ws = version['webSocketDebuggerUrl']
    from urllib.parse import urlsplit
    parsed = urlsplit(ws)
    (tmp_path / 'DevToolsActivePort').write_text(f'{parsed.port}\n{parsed.path}\n')
    assert read_authorized_endpoint(tmp_path) == ws
    session = SharedBrowser()
    try:
        await session.connect_authorized(str(tmp_path))
        await session.select_page((await session.list_pages())[0]['id'])
        async with session.execution('first'):
            session.emit({'event': 'error_pause', 'error': '待处理'})
            run = dict(session.status()['run'])
            with pytest.raises(ValueError):
                session.resume('stale-id', run['pause_version'])
            with pytest.raises(ValueError):
                session.resume(run['id'], run['pause_version'] + 1)
            with pytest.raises(RuntimeError, match='占用'):
                async with session.execution('second'):
                    pytest.fail('并发任务不应进入')
            session.cancel(run['id'])
            assert await session.wait_continue() is False
        assert session.status()['run']['state'] == 'cancelled'
        assert proc.poll() is None
        assert len(browser.contexts) == 1
    finally:
        await session.detach()


@pytest.mark.parametrize('endpoint', ['http://example.com:9222', 'http://user:pass@127.0.0.1:9222',
                                      'file:///tmp/socket', 'http://127.0.0.1', 'http://127.0.0.1:0'])
def test_remote_or_ambiguous_endpoints_rejected(endpoint):
    from spider_gd.core.crawler.shared_browser import validate_local_endpoint
    with pytest.raises(ValueError):
        validate_local_endpoint(endpoint)


def test_restart_does_not_replay_paused_task(tmp_path):
    import json
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'config': {}, 'run': {'id': 'old', 'state': 'paused'}}))
    session = SharedBrowser(path)
    assert session.status()['run']['state'] == 'interrupted'
    assert not session.status()['busy']


def test_authorized_endpoint_accepts_chrome_profile_path(tmp_path):
    from spider_gd.core.crawler.shared_browser import read_authorized_endpoint
    (tmp_path / 'Default').mkdir()
    (tmp_path / 'DevToolsActivePort').write_text('9222\n/devtools/browser/abc-123\n')
    assert read_authorized_endpoint(tmp_path / 'Default') == 'ws://127.0.0.1:9222/devtools/browser/abc-123'


def test_authorized_endpoint_missing_file_explains_user_data_root(tmp_path):
    from spider_gd.core.crawler.shared_browser import read_authorized_endpoint
    (tmp_path / 'Default').mkdir()
    with pytest.raises(FileNotFoundError, match='用户数据根目录'):
        read_authorized_endpoint(tmp_path / 'Default')


@pytest.mark.integration
async def test_non_cdp_listener_reports_actionable_connection_error():
    async def non_cdp(_reader, writer):
        await _reader.read(4096)
        writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(non_cdp, '127.0.0.1', 0)
    endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        session = SharedBrowser()
        with pytest.raises(RuntimeError, match='没有提供 Chrome 调试接口'):
            await session.connect(endpoint)
        assert not session.is_running
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.integration
async def test_http_workflow_alerts_and_logs_paused_before_resume(external_chrome, tmp_path, monkeypatch):
    import httpx
    from unittest.mock import AsyncMock
    from spider_gd.app import assemble
    from spider_gd.config.settings import load_config
    from spider_gd.gateway.api.server import create_app
    from spider_gd.gateway.control_service import ControlService
    import spider_gd.core.crawler.shared_browser as module
    endpoint, browser, _ = external_chrome
    page = browser.contexts[0].pages[0]
    await page.route('http://business.test/**', lambda route: route.fulfill(body='<html></html>'))
    await page.goto('http://business.test/')
    await page.set_content('''<div id="ready">已登录</div>
      <button id="login" onclick="document.querySelector('.ant-modal-confirm').style.display='block'">登录</button>
      <div class="ant-modal-confirm" style="display:none">
      <div class="ant-modal-confirm-title">数字证书连接失败</div>
      <div class="ant-modal-confirm-btns"><button>确定</button></div></div>''')
    session = SharedBrowser()
    session.configure(target_origin='http://business.test', success_selector='#ready')
    await session.connect(endpoint)
    monkeypatch.setattr(module, '_shared_browser', session)
    settings = load_config('configs/mode_d_offline.yaml')
    settings.storage.sqlite_path = str(tmp_path / 'state.db')
    settings.storage.data_dir = str(tmp_path / 'downloads')
    settings.log.persist_to_file = False
    ctx = assemble(settings)
    alert = AsyncMock()
    monkeypatch.setattr(ctx.alert, 'send_alert', alert)
    ctx.state_store.save_workflow_config({'id': 'pause-test', 'name': '暂停测试', 'yaml_text':
        'name: pause-test\nneeds_login: false\nsteps:\n- action: click\n  selector: "#login"\n- action: dismiss_modal\n  timeout: 1000\n  report_error: true\n'})
    app = create_app(ControlService(ctx))
    task = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            task = asyncio.create_task(client.post('/api/workflows/pause-test/run/stream', json={}))
            async with asyncio.timeout(10):
                while not alert.await_count:
                    await asyncio.sleep(0.02)
            assert not task.done()
            assert session.status()['run']['state'] == 'paused'
            assert ctx.state_store.list_execution_logs()[0].status == 'paused'
            await page.locator('.ant-modal-confirm').evaluate('el => el.remove()')
            run = dict(session.status()['run'])
            reply = await client.post('/api/browser/resume', json={'run_id': run['id'], 'pause_version': run['pause_version']})
            assert reply.json()['status'] == 'ok'
            reply = await asyncio.wait_for(task, 10)
            assert '"event": "workflow_done"' in reply.text
            assert ctx.state_store.list_execution_logs()[0].status == 'success'
    finally:
        if task and not task.done():
            run = session.status()['run']
            if session.status()['busy']:
                session.cancel(run['id'])
            await asyncio.wait_for(task, 5)
        await session.detach()
        ctx.state_store.close()
