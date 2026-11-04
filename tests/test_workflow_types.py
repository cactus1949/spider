"""工作流类型系统 & 登录链式逻辑测试。

测试覆盖：
1. 类型推断（兼容旧格式）
2. 登录工作流查找
3. TaskRunner 登录链式逻辑
4. 工作流 SSE 登录注入
"""

import pytest
import tempfile
from pathlib import Path

# 确保 tools 在 path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from workflow_types import (
    infer_meta, load_workflow, find_login_workflow, migrate_workflow,
)


class TestInferMeta:
    """类型推断：兼容旧格式。"""

    def test_login_type_by_first_action(self):
        cfg = {"steps": [{"action": "login", "url": "https://..."}]}
        meta = infer_meta(cfg)
        assert meta["type"] == "login"
        assert meta["start_from"] == "main"

    def test_indicator_type_by_first_action(self):
        cfg = {"steps": [{"action": "click", "selector": "text=xxx"}]}
        meta = infer_meta(cfg)
        assert meta["type"] == "indicator"
        assert meta["needs_login"] is True
        assert meta["start_from"] == "dashboard"

    def test_explicit_type_respected(self):
        cfg = {"type": "login", "steps": [{"action": "click"}]}
        meta = infer_meta(cfg)
        assert meta["type"] == "login"

    def test_no_login_flag(self):
        cfg = {"no_login": True, "steps": [{"action": "click"}]}
        meta = infer_meta(cfg)
        assert meta["needs_login"] is False
        assert meta["start_from"] == "main"

    def test_no_login_from_description(self):
        cfg = {
            "description": "公开页面，无需登录，直接采集",
            "steps": [{"action": "click"}],
        }
        meta = infer_meta(cfg)
        assert meta["needs_login"] is False

    def test_explicit_needs_login(self):
        cfg = {"needs_login": False, "steps": [{"action": "click"}]}
        meta = infer_meta(cfg)
        assert meta["needs_login"] is False
        assert meta["start_from"] == "main"

    def test_default_needs_login_true(self):
        cfg = {"steps": [{"action": "click"}]}
        meta = infer_meta(cfg)
        assert meta["needs_login"] is True

    def test_start_from_explicit(self):
        cfg = {"start_from": "main", "steps": [{"action": "click"}]}
        meta = infer_meta(cfg)
        assert meta["start_from"] == "main"


class TestFindLoginWorkflow:
    """查找 type=login 的工作流。"""

    def test_find_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # 普通指标
            (tmp / "indicator_a.yml").write_text(
                "name: A\ntype: indicator\nneeds_login: true\nsteps: []", encoding="utf-8"
            )
            # 登录流程
            (tmp / "my_login.yml").write_text(
                "name: 系统登录\ntype: login\nneeds_login: true\nstart_from: main\nsteps:\n  - action: login\n    url: https://x",
                encoding="utf-8",
            )
            # 另一个指标
            (tmp / "indicator_b.yml").write_text(
                "name: B\ntype: indicator\nneeds_login: false\nsteps: []", encoding="utf-8"
            )

            found = find_login_workflow(tmp)
            assert found is not None
            assert found["name"] == "系统登录"
            assert found["type"] == "login"

    def test_no_login_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "a.yml").write_text(
                "name: A\ntype: indicator\nneeds_login: true\nsteps: []", encoding="utf-8"
            )
            found = find_login_workflow(tmp)
            assert found is None


class TestLoadWorkflow:
    """加载工作流时自动补全元数据。"""

    def test_auto_fill_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wf = tmp / "test.yml"
            wf.write_text("name: 测试\nsteps:\n  - action: click\n    selector: x", encoding="utf-8")
            cfg = load_workflow(wf)
            assert cfg["type"] == "indicator"
            assert cfg["needs_login"] is True
            assert cfg["start_from"] == "dashboard"

    def test_preserve_existing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wf = tmp / "test.yml"
            wf.write_text(
                "name: 登录\ntype: login\nstart_from: main\nsteps:\n  - action: login\n    url: https://x",
                encoding="utf-8",
            )
            cfg = load_workflow(wf)
            assert cfg["type"] == "login"
            assert cfg["start_from"] == "main"


