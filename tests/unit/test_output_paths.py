"""任务级存储路径模板：变量、根目录和非法路径。"""

from __future__ import annotations

from datetime import date

import pytest

from spider_gd.core.output_paths import (
    PATH_TOKENS,
    choose_download_root,
    render_task_path,
    resolve_task_output_dir,
    template_example,
)

WHEN = date(2026, 10, 30)


@pytest.mark.unit
def test_render_supported_tokens():
    assert render_task_path("{YYYY}/{MM}/{DD}", WHEN).as_posix() == "2026/10/30"
    assert render_task_path("{YYYY-MM}/{YYYY-MM-DD}", WHEN).as_posix() == "2026-10/2026-10-30"
    assert render_task_path("{YY}-{MM-DD}", WHEN).as_posix() == "26-10-30"
    assert render_task_path("{YYYY}/{YYYY-MM}/{MM-DD}", WHEN).as_posix() == "2026/2026-10/10-30"


@pytest.mark.unit
def test_empty_template_keeps_root():
    assert render_task_path("", WHEN) == render_task_path("   ", WHEN)
    assert resolve_task_output_dir("/data", "", WHEN).as_posix() == "/data"
    assert resolve_task_output_dir("/data", "{YYYY-MM}", WHEN).as_posix() == "/data/2026-10"


@pytest.mark.unit
def test_download_root_precedence():
    assert choose_download_root("", "", "/settings", "/fallback").as_posix() == "/settings"
    assert choose_download_root("", "/task", "/settings", "/fallback").as_posix() == "/task"
    assert choose_download_root("/manual", "/task", "/settings", "/fallback").as_posix() == "/manual"


@pytest.mark.unit
def test_backslash_and_repeated_separators_are_normalized():
    assert render_task_path("2026\\10\\\\30", WHEN).as_posix() == "2026/10/30"
    assert render_task_path("./2026/./10/", WHEN).as_posix() == "2026/10"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("/absolute", "不要以"),
        ("\\absolute", "不要以"),
        ("D:/deliver", "不要带盘符"),
        ("../escape", "不能包含 .."),
        ("2026/../../escape", "不能包含 .."),
        ("{UNKNOWN}/x", "不认识的变量"),
        ("{YYYY-MM", "括号不完整"),
        ("2026/10:30", "不能包含这些字符"),
        ("2026/10.", "不能以点"),
    ],
)
def test_reject_illegal_templates(template: str, message: str):
    with pytest.raises(ValueError, match=message):
        render_task_path(template, WHEN)


@pytest.mark.unit
def test_token_catalog_is_complete():
    keys = {key for key, _ in PATH_TOKENS}
    assert keys == {
        "{SSID}", "{YYYY}", "{YY}", "{MM}", "{DD}", "{YYYY-MM}", "{MM-DD}", "{YYYY-MM-DD}",
    }


@pytest.mark.unit
def test_ssid_token_and_fallback():
    assert (
        render_task_path("{SSID}/{YYYY-MM-DD}", WHEN, "CSG-UKEY").as_posix()
        == "CSG-UKEY/2026-10-30"
    )
    assert render_task_path("{SSID}/{YYYY-MM-DD}", WHEN, "").as_posix() == "未连接WiFi/2026-10-30"
    # SSID 里的 Windows 非法字符要清洗成下划线，不能把路径拆坏。
    assert render_task_path("{SSID}", WHEN, "A/B:C").as_posix() == "A_B_C"


@pytest.mark.unit
def test_template_example_never_raises():
    assert template_example("{YYYY-MM}", WHEN) == "2026-10"
    assert template_example("{BAD}", WHEN) == "{BAD}"
