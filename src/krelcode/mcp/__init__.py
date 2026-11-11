"""MCP (Model Context Protocol) 客户端：stdio JSON-RPC 2.0。

远程工具经 MCPTool 适配器实现本地 Tool 端口，
注册进 ToolRegistry 后与本地工具在 AgentLoop 里无差别执行。
"""

from .client import MCPClient, MCPError, MCPServerConfig
from .tools import MCPTool, sanitize_tool_name

__all__ = ["MCPClient", "MCPError", "MCPServerConfig", "MCPTool", "sanitize_tool_name"]
