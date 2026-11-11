"""MCP 客户端测试：用 stub 服务器跑真实子进程（stdio JSON-RPC），全离线。

覆盖：握手、工具发现、远程调用、isError、JSON-RPC 错误、服务器崩溃、
MCPTool 适配（命名/schema/Tool 端口兼容）、与 AgentLoop 集成。
config 驱动/热重载见 test_mcp_supervisor.py；resources/prompts 见
test_mcp_capabilities.py；SSE 见 test_mcp_sse.py。
"""

import asyncio
import sys
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.mcp.client import MCPClient, MCPError, MCPServerConfig
from krelcode.mcp.tools import MCPTool, sanitize_tool_name, _render_content
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry

STUB = Path(__file__).parent / "mcp_stub_server.py"


def make_config(**kw):
    defaults = dict(name="stub", command=sys.executable, args=[str(STUB)], timeout=15.0)
    defaults.update(kw)
    return MCPServerConfig(**defaults)


async def with_client(coro_fn, **cfg_kw):
    client = MCPClient(make_config(**cfg_kw))
    await client.start()
    try:
        return await coro_fn(client)
    finally:
        await client.close()


def run(coro_fn, **cfg_kw):
    return asyncio.run(with_client(coro_fn, **cfg_kw))


# -- 握手与发现 ----------------------------------------------------------------------

def test_initialize_handshake():
    async def go(client):
        assert client.server_info.get("name") == "stub-server"
    run(go)


def test_list_tools():
    async def go(client):
        tools = await client.list_tools()
        return [t["name"] for t in tools]
    assert run(go) == ["echo", "add", "boom",
                       "sample_me", "list_roots", "probe_ping", "probe_unknown"]


def test_call_tool_echo_and_add():
    async def go(client):
        r1 = await client.call_tool("echo", {"text": "你好 MCP"})
        r2 = await client.call_tool("add", {"a": 2, "b": 3})
        return r1, r2
    r1, r2 = run(go)
    assert r1["content"][0]["text"] == "你好 MCP"
    assert r2["content"][0]["text"] == "5"


def test_call_tool_is_error():
    async def go(client):
        return await client.call_tool("boom", {})
    result = run(go)
    assert result.get("isError") is True


def test_jsonrpc_error_raises_mcp_error():
    async def go(client):
        with pytest.raises(MCPError, match="unknown tool"):
            await client.call_tool("no_such_tool", {})
    run(go)


def test_server_crash_fails_requests():
    async def go():
        client = MCPClient(make_config())
        await client.start()
        client._transport._proc.kill()    # 模拟服务器崩溃
        await asyncio.sleep(0.3)          # 等 reader 观察到 EOF
        with pytest.raises(MCPError):
            await client.call_tool("echo", {"text": "x"})
        await client.close()
    asyncio.run(go())


def test_bad_command_raises():
    async def go():
        client = MCPClient(make_config(command="/nonexistent/binary"))
        with pytest.raises(MCPError, match="无法启动"):
            await client.start()
        await client.close()
    asyncio.run(go())


# -- MCPTool 适配器 ---------------------------------------------------------------------

def test_sanitize_tool_name():
    assert sanitize_tool_name("my-server", "do.thing") == "my-server__do_thing"
    assert sanitize_tool_name("s", "ok-name_1") == "s__ok-name_1"


def test_render_content_blocks():
    assert _render_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert "image 内容块" in _render_content([{"type": "image", "data": "..."}])
    assert _render_content(None) == ""


def test_mcp_tool_adapter_maps_spec_and_executes():
    async def go(client):
        specs = await client.list_tools()
        tool = MCPTool(client, specs[0])
        assert tool.name == "stub__echo"
        assert tool.description == "原样返回 text"
        assert tool.parameters == specs[0]["inputSchema"]
        result = await tool.execute({"text": "适配成功"}, ToolContext(workspace_root=Path(".")))
        assert not result.is_error and result.content == "适配成功"
        # isError 透传
        boom = MCPTool(client, specs[2])
        r2 = await boom.execute({}, ToolContext(workspace_root=Path(".")))
        assert r2.is_error and "boom" in r2.content
    run(go)


# -- 与 AgentLoop 集成 ---------------------------------------------------------------------

def test_mcp_tool_in_agent_loop(tmp_path):
    async def go(client):
        specs = await client.list_tools()
        add_tool = MCPTool(client, specs[1])
        provider = MockProvider(scripted=[
            Message.assistant(tool_calls=[ToolCall("m1", add_tool.name, {"a": 20, "b": 22})]),
            "远程 MCP 工具算出 20+22=42。",
        ])
        loop = AgentLoop(
            llm=provider,
            registry=ToolRegistry([add_tool]),
            tool_context=ToolContext(workspace_root=tmp_path),
            policy=AllowAllPolicy(),
            system_prompt="SYS",
        )
        reply = await loop.run("用 MCP 算 20+22")
        assert reply == "远程 MCP 工具算出 20+22=42。"
        assert loop.history[3].content == "42"          # 远程结果回填
        # 循环发给 provider 的是统一 ToolSpec（远程 inputSchema 直通）
        assert provider.tools_seen[0][0]["name"] == "stub__add"

    run(go)
