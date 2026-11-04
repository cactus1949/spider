"""测试指令解析、权限校验与 readonly 执行。"""

from __future__ import annotations

import pytest

from spider_gd.gateway.agent.command_executor import CommandExecutor, parse_command
from spider_gd.gateway.agent.permission_guard import PermissionGuard


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "action", "level"),
    [
        ("检查系统状态", "health_check", "readonly"),
        ("发送最近一次台账", "query_result", "readonly"),
        ("立即爬取今天数据", "trigger_crawl", "config"),
        ("把爬取时间改为每天9点", "update_config", "config"),
        ("拉取最新代码并重启", "system_op", "system"),
        ("你好呀", "unknown", "readonly"),
    ],
)
def test_parse_command(text, action, level):
    cmd = parse_command(text)
    assert cmd.action == action
    assert cmd.permission_level == level


@pytest.mark.unit
async def test_readonly_command_executes():
    async def health(_params):
        return "全部正常"

    executor = CommandExecutor(handlers={"health_check": health})
    resp = await executor.execute(parse_command("检查系统状态"))
    assert resp.success is True
    assert resp.message == "全部正常"


@pytest.mark.unit
async def test_config_command_denied_at_readonly():
    executor = CommandExecutor(
        handlers={"trigger_crawl": lambda _p: None},
        guard=PermissionGuard("readonly"),
    )
    resp = await executor.execute(parse_command("立即爬取今天数据"))
    assert resp.success is False
    assert "权限不足" in resp.message


@pytest.mark.unit
async def test_unsupported_action_returns_failure():
    executor = CommandExecutor(handlers={})
    resp = await executor.execute(parse_command("检查系统状态"))
    assert resp.success is False
    assert "暂不支持" in resp.message


@pytest.mark.unit
async def test_config_command_allowed_when_max_config():
    ran = []

    async def trigger(_params):
        ran.append(True)
        return "已触发"

    executor = CommandExecutor(
        handlers={"trigger_crawl": trigger},
        guard=PermissionGuard("config"),
    )
    resp = await executor.execute(parse_command("立即爬取今天数据"))
    assert resp.success is True
    assert ran == [True]
