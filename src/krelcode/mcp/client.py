"""MCP 客户端：JSON-RPC 2.0 协议层（传输无关）。

传输经 MCPTransport 端口注入：command → StdioTransport；
url → StreamableHTTPTransport（默认，2025-03-26）或 SSETransport（旧规范）。
生命周期：start()（connect + initialize 握手）→ 各 API → close()。
失败安全：传输断开时所有 pending 请求立即以 MCPError 失败，后续调用直接报错。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import __version__
from ..agent.prompt_surface_contract import (
    MCP_PROMPT_SURFACE_CONTRACT,
    PromptSurfaceContract,
    PromptSurfaceExecutor,
    observe_surface_result,
)
from ..core.redact import SecretRedactor
from .stdio_transport import StdioTransport
from .sse_transport import SSETransport
from .streamable import StreamableHTTPTransport
from .oauth import MCPOAuthRequired, OAuthTokenManager
from .transport import MCPAuthRequired, MCPError, MCPTransport
from .security import sanitize_mcp_prompt_result

# url 型服务器的传输选择（config: transport = "..."）
TRANSPORT_STREAMABLE = "streamable-http"
TRANSPORT_SSE = "sse"

PROTOCOL_VERSION = "2025-03-26"
MAX_LIST_PAGES = 100


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    # stdio 传输
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict, repr=False)
    # HTTP 传输（url 优先于 command）
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict, repr=False)
    oauth: Dict[str, Any] = field(default_factory=dict, repr=False)
    # "streamable-http"（默认，2025-03-26 规范）或 "sse"（旧 2024-11-05 规范）
    transport: Optional[str] = None
    # streamable-http GET 监听流重连（0 = 不重连）
    listener_retries: int = 3
    listener_retry_delay: float = 1.0
    # 通用
    timeout: float = 30.0

    def create_transport(self) -> MCPTransport:
        if self.url:
            kind = (self.transport or TRANSPORT_STREAMABLE).lower()
            if kind in (TRANSTREAMABLE_ALIASES := ("streamable-http", "http", "streamable", "auto")):
                return StreamableHTTPTransport(
                    self.url, self.headers, self.timeout,
                    listener_retries=self.listener_retries,
                    listener_retry_delay=self.listener_retry_delay,
                )
            if kind == TRANSPORT_SSE:
                return SSETransport(self.url, self.headers, self.timeout)
            raise MCPError(
                f"MCP 服务器 {self.name!r} 未知 transport: {self.transport!r}"
                f"（可选: {', '.join(TRANSTREAMABLE_ALIASES + (TRANSPORT_SSE,))}）")
        if self.command:
            return StdioTransport(self.command, self.args, self.env)
        raise MCPError(f"MCP 服务器 {self.name!r} 配置缺少 url 或 command")


class MCPClient:
    def __init__(
        self,
        config: MCPServerConfig,
        transport: Optional[MCPTransport] = None,
        sampler: Optional[Any] = None,
        roots: Optional[List[Dict[str, Any]]] = None,
        on_notification: Optional[
            Callable[[str, Dict[str, Any]], Any]
        ] = None,
        oauth_token_path: Optional[Path] = None,
        log: Optional[Callable[[str], None]] = None,
        redactor: Optional[SecretRedactor] = None,
        prompt_surface_registry: Optional[Any] = None,
    ) -> None:
        """sampler: async (messages, params) -> {"text": str, "model": str}，
        服务器发起 sampling/createMessage 时回调（通常由共享 LLM 实现）；
        roots: roots/list 应答的工作区根列表 [{"uri","name"}]。"""
        self.config = config
        self.name = config.name
        self._transport: MCPTransport = transport or config.create_transport()
        self._sampler = sampler
        self._roots = list(roots or [])
        self._on_notification = on_notification
        self._log = log or (lambda _message: None)
        self._redactor = redactor
        self._prompt_surface_registry = prompt_surface_registry
        self._prompt_contract: PromptSurfaceContract = (
            MCP_PROMPT_SURFACE_CONTRACT.with_limits(
                timeout_seconds=self.config.timeout))
        self._surface_executor = PromptSurfaceExecutor()
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._started = False
        self._closed = False
        self._disconnect_reason = ""
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.instructions: str = ""
        self.server_protocol_version: Optional[str] = None
        self.oauth: Optional[OAuthTokenManager] = None
        if self.config.url and self.config.oauth:
            candidate = OAuthTokenManager(
                self.name,
                self.config.url,
                self.config.oauth,
                token_path=oauth_token_path,
                log=self._log,
            )
            if candidate.config.configured:
                self.oauth = candidate

    # -- 生命周期 ---------------------------------------------------------------

    async def start(self) -> None:
        await self._apply_auth_headers()
        await self._transport.connect(self._dispatch, self._on_disconnect)
        self._started = True
        client_caps: Dict[str, Any] = {}
        if self._sampler is not None:
            client_caps["sampling"] = {}
        if self._roots:
            client_caps["roots"] = {"listChanged": False}
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": client_caps,
            "clientInfo": {"name": "krelcode", "version": __version__},
        })
        if isinstance(result, dict):
            self.server_info = result.get("serverInfo", {}) or {}
            self.capabilities = result.get("capabilities", {}) or {}
            self.instructions = str(result.get("instructions") or "")
            self.server_protocol_version = result.get("protocolVersion")
            # 后续 HTTP 请求用协商版本填充 MCP-Protocol-Version（streamable）
            setter = getattr(self._transport, "set_protocol_version", None)
            if setter is not None and self.server_protocol_version:
                setter(self.server_protocol_version)
        await self._notify("notifications/initialized")

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

    async def _apply_auth_headers(self, *, force_refresh: bool = False) -> None:
        if self.oauth is None:
            return
        headers = {
            **self.config.headers,
            **(await self.oauth.headers(force_refresh=force_refresh)),
        }
        setter = getattr(self._transport, "set_headers", None)
        if setter is None:
            raise MCPError("当前 MCP 传输不支持 OAuth Header")
        setter(headers)

    async def _request(self, method: str, params: Optional[dict] = None) -> Any:
        if self._closed or not self._started:
            raise MCPError(f"MCP 服务器 {self.name!r} 未连接")
        if not self._transport.alive:
            raise MCPError(
                f"MCP 服务器 {self.name!r} 连接已断开（{self._disconnect_reason}）")
        auth_retry = False
        while True:
            self._next_id += 1
            rid = self._next_id
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[rid] = future
            payload = {
                "jsonrpc": "2.0", "id": rid, "method": method,
                "params": params or {},
            }
            try:
                await self._transport.send(payload)
            except MCPAuthRequired as exc:
                self._pending.pop(rid, None)
                if self.oauth is None or auth_retry:
                    raise
                self.oauth.note_challenge(exc.headers)
                try:
                    await self._apply_auth_headers(force_refresh=True)
                except MCPOAuthRequired as auth_exc:
                    raise auth_exc
                except MCPError:
                    raise MCPAuthRequired(
                        f"{exc}；运行 /mcp auth {self.name} 完成授权",
                        status=exc.status,
                        headers=exc.headers,
                    ) from exc
                auth_retry = True
                continue
            except MCPError:
                self._pending.pop(rid, None)
                raise
            try:
                return await asyncio.wait_for(future, timeout=self.config.timeout)
            except asyncio.TimeoutError:
                self._pending.pop(rid, None)
                raise MCPError(
                    f"MCP 服务器 {self.name!r} 响应超时"
                    f"（{self.config.timeout:g}s, method={method}）"
                ) from None

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        """发送通知并等待写盘完成。

        必须 await（而非 create_task fire-and-forget）：MCP 规范要求
        notifications/initialized 先于一切后续请求到达服务器，
        调度延迟会造成乱序（曾经让 stub 把迟到通知误当反向请求应答）。
        """
        if self._closed or not self._transport.alive:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            await self._transport.send(payload)
        except MCPError:
            pass  # 通知失败不致命

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        if method is not None and rid is not None:
            # 服务器主动请求（sampling/roots/ping…）：调度异步处理并应答
            try:
                asyncio.get_running_loop().create_task(
                    self._handle_server_request(msg))
            except RuntimeError:
                pass  # 无运行循环（已关闭）
            return
        if rid is None:
            if method is not None:
                try:
                    asyncio.get_running_loop().create_task(
                        self._handle_notification(str(method), msg.get("params") or {}))
                except RuntimeError:
                    pass
            return
        future = self._pending.pop(rid, None)
        if future is None or future.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            future.set_exception(
                MCPError(f"MCP {self.name!r} 错误 {err.get('code')}: {err.get('message')}"))
        else:
            future.set_result(msg.get("result"))

    async def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        if self._on_notification is None:
            return
        try:
            pending = self._on_notification(method, params)
            if asyncio.iscoroutine(pending):
                await pending
        except Exception as exc:
            self._log(
                f"[mcp] {self.name} 通知处理失败 {method}: "
                f"{type(exc).__name__}: {exc}")

    # -- 服务器 → 客户端 请求处理 -------------------------------------------------

    async def _handle_server_request(self, msg: Dict[str, Any]) -> None:
        method = str(msg.get("method"))
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "sampling/createMessage":
            await self._handle_sampling(mid, params)
        elif method == "roots/list":
            await self._send_response(mid, result={"roots": list(self._roots)})
        elif method == "ping":
            await self._send_response(mid, result={})
        else:
            await self._send_response(
                mid, error={"code": -32601,
                            "message": f"method not supported by krelcode: {method}"})

    async def _handle_sampling(self, mid: Any, params: Dict[str, Any]) -> None:
        if self._sampler is None:
            await self._send_response(
                mid, error={"code": -32601, "message": "sampling not supported"})
            return
        try:
            out = await self._sampler(params.get("messages") or [], params)
            text = (out or {}).get("text", "")
            model = (out or {}).get("model", "")
        except Exception as e:
            await self._send_response(
                mid, error={"code": -32603, "message": f"sampling failed: {e}"})
            return
        await self._send_response(mid, result={
            "role": "assistant",
            "content": {"type": "text", "text": text},
            "model": model,
        })

    async def _send_response(self, mid: Any, result: Any = None,
                             error: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        try:
            await self._transport.send(payload)
        except MCPError:
            pass  # 应答失败不致命（连接已断时 pending 自会失败）

    def _on_disconnect(self, reason: str) -> None:
        self._disconnect_reason = reason
        self._fail_all_pending(f"MCP 服务器 {self.name!r}: {reason}")

    def _fail_all_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError(reason))
        self._pending.clear()

    # -- MCP 高层 API --------------------------------------------------------------

    async def _list_paginated(self, method: str, key: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen: set[str] = set()
        for _ in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self._request(method, params)
            if result is not None and not isinstance(result, dict):
                raise MCPError(
                    f"MCP 服务器 {self.name!r} 返回了非法 {method} 结果")
            payload = result or {}
            items.extend(_as_list(payload.get(key), self.name, key))
            raw_cursor = payload.get("nextCursor")
            if raw_cursor in (None, ""):
                return items
            next_cursor = str(raw_cursor)
            if next_cursor in seen:
                raise MCPError(
                    f"MCP 服务器 {self.name!r} 的 {method} 分页 cursor 重复")
            seen.add(next_cursor)
            cursor = next_cursor
        raise MCPError(
            f"MCP 服务器 {self.name!r} 的 {method} 超过 {MAX_LIST_PAGES} 页")

    async def list_tools(self) -> List[Dict[str, Any]]:
        return await self._list_paginated("tools/list", "tools")

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError(f"MCP 服务器 {self.name!r} 返回了非法 tools/call 结果")
        return result

    async def list_resources(self) -> List[Dict[str, Any]]:
        return await self._list_paginated("resources/list", "resources")

    async def list_resource_templates(self) -> List[Dict[str, Any]]:
        return await self._list_paginated(
            "resources/templates/list", "resourceTemplates")

    async def read_resource(self, uri: str) -> Dict[str, Any]:
        result = await self._request("resources/read", {"uri": uri})
        if not isinstance(result, dict):
            raise MCPError(f"MCP 服务器 {self.name!r} 返回了非法 resources/read 结果")
        return result

    async def list_prompts(self) -> List[Dict[str, Any]]:
        return await self._list_paginated("prompts/list", "prompts")

    async def get_prompt(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        safe_arguments = arguments if isinstance(arguments, dict) else {}

        async def run_surface(safe_input: object) -> object:
            data = safe_input if isinstance(safe_input, dict) else {}
            return await self._request("prompts/get", {
                "name": str(data.get("name", "")),
                "arguments": (
                    data.get("arguments")
                    if isinstance(data.get("arguments"), dict) else {}),
            })

        surface = await self._surface_executor.execute(
            f"mcp-prompt:{self.name}:{name}",
            self._prompt_contract,
            {"server": self.name, "name": name, "arguments": safe_arguments},
            run_surface,
            redactor=self._redactor,
            observer=lambda _surface_id, result: observe_surface_result(
                self._prompt_surface_registry, result),
        )
        if not surface.ok or not isinstance(surface.value, dict):
            raise MCPError(
                f"MCP 服务器 {self.name!r} 的 Prompt {name!r} "
                f"未通过 Surface Contract（{surface.status}）: {surface.error}")
        # The contract validates and redacts the remote payload.  The final
        # projection additionally removes control/bidi characters, neutralizes
        # instruction-shaped descriptions and bounds every model-visible text
        # field without changing the prompts/get wire shape.
        return sanitize_mcp_prompt_result(
            surface.value, redactor=self._redactor)


def _as_list(value: Any, server: str, what: str) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MCPError(f"MCP 服务器 {server!r} 返回了非法 {what} 结构")
    return value
