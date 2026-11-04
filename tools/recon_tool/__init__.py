"""现场侦察工具：一次现场，离线开发。

复用 ``spider_gd.infrastructure.network`` 的网络核心，叠加 Playwright HAR
全量录制与 HAR 分析，产出可离线回放的侦察包。
"""

from __future__ import annotations

from recon_tool.har_analyzer import analyze_har, extract_auth

__all__ = ["analyze_har", "extract_auth"]
