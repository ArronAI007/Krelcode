"""MCP 客户端：JSON-RPC 2.0 协议层（传输无关）。

传输经 MCPTransport 端口注入：command → StdioTransport，url → SSETransport。
生命周期：start()（connect + initialize 握手）→ 各 API → close()。
失败安全：传输断开时所有 pending 请求立即以 MCPError 失败，后续调用直接报错。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .. import __version__
from .stdio_transport import StdioTransport
from .sse_transport import SSETransport
from .transport import MCPError, MCPTransport

PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    # stdio 传输
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    # SSE 传输（url 优先于 command）
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    # 通用
    timeout: float = 30.0

    def create_transport(self) -> MCPTransport:
        if self.url:
            return SSETransport(self.url, self.headers, self.timeout)
        if self.command:
            return StdioTransport(self.command, self.args, self.env)
        raise MCPError(f"MCP 服务器 {self.name!r} 配置缺少 url 或 command")


class MCPClient:
    def __init__(self, config: MCPServerConfig, transport: Optional[MCPTransport] = None) -> None:
        self.config = config
        self.name = config.name
        self._transport: MCPTransport = transport or config.create_transport()
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._started = False
        self._closed = False
        self._disconnect_reason = ""
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}

    # -- 生命周期 ---------------------------------------------------------------

    async def start(self) -> None:
        await self._transport.connect(self._dispatch, self._on_disconnect)
        self._started = True
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "krelcode", "version": __version__},
        })
        if isinstance(result, dict):
            self.server_info = result.get("serverInfo", {}) or {}
            self.capabilities = result.get("capabilities", {}) or {}
        self._notify("notifications/initialized")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._transport.close()
        self._fail_all_pending("MCP 客户端已关闭")

    @property
    def alive(self) -> bool:
        return self._started and not self._closed and self._transport.alive

    def supports(self, capability: str) -> bool:
        """服务器在 initialize 中声明的能力（tools/resources/prompts）。"""
        return capability in self.capabilities

    # -- JSON-RPC 核心 --------------------------------------------------------------

    async def _request(self, method: str, params: Optional[dict] = None) -> Any:
        if self._closed or not self._started:
            raise MCPError(f"MCP 服务器 {self.name!r} 未连接")
        if not self._transport.alive:
            raise MCPError(
                f"MCP 服务器 {self.name!r} 连接已断开（{self._disconnect_reason}）")
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        try:
            await self._transport.send(payload)
        except MCPError:
            self._pending.pop(rid, None)
            raise
        try:
            return await asyncio.wait_for(future, timeout=self.config.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise MCPError(
                f"MCP 服务器 {self.name!r} 响应超时（{self.config.timeout:g}s, method={method}）"
            ) from None

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        if self._closed or not self._transport.alive:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            # 通知不等待响应；send 是协程，fire-and-forget 交由事件循环调度
            asyncio.get_running_loop().create_task(self._safe_send(payload))
        except RuntimeError:
            pass  # 无运行循环（已关闭）

    async def _safe_send(self, payload: dict) -> None:
        try:
            await self._transport.send(payload)
        except MCPError:
            pass  # 通知失败不致命

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        rid = msg.get("id")
        if rid is None or "method" in msg:
            return  # 服务器通知/请求：当前忽略
        future = self._pending.pop(rid, None)
        if future is None or future.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            future.set_exception(
                MCPError(f"MCP {self.name!r} 错误 {err.get('code')}: {err.get('message')}"))
        else:
            future.set_result(msg.get("result"))

    def _on_disconnect(self, reason: str) -> None:
        self._disconnect_reason = reason
        self._fail_all_pending(f"MCP 服务器 {self.name!r}: {reason}")

    def _fail_all_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError(reason))
        self._pending.clear()

    # -- MCP 高层 API --------------------------------------------------------------

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._request("tools/list")
        return _as_list((result or {}).get("tools"), self.name, "tools")

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError(f"MCP 服务器 {self.name!r} 返回了非法 tools/call 结果")
        return result

    async def list_resources(self) -> List[Dict[str, Any]]:
        result = await self._request("resources/list")
        return _as_list((result or {}).get("resources"), self.name, "resources")

    async def read_resource(self, uri: str) -> Dict[str, Any]:
        result = await self._request("resources/read", {"uri": uri})
        if not isinstance(result, dict):
            raise MCPError(f"MCP 服务器 {self.name!r} 返回了非法 resources/read 结果")
        return result

    async def list_prompts(self) -> List[Dict[str, Any]]:
        result = await self._request("prompts/list")
        return _as_list((result or {}).get("prompts"), self.name, "prompts")

    async def get_prompt(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = await self._request(
            "prompts/get", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise MCPError(f"MCP 服务器 {self.name!r} 返回了非法 prompts/get 结果")
        return result


def _as_list(value: Any, server: str, what: str) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MCPError(f"MCP 服务器 {server!r} 返回了非法 {what} 结构")
    return value
