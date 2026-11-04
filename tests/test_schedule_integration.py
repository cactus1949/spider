"""集成测试：调度器、handler、cron 转换、路径解析。

覆盖之前出过的 bug：
1. cron 表达式转换
2. tools/ 路径解析
3. SQLite task_configs 读写
4. 调度注册/移除
5. 路由顺序
"""

import pytest
import tempfile
import time
import sys
from pathlib import Path

# 确保 src 在 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class TestRepeatToCron:
    """cron 表达式转换。"""

    @pytest.fixture
    def _repeat_to_cron(self):
        from spider_gd.app import _repeat_to_cron
        return _repeat_to_cron

    def test_daily(self, _repeat_to_cron):
        assert _repeat_to_cron({"repeat": "daily", "time": "09:00"}) == "0 9 * * *"
        assert _repeat_to_cron({"repeat": "daily", "time": "21:35"}) == "35 21 * * *"
        assert _repeat_to_cron({"repeat": "daily", "time": "22:00"}) == "0 22 * * *"

    def test_daily_single_digit_hour(self, _repeat_to_cron):
        assert _repeat_to_cron({"repeat": "daily", "time": "8:05"}) == "5 8 * * *"

    def test_weekly(self, _repeat_to_cron):
        assert _repeat_to_cron({
            "repeat": "weekly", "time": "09:30", "weekdays": [1, 3, 5]
        }) == "30 9 * * 1,3,5"

    def test_weekly_empty_weekdays(self, _repeat_to_cron):
        """空 weekdays 回退 daily。"""
        assert _repeat_to_cron({
            "repeat": "weekly", "time": "10:00", "weekdays": []
        }) == "0 10 * * *"

    def test_invalid_repeat(self, _repeat_to_cron):
        assert _repeat_to_cron({"repeat": "", "time": "09:00"}) is None
        assert _repeat_to_cron({"repeat": "monthly", "time": "09:00"}) is None

    def test_invalid_time(self, _repeat_to_cron):
        assert _repeat_to_cron({"repeat": "daily", "time": ""}) is None
        assert _repeat_to_cron({"repeat": "daily", "time": "abc"}) is None
        assert _repeat_to_cron({"repeat": "daily", "time": "25:00"}) == "0 23 * * *"  # clamped


class TestToolsPath:
    """tools/ 路径解析——之前 parent.parent 少了一层。"""

    def test_tools_path_from_app(self):
        """app.py 往上 3 层才到项目根。"""
        import spider_gd.app as app_mod
        app_file = Path(app_mod.__file__).resolve()
        # app.py → spider_gd/ → src/ → 项目根
        project_root = app_file.parent.parent.parent
        tools_dir = project_root / "tools"
        assert tools_dir.exists(), f"tools/ not found at {tools_dir}"
        assert (tools_dir / "task_runner.py").exists(), f"task_runner.py not found in {tools_dir}"