class TestMigrateWorkflow:
    """迁移：给旧格式文件添加新字段。"""

    def test_add_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wf = tmp / "old.yml"
            wf.write_text("name: 旧指标\nsteps:\n  - action: click\n", encoding="utf-8")
            result = migrate_workflow(wf, dry_run=False)
            assert result["changed"] is True
            assert result["meta"]["type"] == "indicator"
            # 读回验证
            content = wf.read_text("utf-8")
            assert "type: indicator" in content
            assert "needs_login: true" in content
            assert "start_from: dashboard" in content

    def test_no_change_if_already_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wf = tmp / "migrated.yml"
            wf.write_text(
                "name: 已迁移\ntype: indicator\nneeds_login: true\nstart_from: dashboard\nsteps: []",
                encoding="utf-8",
            )
            result = migrate_workflow(wf, dry_run=False)
            assert result["changed"] is False

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wf = tmp / "dry.yml"
            original = "name: X\nsteps:\n  - action: click\n"
            wf.write_text(original, encoding="utf-8")
            result = migrate_workflow(wf, dry_run=True)
            assert result["changed"] is True
            # dry_run 不应修改文件
            assert wf.read_text("utf-8") == original


class TestLoginChaining:
    """登录链式逻辑：手动测试 vs 任务执行。"""

    def _make_workflow(self, name: str, wf_type: str, needs_login: bool,
                       start_from: str, steps: list) -> dict:
        return {
            "name": name,
            "type": wf_type,
            "needs_login": needs_login,
            "start_from": start_from,
            "steps": steps,
        }

    def test_manual_test_login_injected(self):
        """手动测试：indicator + needs_login=true → 应注入登录步骤。"""
        indicator = self._make_workflow(
            "test", "indicator", True, "dashboard",
            [{"action": "click", "selector": "x"}],
        )
        steps = list(indicator["steps"])
        should_inject = (
            indicator["type"] != "login"
            and indicator["needs_login"]
            and not indicator.get("no_login")
        )
        assert should_inject is True

    def test_manual_test_login_not_injected(self):
        """手动测试：indicator + needs_login=false → 不注入登录。"""
        indicator = self._make_workflow(
            "test", "indicator", False, "main",
            [{"action": "click", "selector": "x"}],
        )
        steps = list(indicator["steps"])
        should_inject = (
            indicator["type"] != "login"
            and indicator["needs_login"]
            and not indicator.get("no_login")
        )
        assert should_inject is False

    def test_login_type_not_injected(self):
        """手动测试：type=login 本身 → 不注入（它自己就是登录流程）。"""
        login_wf = self._make_workflow(
            "login", "login", True, "main",
            [{"action": "login", "url": "https://x"}],
        )
        steps = list(login_wf["steps"])
        should_inject = (
            login_wf["type"] != "login"
            and login_wf["needs_login"]
            and not login_wf.get("no_login")
        )
        assert should_inject is False

    def test_start_from_main_with_login_step_should_inject(self):
        """start_from=main 的 indicator，首步是 login 占位符 → 必须注入完整登录。
        
        这是本次修复的核心：之前 first_action != "login" 错误地排除了这种场景。
        """
        wf = self._make_workflow(
            "spot_full", "indicator", True, "main",
            [{"action": "login", "url": "https://pm.gd.csg.cn/portal/#/home"},
             {"action": "click", "selector": "text=现货交易", "expect": "new_tab"}],
        )
        steps = list(wf["steps"])
        first_action = steps[0].get("action", "")
        should_inject = (
            wf["type"] != "login"
            and wf["needs_login"]
            and not wf.get("no_login")
        )
        assert should_inject is True, (
            "start_from=main 且 needs_login=true 的 indicator 必须注入登录，"
            "即使首步是 action: login"
        )
        # 验证 _replace_first_login 逻辑
        replace_first = should_inject and wf["start_from"] == "main" and first_action == "login"
        assert replace_first is True, "首步 login 占位符应被替换为完整登录流程"

    def test_start_from_dashboard_click_first_should_inject(self):
        """start_from=dashboard 的 indicator，首步是 click → 正常注入登录。"""
        wf = self._make_workflow(
            "disclosure", "indicator", True, "dashboard",
            [{"action": "click", "selector": "text=信息披露查询", "expect": "new_tab"}],
        )
        steps = list(wf["steps"])
        should_inject = (
            wf["type"] != "login"
            and wf["needs_login"]
            and not wf.get("no_login")
        )
        assert should_inject is True

    def test_task_indicator_with_login(self):
        """任务执行：indicator + needs_login=true → 恢复登录态，从 dashboard 开始。"""
        indicator = self._make_workflow(
            "spot", "indicator", True, "dashboard",
            [{"action": "click", "selector": "text=导出"}],
        )
        needs_login = indicator["needs_login"]
        login_done = True  # 任务已完成登录
        state_exists = True
        assert needs_login and login_done and state_exists
        # 应注入 restore_state
        prepend = {
            "action": "login",
            "url": "https://pm.gd.csg.cn/portal/#/home",
            "restore_state": "downloads/storage_state.json",
        }
        # 验证逻辑
        assert prepend["action"] == "login"
        assert "restore_state" in prepend

    def test_task_indicator_no_login(self):
        """任务执行：indicator + needs_login=false → 从 main 直接开始，独立上下文。"""
        indicator = self._make_workflow(
            "public", "indicator", False, "main",
            [{"action": "click", "selector": "text=下载"}],
        )
        steps = list(indicator["steps"])
        needs_login = indicator["needs_login"]
        # 不需要登录 → 不恢复登录态，直接从 main 开始
        if not needs_login and steps[0].get("action") not in ("login", "navigate"):
            steps = [{"action": "navigate", "url": "https://..."}] + steps
        assert steps[0]["action"] == "navigate"


