"""MCPTool：把远程 MCP 工具适配为本地 Tool 端口。

命名规范：<server>__<tool>（非法字符替换为 _），与本地工具同一注册表；
远程 inputSchema 即统一 ToolSpec.parameters，零转换成本。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..tools.base import ToolContext, ToolResult
from .client import MCPClient, MCPError


def sanitize_tool_name(server: str, tool: str) -> str:
    raw = f"{server}__{tool}"
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)


class MCPTool:
    def __init__(self, client: MCPClient, spec: Dict[str, Any]) -> None:
        self.client = client
        self.remote_name: str = str(spec.get("name", ""))
        self.name = sanitize_tool_name(client.name, self.remote_name)
        self.description = str(spec.get("description") or f"MCP 远程工具 {self.remote_name}（服务器: {client.name}）")
        schema = spec.get("inputSchema")
        self.parameters: Dict[str, Any] = schema if isinstance(schema, dict) else {
            "type": "object", "properties": {}}

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await self.client.call_tool(self.remote_name, args)
        except MCPError as e:
            return ToolResult(content=str(e), is_error=True)

        is_error = bool(result.get("isError", False))
        text = _render_content(result.get("content"))
        return ToolResult(content=text, is_error=is_error, data={"mcp": self.client.name})


def _render_content(content: Any) -> str:
    """MCP content blocks → 纯文本（text 直出；image/resource 等留类型占位）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        else:
            parts.append(f"[{btype or 'unknown'} 内容块，暂不支持渲染]")
    return "\n".join(parts)
