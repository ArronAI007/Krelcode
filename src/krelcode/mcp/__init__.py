"""MCP (Model Context Protocol) 客户端：stdio / SSE 双传输。

远程 tools/resources/prompts 经适配器实现本地 Tool 端口，
注册进 ToolRegistry 后与本地工具在 AgentLoop 里无差别执行；
MCPSupervisor 负责配置同步与崩溃热重启。
"""

from .client import MCPClient, MCPServerConfig
from .stdio_transport import StdioTransport
from .sse_transport import SSETransport
from .streamable import StreamableHTTPTransport
from .supervisor import MCPSupervisor
from .tools import MCPTool, discover_tools, sanitize_tool_name, server_prefix
from .transport import MCPError, MCPTransport

__all__ = [
    "MCPClient", "MCPServerConfig", "MCPError", "MCPTransport",
    "StdioTransport", "SSETransport", "StreamableHTTPTransport",
    "MCPTool", "discover_tools", "sanitize_tool_name", "server_prefix",
    "MCPSupervisor",
]
