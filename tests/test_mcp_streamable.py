"""StreamableHTTPTransport（MCP 2025-03-26）测试。

单元：FakeStreamableServer 实现 HTTPConnector 端口（内存应答，零 socket）；
集成：真实 http.server（沙箱禁 bind 时自动 skip）。
"""

import asyncio
import json
import socket
import threading
from typing import Optional

import pytest

from krelcode.mcp.client import MCPClient, MCPServerConfig
from krelcode.mcp.streamable import StreamableHTTPTransport
from krelcode.mcp.transport import MCPError
from krelcode.mcp.supervisor import config_from_spec
from krelcode.mcp.sse_transport import SSETransport
from krelcode.mcp.stdio_transport import StdioTransport


# -- Fake 服务器（HTTPConnector 端口）--------------------------------------------------------

class FakeHTTPResponse:
    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self):
        return self._body

    def iter_lines(self):
        return iter(self._body.splitlines(keepends=True))

    def close(self):
        pass


class FakeStreamableServer:
    TOOLS = [{"name": "ping", "description": "p",
              "inputSchema": {"type": "object", "properties": {}}}]

    def __init__(self):
        self.posts = []            # [(url, headers, payload)]
        self.gets = []             # [(url, headers)]
        self.session_id = "sess-abc"
        self.expired = False
        self.inject_server_request = False

    # HTTPConnector 端口
    def request(self, method, url, headers, body, timeout):
        if method == "GET":
            self.gets.append((url, dict(headers)))
            return FakeHTTPResponse(405, {}, b"")  # 不支持监听流
        payload = json.loads(body)
        self.posts.append((url, dict(headers), payload))
        if self.expired:
            return FakeHTTPResponse(404, {}, b'{"error":"session gone"}')
        # 客户端对服务器请求的自动应答（带 id + error，无 method）
        if "method" not in payload:
            return FakeHTTPResponse(202, {}, b"")
        m = payload.get("method")
        mid = payload.get("id")
        if m == "initialize":
            return FakeHTTPResponse(200, {
                "content-type": "application/json",
                "mcp-session-id": self.session_id,
            }, json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stream-stub", "version": "1.0"}}}).encode())
        if mid is None:  # 通知
            return FakeHTTPResponse(202, {}, b"")
        if m == "tools/list":
            return self._sse([{"jsonrpc": "2.0", "id": mid,
                               "result": {"tools": self.TOOLS}}])
        if m == "tools/call":
            events = []
            if self.inject_server_request:
                events.append({"jsonrpc": "2.0", "id": 99,
                               "method": "sampling/createMessage", "params": {}})
            events.append({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "pong"}]}})
            return self._sse(events)
        return self._sse([{"jsonrpc": "2.0", "id": mid,
                           "error": {"code": -32601, "message": f"unknown {m}"}}])

    def close_stream(self):
        pass

    @staticmethod
    def _sse(msgs):
        body = b""
        for m in msgs:
            body += b"event: message\ndata: " + json.dumps(m).encode() + b"\n\n"
        return FakeHTTPResponse(200, {"content-type": "text/event-stream"}, body)


def make_client(fake) -> MCPClient:
    transport = StreamableHTTPTransport("http://fake.test/mcp", connector=fake, timeout=10.0)
    return MCPClient(
        MCPServerConfig(name="stream", url="http://fake.test/mcp", timeout=10.0),
        transport=transport)


# -- 单元测试 ----------------------------------------------------------------------------------

def test_initialize_json_mode_and_session_capture():
    async def go():
        fake = FakeStreamableServer()
        client = make_client(fake)
        await client.start()
        assert client.server_info["name"] == "stream-stub"

        # 首个 POST：Accept 双类型，尚无会话头
        url0, headers0, payload0 = fake.posts[0]
        assert headers0["Accept"] == "application/json, text/event-stream"
        assert "Mcp-Session-Id" not in headers0
        assert payload0["params"]["protocolVersion"] == "2025-03-26"
        # 会话 id 被捕获，后续请求携带
        assert client._transport.session_id == "sess-abc"

        await client.list_tools()
        list_post = [p for p in fake.posts if p[2].get("method") == "tools/list"][0]
        assert list_post[1]["Mcp-Session-Id"] == "sess-abc"
        await client.close()
    asyncio.run(go())


