"""MCP resources/prompts 能力测试：能力门控发现 + 四类能力工具 + stub 集成。"""

import asyncio
import sys
from pathlib import Path

import pytest

from krelcode.mcp.client import MCPClient, MCPServerConfig
from krelcode.mcp.supervisor import MCPSupervisor
from krelcode.mcp.tools import discover_tools
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry

STUB = Path(__file__).parent / "mcp_stub_server.py"
CTX = ToolContext(workspace_root=Path("."))


def run(coro):
    return asyncio.run(coro)


async def stub_client():
    client = MCPClient(MCPServerConfig(
        name="stub", command=sys.executable, args=[str(STUB)], timeout=15.0))
    await client.start()
    return client


# -- 能力门控 ------------------------------------------------------------------------

class FakeClient:
    """discover_tools 的门控单测替身。"""
    def __init__(self, caps):
        self.name = "fake"
        self.capabilities = caps
        self.server_info = {}
    def supports(self, cap):
        return cap in self.capabilities
    async def list_tools(self):
        return [{"name": "t", "inputSchema": {"type": "object"}}]


def test_discover_tools_gated_by_capabilities():
    async def go():
        only_tools = await discover_tools(FakeClient({"tools": {}}))
        assert [t.name for t in only_tools] == ["fake__t"]

        none = await discover_tools(FakeClient({}) )
        assert none == []
    run(go())


def test_discover_all_capabilities():
    async def go():
        tools = await discover_tools(FakeClient({"tools": {}, "resources": {}, "prompts": {}}))
        return sorted(t.name for t in tools)
    names = run(go())
    assert names == ["fake__get_prompt", "fake__list_prompts",
                     "fake__list_resources", "fake__read_resource", "fake__t"]


# -- stub 集成：supervisor 注册全部 7 个工具 ----------------------------------------------

def test_supervisor_registers_capability_tools():
    async def go():
        registry = ToolRegistry()
        sup = MCPSupervisor(registry, log=lambda _: None)
        await sup.sync({"stub": {"command": sys.executable, "args": [str(STUB)]}})
        try:
            names = registry.names()
            assert names == [
                "stub__add", "stub__boom", "stub__echo", "stub__get_prompt",
                "stub__list_prompts", "stub__list_resources", "stub__read_resource",
            ]
        finally:
            await sup.close_all()
    run(go())


def test_resource_tools_end_to_end():
    async def go():
        client = await stub_client()
        try:
            tools = {t.name: t for t in await discover_tools(client)}

            listed = await tools["stub__list_resources"].execute({}, CTX)
            assert not listed.is_error
            assert "krel://greeting" in listed.content and "问候语" in listed.content
            assert listed.data["count"] == 2

            read = await tools["stub__read_resource"].execute({"uri": "krel://greeting"}, CTX)
            assert read.content == "你好，MCP 资源！"

            missing = await tools["stub__read_resource"].execute({"uri": "krel://nope"}, CTX)
            assert missing.is_error and "resource not found" in missing.content

            bad_arg = await tools["stub__read_resource"].execute({}, CTX)
            assert bad_arg.is_error and "uri" in bad_arg.content
        finally:
            await client.close()
    run(go())


def test_prompt_tools_end_to_end():
    async def go():
        client = await stub_client()
        try:
            tools = {t.name: t for t in await discover_tools(client)}

            listed = await tools["stub__list_prompts"].execute({}, CTX)
            assert "review(path)" in listed.content and "代码评审提示" in listed.content

            got = await tools["stub__get_prompt"].execute(
                {"name": "review", "arguments": {"path": "a.py"}}, CTX)
            assert not got.is_error
            assert "[user] 请评审文件 a.py" in got.content

            unknown = await tools["stub__get_prompt"].execute({"name": "nope"}, CTX)
            assert unknown.is_error and "unknown prompt" in unknown.content

            bad = await tools["stub__get_prompt"].execute({}, CTX)
            assert bad.is_error and "name" in bad.content
        finally:
            await client.close()
    run(go())
