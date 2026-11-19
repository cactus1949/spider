"""用同一段页面脚本区分工作台布局和真正挡住操作的对话框。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("workbench_popup_dom.mjs")


@pytest.mark.unit
def test_popup_script_ignores_layout_and_keeps_real_dialogs():
    completed = subprocess.run(
        ["node", "--test", str(_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "fail 0" in completed.stdout
