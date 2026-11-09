"""补数落盘路径：请求优先，否则沿用定时任务的存储配置。"""

from task_runner import apply_catchup_storage, catchup_download_dir, match_task_storage

from spider_gd.core.output_paths import choose_download_root


def _task(**overrides):
    base = {
        "name": "每日",
        "indicators": ["日前"],
        "enabled": True,
        "created_at": 1,
        "path_structure": "- [日前]",
        "path_template": "",
        "path_folders": [],
        "download_dir": "",
        "download_dir_custom": False,
    }
    base.update(overrides)
    return base


def test_match_task_storage_prefers_broader_then_enabled_then_newer():
    tasks = [
        _task(name="旧的日前", indicators=["日前"], created_at=1, path_structure="A"),
        _task(
            name="停用全覆盖",
            indicators=["日前", "实时"],
            enabled=False,
            created_at=9,
            path_structure="停",
        ),
        _task(name="全覆盖", indicators=["日前", "实时"], created_at=2, path_structure="B"),
        _task(name="新的日前", indicators=["日前"], created_at=3, path_structure="C"),
    ]

    # 两个指标都要时，覆盖得更多的任务优先，即使有更新但已停用的全覆盖任务。
    assert match_task_storage(["日前", "实时"], tasks)["name"] == "全覆盖"
    # 只补日前时，覆盖数相同，已启用里创建更晚的优先。
    assert match_task_storage(["日前"], tasks)["name"] == "新的日前"
    # 已启用优先于创建更晚但停用的任务。
    assert match_task_storage(["实时"], tasks)["name"] == "全覆盖"
    assert match_task_storage(["其他"], tasks) is None
    assert match_task_storage([], tasks) is None


def test_apply_catchup_storage_defaults_to_task_path_without_mutating_input():
    cfg = {"indicators": ["日前"], "name": "补数据"}
    tasks = [_task(
        path_template="{YYYY-MM}",
        path_folders=[{"name": "阳江", "indicators": ["日前"]}],
        download_dir="/data/task",
        download_dir_custom=True,
    )]

    stored = apply_catchup_storage(cfg, tasks)

    assert stored["path_structure"] == "- [日前]"
    assert stored["path_template"] == "{YYYY-MM}"
    assert stored["path_folders"] == [{"name": "阳江", "indicators": ["日前"]}]
    assert stored["download_dir"] == "/data/task"
    assert stored["download_dir_custom"] is True
    assert "path_structure" not in cfg
    assert catchup_download_dir(stored) == "/data/task"
    assert choose_download_root(
        catchup_download_dir(stored), "", "/settings", "/fallback",
    ).as_posix() == "/data/task"


def test_apply_catchup_storage_ignores_task_dir_that_is_not_custom():
    stored = apply_catchup_storage(
        {"indicators": ["日前"]},
        [_task(download_dir="/data/legacy", download_dir_custom=False)],
    )

    assert stored["path_structure"] == "- [日前]"
    assert "download_dir_custom" not in stored
    assert catchup_download_dir(stored) == ""
    assert choose_download_root(
        catchup_download_dir(stored), "", "/settings", "/fallback",
    ).as_posix() == "/settings"


def test_apply_catchup_storage_request_overrides_task_even_when_cleared():
    tasks = [_task(download_dir="/data/task", download_dir_custom=True)]
    stored = apply_catchup_storage(
        {"indicators": ["日前"]},
        tasks,
        path_structure="- 本次\n  - [日前]",
        path_template="",
        path_folders=[],
        download_dir="/data/once",
        storage_overridden=True,
    )

    assert stored["path_structure"] == "- 本次\n  - [日前]"
    assert stored["path_template"] == ""
    assert stored["path_folders"] == []
    assert stored["download_dir"] == "/data/once"
    assert catchup_download_dir(stored) == "/data/once"

    cleared = apply_catchup_storage(
        {"indicators": ["日前"]},
        tasks,
        path_structure="",
        path_template="",
        path_folders=[],
        download_dir="",
        storage_overridden=True,
    )
    assert cleared["path_structure"] == ""
    assert cleared["download_dir_custom"] is False
    assert catchup_download_dir(cleared) == ""
    assert choose_download_root(
        catchup_download_dir(cleared), "", "/settings", "/fallback",
    ).as_posix() == "/settings"


def test_apply_catchup_storage_without_matching_task_keeps_config():
    cfg = {"indicators": ["日前"], "name": "补数据"}
    stored = apply_catchup_storage(cfg, [_task(indicators=["实时"])])
    assert stored == cfg
    assert catchup_download_dir(stored) == ""
