"""动作诊断只保存指纹，网络请求归属发起时的动作阶段。"""
import json
from unittest.mock import AsyncMock, MagicMock

from workflow_runner import WorkflowRunner


async def test_trace_redacts_credentials_and_keeps_request_start_phase(tmp_path):
    page = MagicMock()
    page.url = "https://pm.gd.csg.cn/pfxh/stc/index?session=private"
    page.is_closed.return_value = False
    page.context.cookies = AsyncMock(return_value=[{
        "name": "VShPPFZUwMakP", "domain": "pm.gd.csg.cn", "path": "/", "value": "cookie-secret",
    }])
    runner = WorkflowRunner({"trace_browser_actions": True}, download_dir=str(tmp_path))
    runner.pages = [page]
    await runner._trace_browser_action("captcha.before_fill")

    request = MagicMock()
    request.url = "https://pm.gd.csg.cn/pfxh/api/getJddjTree?HdKGccbL=signed-secret"
    request.resource_type = "xhr"
    request.method = "GET"
    request.frame.page = page
    runner._on_page_request(request)
    runner._action_phase = "captcha.after_fill"
    response = MagicMock(request=request, status=200, url=request.url,
                         headers={"content-type": "application/json"})
    await runner._capture_page_response(response)

    text = runner._action_trace_file.read_text()
    entries = [json.loads(line) for line in text.splitlines()]
    assert entries[-1]["phase"] == "captcha.before_fill"
    assert entries[-1]["status"] == 200
    assert entries[0]["context"] == entries[-1]["context"]
    assert entries[0]["cookies"][0]["sha256"]
    assert "cookie-secret" not in text
    assert "signed-secret" not in text
    assert "session=private" not in text
    page.evaluate.assert_not_called()


async def test_trace_disabled_does_not_read_cookies_or_write_files(tmp_path):
    page = MagicMock()
    runner = WorkflowRunner({}, download_dir=str(tmp_path))
    runner.pages = [page]
    await runner._trace_browser_action("captcha.before_fill")
    page.context.cookies.assert_not_called()
    assert not runner._action_trace_file.exists()