def test_tools_list_via_sse_response_mode():
    async def go():
        fake = FakeStreamableServer()
        client = make_client(fake)
        await client.start()
        tools = await client.list_tools()      # 该请求走 SSE 响应模式
        assert tools[0]["name"] == "ping"
        result = await client.call_tool("ping", {})
        assert result["content"][0]["text"] == "pong"
        await client.close()
    asyncio.run(go())


def test_notification_returns_202_and_is_recorded():
    async def go():
        fake = FakeStreamableServer()
        client = make_client(fake)
        await client.start()
        await asyncio.sleep(0.2)  # notifications/initialized 经 create_task 异步发出
        notifications = [p for p in fake.posts
                         if p[2].get("method") == "notifications/initialized"]
        assert len(notifications) == 1
        assert "id" not in notifications[0][2]
        await client.close()
    asyncio.run(go())


def test_get_listener_attempted_and_405_tolerated():
    async def go():
        fake = FakeStreamableServer()
        client = make_client(fake)
        await client.start()
        await asyncio.sleep(0.3)               # 监听流线程惰性启动
        assert len(fake.gets) >= 1             # GET 尝试过
        assert fake.gets[0][1]["Accept"] == "text/event-stream"
        tools = await client.list_tools()      # 405 不影响主通道
        assert tools[0]["name"] == "ping"
        await client.close()
    asyncio.run(go())


def test_server_request_in_stream_gets_auto_error_response():
    async def go():
        fake = FakeStreamableServer()
        fake.inject_server_request = True
        client = make_client(fake)
        await client.start()
        result = await client.call_tool("ping", {})
        assert result["content"][0]["text"] == "pong"   # 正常响应不受干扰
        await asyncio.sleep(0.1)
        auto = [p for p in fake.posts
                if p[2].get("id") == 99 and "error" in p[2]]
        assert len(auto) == 1
        assert auto[0][2]["error"]["code"] == -32601
        await client.close()
    asyncio.run(go())


def test_session_expiry_disconnects_fail_closed():
    async def go():
        fake = FakeStreamableServer()
        client = make_client(fake)
        await client.start()
        fake.expired = True                    # 服务器判定会话失效
        with pytest.raises(MCPError, match="过期"):
            await client.list_tools()
        await asyncio.sleep(0.1)               # 等 disconnect 回调落地
        assert not client.alive
        with pytest.raises(MCPError):          # 后续请求快速失败，不再打网络
            await client.list_tools()
        assert len([p for p in fake.posts if p[2].get("method") == "tools/list"]) == 1
        await client.close()
    asyncio.run(go())


def test_config_transport_selection():
    assert isinstance(MCPServerConfig(name="a", url="http://x/mcp").create_transport(),
                      StreamableHTTPTransport)          # url 默认 streamable
    assert isinstance(MCPServerConfig(name="b", url="http://x", transport="sse").create_transport(),
                      SSETransport)                     # 显式回退旧规范
    assert isinstance(MCPServerConfig(name="c", command="x").create_transport(),
                      StdioTransport)
    with pytest.raises(MCPError, match="未知 transport"):
        MCPServerConfig(name="d", url="http://x", transport="bogus").create_transport()
    # supervisor 的 TOML 解析透传 transport 字段
    cfg = config_from_spec("e", {"url": "http://x", "transport": "sse"})
    assert cfg.transport == "sse"


# -- 集成测试：真实 http.server（沙箱禁 bind 时跳过）------------------------------------------------

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
def test_streamable_real_http_server_integration():
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            m, mid = payload.get("method"), payload.get("id")
            if m == "initialize":
                body = json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "real-stream", "version": "1.0"}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "srv-1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if mid is None or "method" not in payload:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if m == "tools/list":
                msg = {"jsonrpc": "2.0", "id": mid,
                       "result": {"tools": [{"name": "ping", "description": "p",
                                             "inputSchema": {"type": "object", "properties": {}}}]}}
            elif m == "tools/call":
                msg = {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "pong"}]}}
            else:
                msg = {"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601, "message": "unknown"}}
            chunk = b"event: message\ndata: " + json.dumps(msg).encode() + b"\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    async def go():
        client = MCPClient(MCPServerConfig(
            name="real", url=f"http://127.0.0.1:{port}/mcp", timeout=15.0))
        await client.start()
        assert client.server_info["name"] == "real-stream"
        assert client._transport.session_id == "srv-1"
        tools = await client.list_tools()
        assert tools[0]["name"] == "ping"
        result = await client.call_tool("ping", {})
        assert result["content"][0]["text"] == "pong"
        await client.close()

    try:
        asyncio.run(go())
    finally:
        server.shutdown()
        server.server_close()