class TestTaskConfigsSQLite:
    """SQLite task_configs 表 CRUD。"""

    @pytest.fixture
    def store(self):
        from spider_gd.infrastructure.storage.database import TaskStateStore
        s = TaskStateStore(":memory:")
        yield s
        s.close()

    def test_save_and_list(self, store):
        cfg = {
            "id": "test-1", "name": "测试任务", "login": "mylogin",
            "indicators": ["a", "b"], "repeat": "daily", "time": "21:35",
            "weekdays": [], "backfill_days": 7, "cover_days": 0,
            "enabled": True, "remark": "test"
        }
        store.save_task_config(cfg)
        configs = store.list_task_configs()
        assert len(configs) == 1
        assert configs[0]["id"] == "test-1"
        assert configs[0]["enabled"] is True
        assert configs[0]["time"] == "21:35"

    def test_update(self, store):
        store.save_task_config({"id": "t1", "name": "T1", "enabled": True,
                                 "repeat": "daily", "time": "09:00"})
        store.save_task_config({"id": "t1", "name": "T1-renamed", "enabled": False,
                                 "repeat": "daily", "time": "22:00"})
        got = store.get_task_config("t1")
        assert got["name"] == "T1-renamed"
        assert got["enabled"] is False
        assert got["time"] == "22:00"

    def test_delete(self, store):
        store.save_task_config({"id": "del-me", "name": "X", "enabled": True,
                                 "repeat": "daily", "time": "09:00"})
        assert store.delete_task_config("del-me") is True
        assert store.delete_task_config("del-me") is False
        assert store.get_task_config("del-me") is None

    def test_load_enabled_only(self, store):
        """_load_task_configs 只返回 enabled + repeat + time 的任务。"""
        from spider_gd.config.settings import AppSettings
        from spider_gd.app import AppContext, _load_task_configs
        import dataclasses

        # 创建最小 ctx
        settings = AppSettings()
        ctx = AppContext(
            settings=settings,
            state_store=store,
            landing=None,  # type: ignore
            scheduler=None,  # type: ignore
            alert=None,  # type: ignore
            agent=None,
            endpoints=[],
        )

        store.save_task_config({"id": "a", "name": "A", "enabled": True,
                                 "repeat": "daily", "time": "09:00"})
        store.save_task_config({"id": "b", "name": "B", "enabled": False,
                                 "repeat": "daily", "time": "10:00"})
        store.save_task_config({"id": "c", "name": "C", "enabled": True,
                                 "repeat": "", "time": "11:00"})
        store.save_task_config({"id": "d", "name": "D", "enabled": True,
                                 "repeat": "daily", "time": "12:00"})

        enabled = _load_task_configs(ctx)
        assert len(enabled) == 2
        ids = {c["id"] for c in enabled}
        assert ids == {"a", "d"}

    def test_indicators_roundtrip(self, store):
        """indicators JSON 数组能正确来回。"""
        store.save_task_config({"id": "x", "name": "X",
                                 "indicators": ["spot_price", "disclosure"],
                                 "enabled": True, "repeat": "daily", "time": "09:00"})
        got = store.get_task_config("x")
        assert got["indicators"] == ["spot_price", "disclosure"]


class TestScheduleRegister:
    """调度注册/移除。"""

    @pytest.fixture
    def store(self):
        from spider_gd.infrastructure.storage.database import TaskStateStore
        s = TaskStateStore(":memory:")
        yield s
        s.close()

    @pytest.mark.asyncio
    async def test_register_from_sqlite(self, store):
        from spider_gd.config.settings import AppSettings
        from spider_gd.core.scheduler.task_scheduler import TaskScheduler
        from spider_gd.app import AppContext, register_jobs

        store.save_task_config({
            "id": "job-1", "name": "测试", "login": "",
            "indicators": ["spot"], "repeat": "daily", "time": "08:00",
            "enabled": True
        })

        scheduler = TaskScheduler()
        await scheduler.start()

        settings = AppSettings()
        ctx = AppContext(
            settings=settings, state_store=store, landing=None,  # type: ignore
            scheduler=scheduler, alert=None, agent=None, endpoints=[],  # type: ignore
        )

        ids = await register_jobs(ctx)
        assert len(ids) >= 1
        assert any("job-1" in jid for jid in ids)

        jobs = await scheduler.list_jobs()
        assert any(j.job_id == "task_job-1" for j in jobs)


