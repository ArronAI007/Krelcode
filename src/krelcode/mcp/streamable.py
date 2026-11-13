"""StreamableHTTPTransport：MCP 2025-03-26 Streamable HTTP 传输。

协议要点（全部在本文件消化，MCPClient 只管 JSON-RPC）：
- 单一端点：所有客户端消息 POST 到同一 url，`Accept: application/json, text/event-stream`
- 响应二态：Content-Type=application/json → 单条 JSON-RPC 响应；
  text/event-stream → 本次请求的 SSE 流（响应+可能的服务器请求混排）
- 会话：服务器可在响应头下发 `Mcp-Session-Id`，之后所有请求携带；
  POST 返回 404 = 会话过期 → 触发 on_disconnect（fail-closed）
- 通知：202 Accepted 无响应体
- 服务器主动请求（流内 method+id）：统一上抛 MCPClient 处理
  （sampling/roots/ping 应答，未知方法回 -32601）
- GET 监听流（可选能力）：首个 POST 往返后惰性启动；断流后指数退避重连，
  携带 `Last-Event-ID` 让服务器补发断线期间的事件（resumability）
- 会话终止：close() 尽力发送 DELETE（带 Mcp-Session-Id），405/网络失败容忍
- 协议版本：请求携带 `MCP-Protocol-Version`（2025-06-18 起的强制头，旧服务器忽略）

线程模型：POST 往返与 GET 监听都跑在线程里，消息经
loop.call_soon_threadsafe 回投事件循环（Future 不能在外部线程 set_result）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Callable, Dict, Optional

from .http_connector import HTTPConnector, UrllibHTTPConnector
from .sseparse import SSEEventParser
from .transport import MCPError

ACCEPT_BOTH = "application/json, text/event-stream"


class StreamableHTTPTransport:
    kind = "streamable-http"

    def __init__(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
        connector: Optional[HTTPConnector] = None,
        protocol_version: str = "2025-03-26",
        listener_retries: int = 3,
        listener_retry_delay: float = 1.0,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._connector = connector or UrllibHTTPConnector()
        self.protocol_version = protocol_version
        # GET 监听流断线重连：次数与基础退避（秒），测试可注入更小值
        self.listener_retries = max(0, int(listener_retries))
        self.listener_retry_delay = max(0.0, float(listener_retry_delay))
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_message: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_disconnect: Optional[Callable[[str], None]] = None
        self._session_id: Optional[str] = None
        self._last_event_id: Optional[str] = None
        self._server_retry_ms: Optional[int] = None
        self._closed = False
        self._expired = False
        self._listener_tried = False

    # -- MCPTransport 端口 -----------------------------------------------------------

    async def connect(self, on_message, on_disconnect) -> None:
        # Streamable HTTP 无需预建连接（POST 惰性发起）；只记录回调与事件循环
        self._loop = asyncio.get_running_loop()
        self._on_message = on_message
        self._on_disconnect = on_disconnect

    async def send(self, payload: Dict[str, Any]) -> None:
        if self._closed:
            raise MCPError("streamable-http 传输已关闭")
        if self._expired:
            raise MCPError("MCP 会话已过期（HTTP 404），需要重连")
        await asyncio.to_thread(self._sync_roundtrip, payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # 先关闭 GET 监听流，避免与 DELETE 共用连接互相阻塞
        try:
            self._connector.close_stream()
        except Exception:
            pass
        # 尽力终止服务端会话（规范允许 405/404；失败静默）
        if self._session_id:
            try:
                await asyncio.to_thread(self._terminate_session)
            except Exception:
                pass

    @property
    def alive(self) -> bool:
        return not self._closed and not self._expired

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def last_event_id(self) -> Optional[str]:
        return self._last_event_id

    def set_protocol_version(self, version: Optional[str]) -> None:
        """initialize 协商出的版本：后续请求用它填充 MCP-Protocol-Version。"""
        if version:
            self.protocol_version = str(version)

    # -- POST 往返（线程内） -------------------------------------------------------------

    def _sync_roundtrip(self, payload: Dict[str, Any]) -> None:
        is_notification = "id" not in payload
        headers = self._request_headers(accept=ACCEPT_BOTH)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = self._connector.request("POST", self.url, headers, body, self.timeout)
        try:
            self._capture_session(resp.headers)
            status = resp.status

            if status == 404 and self._session_id is not None:
                # 会话过期：标记 + 断连回调 + 抛错（三重保险，fail-closed）
                self._expired = True
                reason = "MCP 会话已过期（HTTP 404）"
                self._emit_disconnect(reason)
                raise MCPError(reason)
            if status == 202 or (is_notification and 200 <= status < 300):
                return  # 通知：无响应体
            if not 200 <= status < 300:
                err = resp.read().decode("utf-8", "replace")
                raise MCPError(f"streamable-http POST 失败 HTTP {status}: {err[:300]}")

            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" in ctype:
                self._consume_sse(resp)
            else:
                raw = resp.read()
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError as e:
                    raise MCPError(f"streamable-http 响应非法 JSON: {e}") from e
                self._dispatch_incoming(msg)
        finally:
            try:
                resp.close()
            except Exception:
                pass
            self._maybe_start_listener()

    def _consume_sse(self, resp) -> None:
        parser = SSEEventParser()
        for raw in resp.iter_lines():
            if self._closed:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            event = parser.feed(line)
            if event is not None:
                self._track_event(parser)
                self._handle_sse_event(event[0], event[1])
        tail = parser.flush()
        if tail is not None:
            self._track_event(parser)
            self._handle_sse_event(tail[0], tail[1])

    def _track_event(self, parser: SSEEventParser) -> None:
        """记录最新 SSE id / 服务器建议的重连间隔，供断线恢复使用。"""
        if parser.last_event_id is not None:
            self._last_event_id = parser.last_event_id
        if parser.retry_ms is not None:
            self._server_retry_ms = parser.retry_ms

    def _handle_sse_event(self, event_name: str, data: str) -> None:
        if data.strip() == "[DONE]":
            return
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return  # 忽略非法事件
        self._dispatch_incoming(msg)

    # -- 消息分发与服务器请求应答 ----------------------------------------------------------

    def _dispatch_incoming(self, msg: Any) -> None:
        # 全部消息（含服务器主动请求）统一上抛 MCPClient：
        # sampling/roots/ping 由 client._handle_server_request 应答
        if isinstance(msg, list):
            # 2025-03-26 允许 JSON-RPC batch 响应（2025-06-18 移除，仍兼容）
            for item in msg:
                if isinstance(item, dict):
                    self._emit_message(item)
        elif isinstance(msg, dict):
            self._emit_message(msg)

    def _emit_message(self, msg: Dict[str, Any]) -> None:
        if self._on_message is None:
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._on_message, msg)
            except RuntimeError:
                pass
        else:
            self._on_message(msg)

    def _emit_disconnect(self, reason: str) -> None:
        if self._on_disconnect is None:
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._on_disconnect, reason)
            except RuntimeError:
                pass
        else:
            self._on_disconnect(reason)

    # -- 会话与请求头 --------------------------------------------------------------------

    def _capture_session(self, headers: Dict[str, str]) -> None:
        sid = headers.get("mcp-session-id")
        if sid and self._session_id is None:
            self._session_id = sid

    def _request_headers(self, accept: str,
                         last_event_id: bool = False) -> Dict[str, str]:
        headers = {
            "Accept": accept,
            "Content-Type": "application/json",
            # 2025-06-18 起为强制头；旧服务器忽略未知头，向后兼容
            "MCP-Protocol-Version": self.protocol_version,
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if last_event_id and self._last_event_id:
            headers["Last-Event-ID"] = self._last_event_id
        return headers

    def _terminate_session(self) -> None:
        """尽力 DELETE 终止服务端会话；405/404/网络失败均静默容忍。"""
        headers = self._request_headers(accept="application/json")
        headers.pop("Content-Type", None)
        try:
            resp = self._connector.request(
                "DELETE", self.url, headers, None, self.timeout)
        except Exception:
            return
        try:
            resp.close()
        except Exception:
            pass

    # -- GET 监听流（可选能力，惰性启动 + 断线重连） --------------------------------------------

    def _maybe_start_listener(self) -> None:
        if self._listener_tried or self._closed:
            return
        self._listener_tried = True
        thread = threading.Thread(
            target=self._listener_loop, daemon=True,
            name="krelcode-mcp-streamable-listener")
        thread.start()

    def _listener_loop(self) -> None:
        attempt = 0
        while not self._closed:
            resp = None
            try:
                headers = self._request_headers(
                    accept="text/event-stream", last_event_id=True)
                headers.pop("Content-Type", None)
                resp = self._connector.request(
                    "GET", self.url, headers, None, self.timeout)
            except Exception:
                resp = None
            if resp is None:
                attempt += 1
                if attempt > self.listener_retries or self._wait_listener_delay(attempt):
                    return
                continue
            if resp.status != 200:
                try:
                    resp.close()
                except Exception:
                    pass
                if resp.status in (401, 403, 405):
                    return  # 不支持/无权限：静默容忍
                attempt += 1
                if attempt > self.listener_retries or self._wait_listener_delay(attempt):
                    return
                continue
            attempt = 0
            try:
                parser = SSEEventParser()
                for raw in resp.iter_lines():
                    if self._closed:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    event = parser.feed(line)
                    if event is not None:
                        self._track_event(parser)
                        self._handle_sse_event(event[0], event[1])
                tail = parser.flush()
                if tail is not None:
                    self._track_event(parser)
                    self._handle_sse_event(tail[0], tail[1])
            except Exception:
                pass  # 监听流断开不影响 POST 主通道
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            if self._closed:
                return
            attempt += 1
            if attempt > self.listener_retries or self._wait_listener_delay(attempt):
                return

    def _wait_listener_delay(self, attempt: int) -> bool:
        """指数退避（尊重服务器 retry: 建议），返回 True 表示已关闭应退出。"""
        base = self.listener_retry_delay
        if self._server_retry_ms is not None:
            base = self._server_retry_ms / 1000.0
        delay = min(base * (2 ** max(0, attempt - 1)), 30.0)
        deadline = time.monotonic() + delay
        while not self._closed and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return self._closed
