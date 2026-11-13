"""Step 52 测试：MCP Streamable HTTP 进阶（可恢复性/会话终止/版本头/batch）。

全部走 HTTPConnector 端口的内存 Fake，零 socket；与真实 http.server 集成
由既有 test_mcp_streamable.py 覆盖。
"""

import asyncio
import json

from krelcode.mcp.client import MCPClient, MCPServerConfig
from krelcode.mcp.sseparse import SSEEventParser
from krelcode.mcp.streamable import StreamableHTTPTransport
from krelcode.mcp.supervisor import config_from_spec


class FakeResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    def read(self):
        return self._body

    def iter_lines(self):
        return iter(self._body.splitlines(keepends=True))

    def close(self):
        pass


class FakeServer:
    """最小 Streamable HTTP 服务：POST JSON/SSE + 可编排 GET 监听流。"""

    def __init__(self, *, get_script=None, init_version="2025-06-18", batch=False):
        self.posts = []       # [(headers, payload)]
        self.gets = []        # [headers]
        self.deletes = []     # [headers]
        self.session_id = "sess-42"
        self.get_script = list(get_script or [])
        self.init_version = init_version
        self.batch = batch

    def request(self, method, url, headers, body, timeout):
        if method == "GET":
            self.gets.append(dict(headers))
            if self.get_script:
                return self.get_script.pop(0)()
            return FakeResponse(405)
        if method == "DELETE":
            self.deletes.append(dict(headers))
            return FakeResponse(200)
        payload = json.loads(body)
        self.posts.append((dict(headers), payload))
        mid = payload.get("id")
        name = payload.get("method")
        if name == "initialize":
            body = json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": self.init_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "adv-stub", "version": "1.0"}}}).encode()
            return FakeResponse(200, {
                "content-type": "application/json",
                "mcp-session-id": self.session_id,
            }, body)
        if mid is None:
            return FakeResponse(202)
        result = {"tools": [{"name": "ping", "description": "p",
                             "inputSchema": {"type": "object", "properties": {}}}]}
        resp = {"jsonrpc": "2.0", "id": mid, "result": result}
        if self.batch:
            notif = {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}
            return FakeResponse(200, {"content-type": "application/json"},
                                json.dumps([notif, resp]).encode())
        return FakeResponse(200, {"content-type": "application/json"},
                            json.dumps(resp).encode())

    def close_stream(self):
        pass


def make_client(fake, **kwargs) -> MCPClient:
    transport = StreamableHTTPTransport(
        "http://fake.test/mcp", connector=fake, timeout=2.0, **kwargs)
    return MCPClient(
        MCPServerConfig(name="adv", url="http://fake.test/mcp", timeout=2.0),
        transport=transport)


# -- 1. SSE id:/retry: 解析 -----------------------------------------------------------------

def test_sse_parser_tracks_id_and_retry():
    p = SSEEventParser()
    assert p.feed("id: evt-1") is None
    assert p.last_event_id == "evt-1"
    assert p.feed("retry: 2500") is None
    assert p.retry_ms == 2500
    assert p.feed("data: hello") is None
    assert p.feed("") == ("message", "hello")
    assert p.last_event_id == "evt-1"          # 跨事件保留
    p.feed("id: ")
    assert p.last_event_id == ""               # 空 id 重置
    p.feed("retry: not-a-number")
    assert p.retry_ms == 2500                  # 非法值忽略


# -- 2. batch 响应 ---------------------------------------------------------------------------

def test_batch_response_dispatched():
    async def go():
        fake = FakeServer(batch=True)
        client = make_client(fake)
        await client.start()
        tools = await client.list_tools()
        assert tools[0]["name"] == "ping"
        await client.close()
    asyncio.run(go())


# -- 3. 协议版本头 ---------------------------------------------------------------------------

def test_protocol_version_header_negotiated():
    async def go():
        fake = FakeServer(init_version="2025-06-18")
        client = make_client(fake)
        await client.start()
        # initialize 前用客户端默认版本
        assert fake.posts[0][0]["MCP-Protocol-Version"] == "2025-03-26"
        assert client.server_protocol_version == "2025-06-18"
        # 协商后后续请求使用服务器返回版本
        await client.list_tools()
        assert fake.posts[-1][0]["MCP-Protocol-Version"] == "2025-06-18"
        await client.close()
    asyncio.run(go())


# -- 4. Last-Event-ID 断线恢复 ----------------------------------------------------------------

def _sse_with_id(body: dict, event_id: str) -> FakeResponse:
    chunk = (f"id: {event_id}\nevent: message\ndata: "
             + json.dumps(body) + "\n\n").encode()
    return FakeResponse(200, {"content-type": "text/event-stream"}, chunk)


def test_last_event_id_sent_on_listener_reconnect():
    async def go():
        server_notif = {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}
        fake = FakeServer(get_script=[lambda: _sse_with_id(server_notif, "evt-7")])
        client = make_client(fake, listener_retries=2, listener_retry_delay=0.01)
        await client.start()
        await asyncio.sleep(0.3)               # 首个 GET 读到事件后 EOF，触发重连
        assert len(fake.gets) >= 2
        assert fake.gets[1].get("Last-Event-ID") == "evt-7"
        assert client._transport.last_event_id == "evt-7"
        await client.close()
    asyncio.run(go())


def test_listener_405_tolerated_without_retry():
    async def go():
        fake = FakeServer()                    # GET 恒 405
        client = make_client(fake, listener_retries=3, listener_retry_delay=0.01)
        await client.start()
        await asyncio.sleep(0.2)
        assert len(fake.gets) == 1             # 不支持监听流：只试一次
        await client.close()
    asyncio.run(go())


# -- 5. DELETE 会话终止 ----------------------------------------------------------------------

def test_delete_session_on_close():
    async def go():
        fake = FakeServer(init_version="2025-06-18")
        client = make_client(fake)
        await client.start()
        await client.close()
        assert len(fake.deletes) == 1
        h = fake.deletes[0]
        assert h["Mcp-Session-Id"] == "sess-42"
        assert h["MCP-Protocol-Version"] == "2025-06-18"
        assert "Content-Type" not in h          # DELETE 无 body
    asyncio.run(go())


def test_delete_failure_is_tolerated():
    class BoomDelete(FakeServer):
        def request(self, method, url, headers, body, timeout):
            if method == "DELETE":
                raise RuntimeError("network down")
            return super().request(method, url, headers, body, timeout)

    async def go():
        fake = BoomDelete()
        client = make_client(fake)
        await client.start()
        await client.close()                   # 不得抛错
        assert client._transport.alive is False
    asyncio.run(go())


# -- 6. TOML 配置入口 ------------------------------------------------------------------------

def test_listener_retry_config_from_toml():
    cfg = config_from_spec("srv", {
        "url": "http://x/mcp",
        "listener_retries": 5,
        "listener_retry_delay": 0.25,
    })
    assert cfg.listener_retries == 5
    assert cfg.listener_retry_delay == 0.25
    transport = cfg.create_transport()
    assert isinstance(transport, StreamableHTTPTransport)
    assert transport.listener_retries == 5
    assert transport.listener_retry_delay == 0.25
