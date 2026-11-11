"""SSETransport 测试。

单元：FakeConnector 内存模拟 SSE 服务器（endpoint 事件/消息事件/POST 配对），
零 socket、零网络；集成：真实本地 http.server（沙箱禁 bind 时自动跳过）。
"""

import asyncio
import json
import queue
import socket
import threading
from pathlib import Path

import pytest

from krelcode.mcp.client import MCPClient, MCPServerConfig
from krelcode.mcp.sse_transport import SSETransport
from krelcode.mcp.transport import MCPError


# -- Fake SSE 服务器（内存 connector）----------------------------------------------------

class FakeSSEServer:
    """实现 SSEConnector 端口：GET 流由内存队列驱动，POST 生成响应事件。"""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.posts = []          # [(url, payload)]
        self.closed = False

    # connector 端口
    def open_stream(self, url, headers, timeout):
        for line in (b"event: endpoint\n", b"data: /messages?sid=42\n", b"\n"):
            self.q.put(line)
        return 200, {"content-type": "text/event-stream"}, self._iter()

    def post(self, url, headers, body, timeout):
        msg = json.loads(body)
        self.posts.append((url, msg))
        resp = self.handle(msg)
        if resp is not None:
            self.emit(resp)
        return 202, b""

    def close_stream(self):
        self.closed = True
        self.q.put(None)

    # 内部
    def _iter(self):
        while True:
            try:
                item = self.q.get(timeout=10)
            except queue.Empty:
                return
            if item is None:
                return
            yield item

    def emit(self, resp):
        data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        for line in (b"event: message\n", b"data: " + data + b"\n", b"\n"):
            self.q.put(line)

    def handle(self, msg):
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sse-stub", "version": "0.1"}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "ping", "description": "p",
                 "inputSchema": {"type": "object", "properties": {}}}]}}
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "pong"}]}}
        if method and str(method).startswith("notifications/"):
            return None
        if mid is not None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown: {method}"}}
        return None


def make_client(fake):
    transport = SSETransport("http://fake.test/sse", connector=fake, timeout=10.0)
    return MCPClient(
        MCPServerConfig(name="sse", url="http://fake.test/sse", timeout=10.0),
        transport=transport)


# -- 单元测试 ----------------------------------------------------------------------------

def test_sse_handshake_and_rpc_over_fake_connector():
    async def go():
        fake = FakeSSEServer()
        client = make_client(fake)
        await client.start()
        assert client.server_info["name"] == "sse-stub"
        assert client.supports("tools")

        tools = await client.list_tools()
        assert tools[0]["name"] == "ping"

        result = await client.call_tool("ping", {})
        assert result["content"][0]["text"] == "pong"

        # endpoint 事件的相对路径按 urljoin 解析
        assert fake.posts[0][0] == "http://fake.test/messages?sid=42"
        # initialize / tools/list / tools/call 三个请求都到了（可能还有 initialized 通知）
        methods = [m for _, m in fake.posts]
        assert {"initialize", "tools/list", "tools/call"} <= {
            m.get("method") for m in methods}
        await client.close()
        assert not client.alive
    asyncio.run(go())


def test_sse_server_disconnect_fails_requests():
    async def go():
        fake = FakeSSEServer()
        client = make_client(fake)
        await client.start()
        fake.close_stream()            # 服务器关流
        await asyncio.sleep(0.5)       # 等 reader 线程观察到 EOF
        assert not client.alive
        with pytest.raises(MCPError, match="断开"):
            await client.list_tools()
        await client.close()
    asyncio.run(go())


def test_sse_jsonrpc_error_propagates():
    async def go():
        fake = FakeSSEServer()
        client = make_client(fake)
        await client.start()
        with pytest.raises(MCPError, match="unknown"):
            await client._request("no/such_method")
        await client.close()
    asyncio.run(go())


# -- 集成测试：真实本地 HTTP 服务器（沙箱禁 bind 时跳过）--------------------------------------

def _can_bind_localhost() -> bool:
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
        return True
    except OSError:
        return False


CAN_BIND = _can_bind_localhost()


@pytest.mark.skipif(not CAN_BIND, reason="沙箱禁止本地端口绑定")
def test_sse_real_http_server_integration():
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b"event: endpoint\ndata: /messages\n\n")
                self.wfile.flush()
                while True:
                    item = self.server.events.get(timeout=15)
                    if item is None:
                        break
                    self.wfile.write(b"event: message\ndata: " + item + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            msg = json.loads(self.rfile.read(length))
            resp = FakeSSEServer.handle(FakeSSEServer(), msg)  # 复用响应逻辑
            if resp is not None:
                self.server.events.put(json.dumps(resp).encode("utf-8"))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.events = queue.Queue()
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    async def go():
        client = MCPClient(MCPServerConfig(
            name="real-sse", url=f"http://127.0.0.1:{port}/sse", timeout=15.0))
        await client.start()
        assert client.server_info["name"] == "sse-stub"
        tools = await client.list_tools()
        assert tools[0]["name"] == "ping"
        result = await client.call_tool("ping", {})
        assert result["content"][0]["text"] == "pong"
        await client.close()

    try:
        asyncio.run(go())
    finally:
        server.events.put(None)
        server.shutdown()
        server.server_close()
