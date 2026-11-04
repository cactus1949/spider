"""测试三级权限控制。"""

from __future__ import annotations

import pytest

from spider_gd.gateway.agent.permission_guard import PermissionGuard


@pytest.mark.unit
def test_readonly_max_allows_only_readonly():
    guard = PermissionGuard("readonly")
    assert guard.check("readonly") is True
    assert guard.check("config") is False
    assert guard.check("system") is False


@pytest.mark.unit
def test_config_max_allows_readonly_and_config():
    guard = PermissionGuard("config")
    assert guard.check("readonly") is True
    assert guard.check("config") is True
    assert guard.check("system") is False


@pytest.mark.unit
def test_unknown_level_denied():
    assert PermissionGuard("readonly").check("nonsense") is False


@pytest.mark.unit
def test_invalid_max_level_raises():
    with pytest.raises(ValueError, match="未知权限级别"):
        PermissionGuard("god")
