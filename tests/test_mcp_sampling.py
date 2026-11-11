"""Step 35 测试：MCP 服务器→客户端反向请求（sampling/roots/ping/未知方法）。"""

import asyncio
import sys
from pathlib import Path

import pytest

from krelcode.mcp.client import MCPClient, MCPServerConfig

STUB = Path(__file__).parent / "mcp_stub_server.py"


def make_client(**kw):
    cfg = MCPServerConfig(
        name="stub", command=sys.executable, args=[str(STUB)], timeout=15.0)
    return MCPClient(cfg, **kw)


async def call_text(client, tool):
    result = await client.call_tool(tool, {})
    return result["content"][0]["text"]


def test_sampling_roundtrip():
    """服务器请求 sampling → 客户端回调 sampler → 结果回到工具输出。"""
    async def go():
        seen = {}

        async def sampler(messages, params):
            seen["messages"] = messages
            seen["params"] = params
            return {"text": "SAMPLE-RESPONSE", "model": "fake-model"}

        client = make_client(sampler=sampler)
        await client.start()
        try:
            text = await call_text(client, "sample_me")
            assert text == "sampled=SAMPLE-RESPONSE|model=fake-model"
            # sampler 收到 MCP 原始请求内容
            assert seen["messages"][0]["content"]["text"] == "说 SAMPLE"
            assert seen["params"]["maxTokens"] == 100
        finally:
            await client.close()
    asyncio.run(go())


def test_sampling_without_sampler_rejected():
    async def go():
        client = make_client()          # 未配置 sampler
        await client.start()
        try:
            text = await call_text(client, "sample_me")
            assert text.startswith("error -32601")
            assert "sampling not supported" in text
        finally:
            await client.close()
    asyncio.run(go())


def test_roots_list_answers_workspace_roots():
    async def go():
        client = make_client(roots=[{"uri": "file:///ws", "name": "ws"}])
        await client.start()
        try:
            text = await call_text(client, "list_roots")
            assert "file:///ws" in text and '"roots"' in text
        finally:
            await client.close()
    asyncio.run(go())


def test_ping_answered():
    async def go():
        client = make_client()
        await client.start()
        try:
            assert await call_text(client, "probe_ping") == "{}"
        finally:
            await client.close()
    asyncio.run(go())


def test_unknown_server_method_rejected():
    async def go():
        client = make_client()
        await client.start()
        try:
            text = await call_text(client, "probe_unknown")
            assert text.startswith("error -32601")
            assert "custom/unsupported" in text
        finally:
            await client.close()
    asyncio.run(go())


def test_initialize_declares_client_capabilities():
    """有 sampler/roots 时，initialize 声明对应客户端能力。"""
    async def go():
        async def sampler(messages, params):
            return {"text": "", "model": ""}

        cfg = MCPServerConfig(name="stub", command=sys.executable,
                              args=[str(STUB)], timeout=15.0)
        client = MCPClient(cfg, sampler=sampler,
                           roots=[{"uri": "file:///x", "name": "x"}])
        # 拦截 initialize 请求内容：包一层 transport 记录
        sent = []
        inner = cfg.create_transport()
        original_send = inner.send

        async def spy_send(payload):
            sent.append(payload)
            return await original_send(payload)

        inner.send = spy_send
        client._transport = inner
        await client.start()
        try:
            init_payload = sent[0]
            caps = init_payload["params"]["capabilities"]
            assert "sampling" in caps and "roots" in caps
        finally:
            await client.close()
    asyncio.run(go())


# -- app 侧 sampler/roots 构造 -----------------------------------------------------------

def test_build_mcp_sampler_uses_shared_llm():
    from krelcode.app import build_mcp_sampler
    from krelcode.llm.providers.mock import MockProvider

    provider = MockProvider(scripted=["采样结果"])
    sampler = build_mcp_sampler(provider)
    out = asyncio.run(sampler(
        [{"role": "user", "content": {"type": "text", "text": "hi"}}],
        {"systemPrompt": "你是助手"},
    ))
    assert out == {"text": "采样结果", "model": "mock-model"}
    # systemPrompt 进 system 消息、user 文本进 user 消息
    sent = provider.calls[0]
    assert sent[0].role == "system" and sent[0].content == "你是助手"
    assert sent[1].role == "user" and sent[1].content == "hi"


def test_build_mcp_sampler_without_system_prompt():
    from krelcode.app import build_mcp_sampler
    from krelcode.llm.providers.mock import MockProvider

    provider = MockProvider(scripted=["ok"])
    sampler = build_mcp_sampler(provider)
    asyncio.run(sampler([{"role": "user", "content": {"type": "text", "text": "q"}}], {}))
    assert provider.calls[0][0].role == "user"


def test_build_mcp_roots(tmp_path):
    from krelcode.app import build_mcp_roots
    roots = build_mcp_roots(tmp_path)
    assert roots == [{"uri": tmp_path.as_uri(), "name": tmp_path.name}]
