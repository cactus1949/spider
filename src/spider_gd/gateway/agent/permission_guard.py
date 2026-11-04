"""三级权限控制：readonly < config < system。"""

from __future__ import annotations

from loguru import logger

_LEVELS = {"readonly": 0, "config": 1, "system": 2}


class PermissionGuard:
    def __init__(self, max_level: str = "readonly") -> None:
        if max_level not in _LEVELS:
            raise ValueError(f"未知权限级别：{max_level}")
        self._max = _LEVELS[max_level]
        self._max_name = max_level

    def check(self, command_level: str) -> bool:
        if command_level not in _LEVELS:
            logger.warning("未知指令权限级别：{}", command_level)
            return False
        allowed = _LEVELS[command_level] <= self._max
        if not allowed:
            logger.warning("权限不足：指令需 {}，当前上限 {}", command_level, self._max_name)
        return allowed