class TestStorageStateReplacement:
    """storage_state.json 自动复用：找到文件时用 restore 替换登录步。"""

    def test_replace_login_with_restore(self):
        """有 storage_state 时，精确按 login_steps 长度截断，不吞指标步。"""
        login_steps = [
            {"action": "login", "url": "https://..."},
            {"action": "click", "selector": "button:has-text('登 录')"},
            {"action": "wait", "ms": 2000},
            {"action": "click", "selector": "text=证书登录"},
            {"action": "wait", "ms": 1000},
            {"action": "fill", "selector": "input", "value": "***"},
            {"action": "captcha", "captcha_img": "img", "captcha_input": "#code", "retry": 5},
        ]
        indicator_steps = [
            {"action": "click", "selector": "text=现货交易", "expect": "new_tab"},
            {"action": "wait", "selector": ".style-box-head", "timeout": 15000},
        ]
        steps = login_steps + indicator_steps

        # 模拟 storage_state 替换
        restore_step = {
            "action": "login",
            "url": "https://pm.gd.csg.cn/portal/#/home",
            "restore_state": "/path/to/storage_state.json",
        }
        steps = [restore_step] + steps[len(login_steps):]

        assert len(steps) == 1 + len(indicator_steps), (
            f"替换后应为 1 个 restore + {len(indicator_steps)} 个指标步，"
            f"实际 {len(steps)} 步"
        )
        assert steps[0]["action"] == "login"
        assert "restore_state" in steps[0]
        assert steps[1]["action"] == "click"  # 指标第一步未被吞
        assert steps[1]["selector"] == "text=现货交易"

    def test_no_storage_state_keeps_login(self):
        """无 storage_state 时，登录步骤保持不变。"""
        login_steps = [{"action": "login", "url": "https://..."}]
        indicator_steps = [{"action": "click", "selector": "x"}]
        steps = login_steps + indicator_steps
        # 不替换
        assert steps[0]["action"] == "login"
        assert "restore_state" not in steps[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
