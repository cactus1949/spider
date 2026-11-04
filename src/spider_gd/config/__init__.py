"""配置与日志（链路0）。"""

from __future__ import annotations

from spider_gd.config.deploy_mode import DeployMode, ModuleToggle
from spider_gd.config.logging import attach_task_sink, setup_logging
from spider_gd.config.settings import AppSettings, load_config

__all__ = [
    "AppSettings",
    "DeployMode",
    "ModuleToggle",
    "attach_task_sink",
    "load_config",
    "setup_logging",
]