class TestDualNicPreflight:
    @pytest.mark.asyncio
    async def test_scheduled_task_fails_before_runner_when_dual_nic_preflight_fails(
        self, tmp_path, mocker
    ):
        from spider_gd.app import AppContext, _task_runner_job
        from spider_gd.config.settings import AppSettings
        from spider_gd.infrastructure.network.dual_nic_closure import (
            DualNicClosureReport,
            DualNicRuleResult,
        )
        from spider_gd.infrastructure.storage.database import TaskStateStore

        class FakeAlert:
            def __init__(self):
                self.messages = []

            async def send_alert(self, level, message, event_type="info"):
                self.messages.append((level, message, event_type))
                return None

        class FakeDualNicClosure:
            def __init__(self, *, cache_dir):
                self.cache_dir = cache_dir

            def apply_rules(self, rules):
                return DualNicClosureReport(
                    status="error",
                    applied=0,
                    message="双网卡闭环未通过",
                    details=["ERR 验证失败 专网=False 公网=True 路由=False"],
                    results=[
                        DualNicRuleResult(
                            domain="pm.gd.csg.cn",
                            interface_name="UKey-WiFi",
                            status="error",
                            private_ok=False,
                            public_ok=True,
                            routes_ok=False,
                        )
                    ],
                )

        mocker.patch("spider_gd.infrastructure.network.DualNicClosure", FakeDualNicClosure)

        store = TaskStateStore(tmp_path / "spider.db")
        alert = FakeAlert()
        settings = AppSettings()
        settings.storage.data_dir = str(tmp_path / "data")
        ctx = AppContext(
            settings=settings, state_store=store, landing=None,  # type: ignore
            scheduler=None, alert=alert, agent=None, endpoints=[],  # type: ignore
        )
        try:
            store.save_task_config({
                "id": "job-net", "name": "网络预检任务", "login": "",
                "indicators": ["spot"], "repeat": "daily", "time": "08:00",
                "enabled": True,
            })
            store.save_route_rules([
                {"domain_pattern": "pm.gd.csg.cn", "interface_name": "UKey-WiFi", "enabled": True}
            ])

            handler = _task_runner_job(ctx, "job-net")
            await handler({})

            rows = store._conn.execute("SELECT status, reason, detail_log FROM execution_logs").fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == "failed"
            assert "双网卡" in rows[0]["reason"]
            assert "双网卡预检" in rows[0]["detail_log"]
            assert alert.messages
            assert "双网卡预检失败" in alert.messages[0][1]
        finally:
            store.close()

    def test_dual_nic_preflight_allows_public_down_when_private_ready(self, tmp_path, mocker):
        from spider_gd.app import AppContext, ensure_dual_nic_ready
        from spider_gd.config.settings import AppSettings
        from spider_gd.infrastructure.network.dual_nic_closure import (
            DualNicClosureReport,
            DualNicRuleResult,
        )
        from spider_gd.infrastructure.storage.database import TaskStateStore

        class FakeDualNicClosure:
            def __init__(self, *, cache_dir):
                self.cache_dir = cache_dir

            def apply_rules(self, rules):
                return DualNicClosureReport(
                    status="degraded",
                    applied=1,
                    message="专网闭环通过，公网不可达",
                    details=["WARN 专网验证通过，公网不可达，继续本地采集"],
                    results=[
                        DualNicRuleResult(
                            domain="pm.gd.csg.cn",
                            interface_name="UKey-WiFi",
                            status="degraded",
                            resolved_ips=["36.137.0.13"],
                            private_ok=True,
                            public_ok=False,
                            routes_ok=True,
                        )
                    ],
                )

        mocker.patch("spider_gd.infrastructure.network.DualNicClosure", FakeDualNicClosure)

        store = TaskStateStore(tmp_path / "spider.db")
        settings = AppSettings()
        settings.storage.data_dir = str(tmp_path / "data")
        ctx = AppContext(
            settings=settings, state_store=store, landing=None,  # type: ignore
            scheduler=None, alert=None, agent=None, endpoints=[],  # type: ignore
        )
        try:
            store.save_route_rules([
                {"domain_pattern": "pm.gd.csg.cn", "interface_name": "UKey-WiFi", "enabled": True}
            ])
            ok, message, details = ensure_dual_nic_ready(ctx)
            assert ok is True
            assert "公网不可达" in message
            assert "继续本地采集" in details[0]
        finally:
            store.close()


class TestSanitizeName:
    """确保 TaskRunner 和 API server 的文件名清理一致。"""

    def test_hyphen_kept_in_name(self):
        import sys
        sys.path.insert(0, "tools")
        from task_runner import _sanitize_name
        # r"[^\w\-.]" → NOT (\w or - or .)
        # 中文是 \w（Python 3），`-` 在字符组里，所以保留
        name = "信息披露-发电企业导出"
        assert _sanitize_name(name) == "信息披露-发电企业导出"

    def test_slash_replaced(self):
        import sys
        sys.path.insert(0, "tools")
        from task_runner import _sanitize_name
        assert _sanitize_name("a/b") == "a_b"

    def test_space_replaced(self):
        import sys
        sys.path.insert(0, "tools")
        from task_runner import _sanitize_name
        assert _sanitize_name("hello world") == "hello_world"
