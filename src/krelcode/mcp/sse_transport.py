"""SSETransport：MCP 的 HTTP+SSE 传输（2024-11-05 规范）。

协议：
- GET <url> 建立 text/event-stream 长连接（服务器→客户端通道）
- 首个 `event: endpoint` 给出 POST 目标（相对路径按 urljoin 解析）
- 之后服务器经 `event: message` 推送 JSON-RPC 消息
- 客户端所有 JSON-RPC 消息 POST 到 endpoint（响应仍从 SSE 流回来）

实现：reader 跑在独立线程（urllib 阻塞流），经 loop.call_soon_threadsafe
回投 asyncio；connector 可注入（单测零网络），默认 urllib 实现。
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterator, Optional, Protocol, Tuple

from .sseparse import SSEEventParser
from .transport import MCPError

Headers = Dict[str, str]


class SSEConnector(Protocol):
    """SSE 连接器端口（同步接口，由 transport 在线程中调用）。"""

    def open_stream(self, url: str, headers: Headers, timeout: float
                    ) -> Tuple[int, Headers, Iterator[bytes]]:
        """GET 建立 SSE 流；返回 (status, headers, 行迭代器)。"""
        ...

    def post(self, url: str, headers: Headers, body: bytes, timeout: float
             ) -> Tuple[int, bytes]:
        """POST JSON-RPC 消息；返回 (status, 响应体)。"""
        ...

    def close_stream(self) -> None:
        """关闭 GET 流（幂等）。"""
        ...


class UrllibSSEConnector:
    """默认连接器：标准库 urllib。"""

    def __init__(self) -> None:
        self._resp = None
        self._lock = threading.Lock()

    def open_stream(self, url, headers, timeout):
        req = urllib.request.Request(url, headers={**headers, "Accept": "text/event-stream"})
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            return e.code, {}, iter([e.read()])
        except urllib.error.URLError as e:
            raise MCPError(f"SSE 连接失败: {e.reason}") from e
        with self._lock:
            self._resp = resp
        resp_headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        return resp.status, resp_headers, iter(resp.readline, b"")

    def post(self, url, headers, body, timeout):
        req = urllib.request.Request(
            url, data=body,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            raise MCPError(f"SSE POST 失败: {e.reason}") from e

    def close_stream(self) -> None:
        with self._lock:
            resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


class SSETransport:
    kind = "sse"

    def __init__(
        self,
        url: str,
        headers: Optional[Headers] = None,
        timeout: float = 30.0,
        connector: Optional[Any] = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._connector = connector or UrllibSSEConnector()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._endpoint: Optional[str] = None
        self._endpoint_ready = threading.Event()
        self._closed = False
        self._disconnected = False
        self._disconnect_reason = ""

    # -- MCPTransport 端口 ------------------------------------------------------

    async def connect(self, on_message, on_disconnect) -> None:
        self._loop = asyncio.get_running_loop()
        status, _, lines = await asyncio.to_thread(
            self._connector.open_stream, self.url, self.headers, self.timeout)
        if status != 200:
            raise MCPError(f"SSE 连接被拒绝: HTTP {status}")
        self._reader_thread = threading.Thread(
            target=self._reader, args=(lines, on_message, on_disconnect),
            daemon=True, name="krelcode-sse-reader")
        self._reader_thread.start()

    async def send(self, payload: Dict[str, Any]) -> None:
        if self._closed or self._disconnected:
            raise MCPError(f"SSE 传输不可用（{self._disconnect_reason or '已关闭'}）")
        if not await asyncio.to_thread(self._endpoint_ready.wait, self.timeout):
            raise MCPError("SSE 服务器未下发 endpoint 事件（等待超时）")
        assert self._endpoint is not None
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, resp_body = await asyncio.to_thread(
            self._connector.post, self._endpoint, self.headers, body, self.timeout)
        if status not in (200, 202, 204):
            raise MCPError(f"SSE POST 失败 HTTP {status}: {resp_body[:500]!r}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connector.close_stream()
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        return not self._closed and not self._disconnected

    # -- reader 线程 --------------------------------------------------------------

    def _reader(self, lines, on_message, on_disconnect) -> None:
        parser = SSEEventParser()
        try:
            for raw in lines:
                if self._closed:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                event = parser.feed(line)
                if event is not None:
                    self._handle_event(event[0], event[1], on_message)
            tail = parser.flush()
            if tail is not None:
                self._handle_event(tail[0], tail[1], on_message)
            reason = "SSE 流已结束"
        except Exception as e:
            reason = f"SSE 读取异常: {e}"
        self._disconnected = True
        self._disconnect_reason = reason
        if not self._closed and self._loop is not None:
            self._safe_call(on_disconnect, reason)

    def _handle_event(self, event_name: str, data: str, on_message) -> None:
        if event_name == "endpoint":
            self._endpoint = urllib.parse.urljoin(self.url, data.strip())
            self._endpoint_ready.set()
            return
        # message（或无 event 字段）事件：JSON-RPC 消息
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        if self._loop is not None:
            self._safe_call(on_message, msg)

    def _safe_call(self, fn: Callable, *args) -> None:
        """reader 线程 → 事件循环线程安全回投。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass  # 循环已关闭
