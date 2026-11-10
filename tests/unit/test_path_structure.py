"""YAML 风格存储结构：解析、占位符、指标映射。"""

from __future__ import annotations

from datetime import date

import pytest

from spider_gd.core.path_structure import (
    build_structure_dirs,
    common_prefix,
    parse_structure,
    resolve_structure,
    save_as_conflicts,
    structure_warnings,
)
from spider_gd.core.workflow_outputs import OutputSpec

WHEN = date(2026, 10, 30)


def test_parse_simple_outline():
    text = """
    - {wifi}
      - 披露文件
        - [实时节点电价查询导出]
        - [交易日历]
      - 阳江
        - [场站-日前交易结果发电侧导出]
    """
    nodes = parse_structure(text)
    assert [node.name for node in nodes] == ["{wifi}"]
    wifi = nodes[0]
    assert [node.name for node in wifi.children] == ["披露文件", "阳江"]
    assert wifi.children[0].children[0].kind == "file"
    assert wifi.children[0].children[0].name == "实时节点电价查询导出"


def test_file_tag_cannot_have_children():
    with pytest.raises(ValueError, match="文件标签下面"):
        parse_structure("- [交易日历]\n  - 子目录")


def test_resolve_with_wifi_and_date():
    text = """
    - {wifi}
      - {YYYY-MM-DD}
        - 披露文件
          - [实时节点电价查询导出]
          - [交易日历]
    """
    result = resolve_structure(
        text,
        ["实时节点电价查询导出", "交易日历"],
        ssid="CSG-UKEY",
        when=WHEN,
    )
    assert result.assignments == {
        "实时节点电价查询导出::": ["CSG-UKEY", "2026-10-30", "披露文件"],
        "交易日历::": ["CSG-UKEY", "2026-10-30", "披露文件"],
    }
    assert result.base_parts == ["CSG-UKEY", "2026-10-30", "披露文件"]
    assert result.unassigned == []


def test_wifi_tag_is_optional():
    text = """
    - 披露文件
      - [A]
    - 阳江
      - [B]
    """
    result = resolve_structure(text, ["A", "B"], ssid="CSG-UKEY", when=WHEN)
    assert result.assignments["A::"] == ["披露文件"]
    assert result.assignments["B::"] == ["阳江"]
    assert result.base_parts == []


def test_unassigned_indicators():
    result = resolve_structure("- {wifi}\n  - [A]", ["A", "B"], ssid="X", when=WHEN)
    assert result.assignments == {"A::": ["X"]}
    assert result.unassigned == ["B::"]


def test_unknown_tag_is_reported_not_raised():
    result = resolve_structure("- 目录\n  - [未知]\n  - [A]", ["A"], ssid="X", when=WHEN)
    assert result.invalid == ["未知"]
    assert result.assignments == {"A::": ["目录"]}


def test_renamed_save_as_key_is_reported_and_others_still_run():
    outputs = {
        "指标X": [
            OutputSpec("日前.xlsx", "指标X", 1, "export", "日前.xlsx", "日前.xlsx", "s1"),
            OutputSpec("实时.xlsx", "指标X", 3, "export", "实时.xlsx", "实时.xlsx", "s3"),
        ]
    }
    result = resolve_structure(
        "- 日前\n  - [指标X::旧日前.xlsx]\n- 实时\n  - [指标X::实时.xlsx]",
        ["指标X"], outputs=outputs, ssid="X", when=WHEN,
    )
    assert result.invalid == ["指标X::旧日前.xlsx"]
    assert result.assignments == {"指标X::实时.xlsx": ["实时"]}
    assert result.unassigned == ["指标X::日前.xlsx"]


def test_duplicate_tags_still_rejected():
    with pytest.raises(ValueError, match="出现了多次"):
        resolve_structure("- [A]\n- [A]", ["A"], ssid="X", when=WHEN)


def test_build_dirs_and_common_base():
    result = resolve_structure(
        "- {wifi}\n  - 披露文件\n    - [A]\n  - 阳江\n    - [B]",
        ["A", "B"],
        ssid="CSG-UKEY",
        when=WHEN,
    )
    base, dirs = build_structure_dirs("/data", result)
    assert base == "/data/CSG-UKEY"
    assert dirs["A::"] == "/data/CSG-UKEY/披露文件"
    assert dirs["B::"] == "/data/CSG-UKEY/阳江"


def test_common_prefix():
    assert common_prefix([["a", "b"], ["a", "c"]]) == ["a"]
    assert common_prefix([["a"], ["a", "b"]]) == ["a"]
    assert common_prefix([["a"], ["b"]]) == []
    assert common_prefix([]) == []


def test_multi_output_indicator_can_be_split_or_grouped():
    outputs = {
        "指标X": [
            OutputSpec("s1", "指标X", 1, "export", "文件1({date}).xlsx", "文件1({date}).xlsx"),
            OutputSpec("s3", "指标X", 3, "export", "文件2({date}).xlsx", "文件2({date}).xlsx"),
        ]
    }
    # 只写 [指标X]：两个输出都放进同一个文件夹
    grouped = resolve_structure(
        "- 披露\n  - [指标X]", ["指标X"], outputs=outputs, ssid="X", when=WHEN
    )
    assert grouped.assignments == {
        "指标X::s1": ["披露"],
        "指标X::s3": ["披露"],
    }
    # 写 [指标X::s3]：只放指定的输出
    single = resolve_structure(
        "- 披露\n  - [指标X::s3]", ["指标X"], outputs=outputs, ssid="X", when=WHEN
    )
    assert single.assignments == {"指标X::s3": ["披露"]}
    assert single.unassigned == ["指标X::s1"]


