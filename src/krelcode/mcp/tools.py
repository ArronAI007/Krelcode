"""MCP → 本地 Tool 端口的适配器族。

- MCPTool: 远程 tools/call
- 资源工具: <server>__list_resources / <server>__read_resource（capabilities.resources）
- 提示词工具: <server>__list_prompts / <server>__get_prompt（capabilities.prompts）
全部注册进同一 ToolRegistry，AgentLoop 对"远程/本地"零感知。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..tools.base import ToolContext, ToolResult
from .client import MCPClient, MCPError


def sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def sanitize_tool_name(server: str, tool: str) -> str:
    return f"{sanitize(server)}__{sanitize(tool)}"


def server_prefix(server: str) -> str:
    """该服务器全部工具名的公共前缀（registry.unregister_prefix 用）。"""
    return sanitize(server) + "__"


def _render_content(content: Any) -> str:
    """MCP content blocks → 纯文本（text 直出；image/resource 等留类型占位）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]  # prompts 消息的 content 可能是单个块而非数组
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


def _error(e: MCPError) -> ToolResult:
    return ToolResult(content=str(e), is_error=True)


class MCPTool:
    """远程工具（tools/call）。"""

    def __init__(self, client: MCPClient, spec: Dict[str, Any]) -> None:
        self.client = client
        self.remote_name: str = str(spec.get("name", ""))
        self.name = sanitize_tool_name(client.name, self.remote_name)
        self.description = str(
            spec.get("description")
            or f"MCP 远程工具 {self.remote_name}（服务器: {client.name}）")
        schema = spec.get("inputSchema")
        self.parameters: Dict[str, Any] = schema if isinstance(schema, dict) else {
            "type": "object", "properties": {}}

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await self.client.call_tool(self.remote_name, args)
        except MCPError as e:
            return _error(e)
        return ToolResult(
            content=_render_content(result.get("content")),
            is_error=bool(result.get("isError", False)),
            data={"mcp": self.client.name},
        )


class MCPListResourcesTool:
    def __init__(self, client: MCPClient) -> None:
        self.client = client
        self.name = sanitize_tool_name(client.name, "list_resources")
        self.description = f"列出 MCP 服务器 {client.name} 提供的资源（uri/名称/类型）。"
        self.parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, args, ctx) -> ToolResult:
        try:
            resources = await self.client.list_resources()
        except MCPError as e:
            return _error(e)
        if not resources:
            return ToolResult(content="（无资源）")
        lines = []
        for r in resources:
            desc = r.get("description")
            lines.append(
                f"{r.get('uri')}  [{r.get('mimeType', '?')}]  {r.get('name', '')}"
                + (f" — {desc}" if desc else ""))
        return ToolResult(content="\n".join(lines), data={"count": len(resources)})


class MCPReadResourceTool:
    def __init__(self, client: MCPClient) -> None:
        self.client = client
        self.name = sanitize_tool_name(client.name, "read_resource")
        self.description = f"读取 MCP 服务器 {client.name} 的资源内容（先 list_resources 拿 uri）。"
        self.parameters = {
            "type": "object",
            "properties": {"uri": {"type": "string", "description": "资源 uri"}},
            "required": ["uri"],
        }

    async def execute(self, args, ctx) -> ToolResult:
        uri = args.get("uri")
        if not isinstance(uri, str) or not uri:
            return ToolResult(content="uri 必须是非空字符串", is_error=True)
        try:
            result = await self.client.read_resource(uri)
        except MCPError as e:
            return _error(e)
        parts = []
        for c in result.get("contents") or []:
            if not isinstance(c, dict):
                continue
            if "text" in c:
                parts.append(str(c["text"]))
            elif "blob" in c:
                parts.append(f"[二进制资源 {c.get('mimeType', '?')}，{len(str(c['blob']))} 字符 base64]")
        return ToolResult(content="\n".join(parts) if parts else "（空资源）")


class MCPListPromptsTool:
    def __init__(self, client: MCPClient) -> None:
        self.client = client
        self.name = sanitize_tool_name(client.name, "list_prompts")
        self.description = f"列出 MCP 服务器 {client.name} 提供的提示词模板及参数。"
        self.parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, args, ctx) -> ToolResult:
        try:
            prompts = await self.client.list_prompts()
        except MCPError as e:
            return _error(e)
        if not prompts:
            return ToolResult(content="（无提示词模板）")
        lines = []
        for p in prompts:
            arg_names = [a.get("name") for a in (p.get("arguments") or [])]
            lines.append(
                f"{p.get('name')}({', '.join(arg_names)})"
                + (f" — {p['description']}" if p.get("description") else ""))
        return ToolResult(content="\n".join(lines), data={"count": len(prompts)})


class MCPGetPromptTool:
    def __init__(self, client: MCPClient) -> None:
        self.client = client
        self.name = sanitize_tool_name(client.name, "get_prompt")
        self.description = (
            f"获取 MCP 服务器 {client.name} 的提示词模板渲染结果"
            "（返回一组 role+text 消息，可作为任务指令参考）。")
        self.parameters = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "提示词模板名（先 list_prompts 查看）"},
                "arguments": {"type": "object", "description": "模板参数键值对"},
            },
            "required": ["name"],
        }

    async def execute(self, args, ctx) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return ToolResult(content="name 必须是非空字符串", is_error=True)
        arguments = args.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return ToolResult(content="arguments 必须是对象", is_error=True)
        try:
            result = await self.client.get_prompt(name, arguments)
        except MCPError as e:
            return _error(e)
        lines = []
        if result.get("description"):
            lines.append(f"# {result['description']}")
        for m in result.get("messages") or []:
            role = m.get("role", "?")
            text = _render_content(m.get("content"))
            lines.append(f"[{role}] {text}")
        return ToolResult(content="\n".join(lines) if lines else "（空模板）")


async def discover_tools(client: MCPClient) -> List[Any]:
    """按服务器声明的 capabilities 发现并包装全部可用工具。"""
    tools: List[Any] = []
    if client.supports("tools"):
        for spec in await client.list_tools():
            tools.append(MCPTool(client, spec))
    if client.supports("resources"):
        tools.append(MCPListResourcesTool(client))
        tools.append(MCPReadResourceTool(client))
    if client.supports("prompts"):
        tools.append(MCPListPromptsTool(client))
        tools.append(MCPGetPromptTool(client))
    return tools
