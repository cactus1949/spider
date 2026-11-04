"""MCP server 子包：通过 streamable-http 暴露只读 tool 供 OpenClaw 调用。"""

from __future__ import annotations

from spider_gd.gateway.mcp.server import create_mcp_server

__all__ = ["create_mcp_server"]
