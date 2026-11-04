"""部署模式枚举与模块开关。

业务模块永远不写 ``if mode == ...``，而是读取 :class:`ModuleToggle` 决定装配。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class DeployMode(str, Enum):
    """部署形态。单机双网卡场景，聚焦离线核心 + 可选联网附加。

    注：原"双机模式 C"在单机双网卡的实际场景下属过度工程，已废弃。
    """

    STANDALONE_WIRED = "standalone_wired"  # A: 单机 + 有线（专网+公网双网卡）
    STANDALONE_WIRELESS = "standalone_wireless"  # B: 单机 + USB 无线/4G
    OFFLINE = "offline"  # D: 纯离线保底（默认底座）


class ModuleToggle(BaseModel):
    """各模块启停开关，由配置文件控制。"""

    crawler: bool = True
    network: bool = True
    scheduler: bool = True
    local_landing: bool = True
    alert_gateway: bool = False  # 需外网
    agent: bool = False  # 可拆卸，需外网
    web_ui: bool = False  # 可选
