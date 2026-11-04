"""build_workflow HTML 元数据清洗测试。"""

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

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