def test_old_single_output_key_survives_week_step_insertion():
    outputs = {"交易日历": [OutputSpec("s8", "交易日历", 8, "extract_calendar", "x.csv", "x.csv")]}
    result = resolve_structure("- 周报\n  - [交易日历::s7]", ["交易日历"], outputs=outputs)
    assert result.assignments == {"交易日历::s8": ["周报"]}


def test_structure_scope_keeps_tags_from_the_whole_task():
    """补数据只跑其中一个指标时，任务结构里的其他标签不能被报成失效。"""
    from spider_gd.core.path_structure import structure_indicator_scope

    scope = structure_indicator_scope(["日清算"], ["日清算", "交易日历", "现货"])
    result = resolve_structure(
        "- 日\n  - [日清算]\n  - [交易日历]\n  - [现货]",
        scope,
    )

    assert result.invalid == []
    assert result.assignments["日清算::"] == ["日"]
    assert structure_warnings(result, indicators=scope) == []


def test_duplicate_invalid_tags_warn_once_and_name_the_reason():
    result = resolve_structure(
        "- [现货]\n- [现货]\n- [日清算::旧文件.xlsx]",
        ["日清算"],
        outputs={"日清算": [
            OutputSpec("甲.xlsx", "日清算", 1, "export", "甲.xlsx", "甲.xlsx", "s1"),
            OutputSpec("乙.xlsx", "日清算", 2, "export", "乙.xlsx", "乙.xlsx", "s2"),
        ]},
    )

    lines = structure_warnings(result, indicators=["日清算"])

    assert result.invalid.count("现货") == 2
    assert sum("现货" in line for line in lines) == 1
    assert any("现货" in line and "不在当前任务" in line for line in lines)
    assert any("日清算::旧文件.xlsx" in line and "save_as" in line for line in lines)


def test_structure_warnings_explain_invalid_tags_and_duplicate_save_as():
    outputs = {"指标X": [
        OutputSpec("s1", "指标X", 1, "export", "同名.xlsx", "同名.xlsx", "s1", True),
        OutputSpec("s3", "指标X", 3, "export", "同名.xlsx", "同名.xlsx", "s3", True),
    ]}
    result = resolve_structure(
        "- 日前\n  - [指标X::旧.xlsx]", ["指标X"], outputs=outputs, ssid="X", when=WHEN,
    )

    lines = structure_warnings(result, outputs)

    assert any("指标X::旧.xlsx" in line and "基础目录" in line for line in lines)
    assert any("同名.xlsx" in line and "覆盖" in line for line in lines)


def test_named_outputs_with_same_file_still_conflict_in_same_folder():
    outputs = {"指标X": [
        OutputSpec("日前", "指标X", 1, "export", "同名.xlsx", "日前", "s1"),
        OutputSpec("实时", "指标X", 3, "export", "同名.xlsx", "实时", "s3"),
    ]}
    same = resolve_structure("- 目录\n  - [指标X]", ["指标X"], outputs=outputs, ssid="X", when=WHEN)
    split = resolve_structure(
        "- 日前\n  - [指标X::日前]\n- 实时\n  - [指标X::实时]",
        ["指标X"], outputs=outputs, ssid="X", when=WHEN,
    )
    assert save_as_conflicts(same, outputs) == ["指标X::日前", "指标X::实时"]
    assert save_as_conflicts(split, outputs) == []


def test_structure_warnings_empty_when_everything_matches():
    outputs = {"A": [OutputSpec("日前.xlsx", "A", 1, "export", "日前.xlsx", "日前.xlsx", "s1")]}
    result = resolve_structure("- [A::日前.xlsx]", ["A"], outputs=outputs, ssid="X", when=WHEN)

    assert structure_warnings(result, outputs) == []


def _same_name_outputs() -> dict[str, list[OutputSpec]]:
    return {"指标X": [
        OutputSpec("s1", "指标X", 1, "export", "/旧/日前/同名.xlsx", "同名.xlsx", "s1", True),
        OutputSpec("s3", "指标X", 3, "export", "/旧/实时/同名.xlsx", "同名.xlsx", "s3", True),
    ]}


def test_same_save_as_in_different_folders_is_not_a_conflict():
    outputs = _same_name_outputs()
    result = resolve_structure(
        "- 日前\n  - [指标X::s1]\n- 实时\n  - [指标X::s3]", ["指标X"], outputs=outputs, ssid="X", when=WHEN,
    )

    assert save_as_conflicts(result, outputs) == []
    assert structure_warnings(result, outputs) == []


def test_same_save_as_in_same_folder_conflicts():
    outputs = _same_name_outputs()
    result = resolve_structure(
        "- 日前\n  - [指标X::s1]\n  - [指标X::s3]", ["指标X"], outputs=outputs, ssid="X", when=WHEN,
    )

    assert save_as_conflicts(result, outputs) == ["指标X::s1", "指标X::s3"]
    assert any("同名.xlsx" in line and "覆盖" in line for line in structure_warnings(result, outputs))


def test_unplaced_same_save_as_falls_into_base_dir_together():
    outputs = _same_name_outputs()
    result = resolve_structure("- 日前\n  - [指标X::s1]", ["指标X"], outputs=outputs, ssid="X", when=WHEN)

    # 未放进结构的文件落到基础目录，而基础目录正好就是「日前」
    assert save_as_conflicts(result, outputs) == ["指标X::s1", "指标X::s3"]
