"""MCP elicitation：capability、协议应答、安全 Schema 与前台桥接。"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from pathlib import Path

from krelcode.core.events import UserInputRequested, UserInputResolved
from krelcode.core.redact import SecretRedactor
from krelcode.mcp.client import MCPClient, MCPServerConfig, PROTOCOL_VERSION
from krelcode.mcp.elicitation import (
    ElicitationRequest,
    ElicitationResult,
    ElicitationValidationError,
    parse_elicitation_request,
)
from krelcode.session.elicitation_bridge import MCPElicitationBridge
from krelcode.session.interactions import UserInputBroker

STUB = Path(__file__).parent / "mcp_stub_server.py"


def make_client(*, elicitor=None, env=None) -> MCPClient:
    config = MCPServerConfig(
        name="stub",
        command=sys.executable,
        args=[str(STUB)],
        env=dict(env or {}),
        timeout=15.0,
    )
    return MCPClient(config, elicitor=elicitor, log=lambda _message: None)


async def call_text(client: MCPClient, kind: str = "simple") -> str:
    result = await client.call_tool("elicit", {"kind": kind})
    return result["content"][0]["text"]


async def wait_pending(broker: UserInputBroker):
    request = await asyncio.wait_for(broker.next_request(), timeout=1.0)
    assert request is not None
    return request


def test_elicitation_capability_and_accept_roundtrip():
    async def go():
        seen: list[ElicitationRequest] = []

        async def elicitor(request):
            seen.append(request)
            return ElicitationResult(
                action="accept", content={"username": "octocat"})

        client = make_client(elicitor=elicitor)
        await client.start()
        try:
            assert client.server_protocol_version == PROTOCOL_VERSION
            assert "elicitation" in client.client_capabilities
            text = await call_text(client)
            assert json.loads(text) == {
                "action": "accept", "content": {"username": "octocat"}}
            assert seen[0].fields[0].name == "username"
            assert seen[0].fields[0].required is True
        finally:
            await client.close()

    asyncio.run(go())


def test_elicitation_decline_and_cancel_actions():
    async def run_action(action):
        async def elicitor(_request):
            return {"action": action}

        client = make_client(elicitor=elicitor)
        await client.start()
        try:
            text = await call_text(client)
            return json.loads(text)
        finally:
            await client.close()

    assert asyncio.run(run_action("decline")) == {"action": "decline"}
    assert asyncio.run(run_action("cancel")) == {"action": "cancel"}


def test_headless_elicitation_is_not_advertised_and_declines():
    async def go():
        client = make_client()
        await client.start()
        try:
            assert "elicitation" not in client.client_capabilities
            assert json.loads(await call_text(client)) == {"action": "decline"}
        finally:
            await client.close()

    asyncio.run(go())


def test_sensitive_and_nested_schemas_fail_closed_before_frontend():
    async def go():
        called = 0

        async def elicitor(_request):
            nonlocal called
            called += 1
            return {"action": "accept", "content": {}}

        client = make_client(elicitor=elicitor)
        await client.start()
        try:
            sensitive = await call_text(client, "sensitive")
            nested = await call_text(client, "nested")
            assert sensitive.startswith("error -32602")
            assert "敏感凭据" in sensitive
            assert nested.startswith("error -32602")
            assert "primitive type" in nested
            assert called == 0
        finally:
            await client.close()

    asyncio.run(go())


def test_elicitation_requires_negotiated_2025_06_18():
    async def go():
        async def elicitor(_request):
            return {"action": "decline"}

        client = make_client(
            elicitor=elicitor,
            env={"KRELCODE_STUB_PROTOCOL_VERSION": "2025-03-26"},
        )
        await client.start()
        try:
            text = await call_text(client)
            assert text.startswith("error -32601")
            assert "2025-06-18" in text
        finally:
            await client.close()

    asyncio.run(go())


def test_invalid_frontend_result_is_not_forwarded():
    async def go():
        async def elicitor(_request):
            return {
                "action": "accept",
                "content": {"username": 7},
            }

        client = make_client(elicitor=elicitor)
        await client.start()
        try:
            assert json.loads(await call_text(client)) == {"action": "cancel"}
        finally:
            await client.close()

    asyncio.run(go())


def test_bridge_keeps_prompt_open_on_invalid_json_and_redacts_message():
    async def go():
        broker = UserInputBroker()
        events = []

        async def emit(event):
            events.append(event)

        redactor = SecretRedactor(["super-secret-value"])
        bridge = MCPElicitationBridge(broker, emit, redactor=redactor)
        request = parse_elicitation_request({
            "message": "请提供 token super-secret-value 对应的用户名",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "title": "Username"},
                },
                "required": ["username"],
            },
        }, server="demo")
        task = asyncio.create_task(bridge(request))
        await wait_pending(broker)

        assert isinstance(events[0], UserInputRequested)
        assert "super-secret-value" not in events[0].prompt
        assert "***" in events[0].prompt

        invalid = broker.resolve("not-json")
        assert invalid.accepted is False
        assert "JSON object" in invalid.reason
        assert broker.pending is not None

        valid = broker.resolve('{"username":"alice"}')
        assert valid.accepted is True
        result = await task
        assert result == ElicitationResult(
            action="accept", content={"username": "alice"})
        assert isinstance(events[-1], UserInputResolved)
        assert events[-1].answered is True
        broker.close()

    asyncio.run(go())


def test_bridge_cancel_and_empty_schema_accept():
    async def go():
        broker = UserInputBroker()
        events = []

        async def emit(event):
            events.append(event)

        bridge = MCPElicitationBridge(broker, emit)
        request = parse_elicitation_request({
            "message": "是否继续？",
            "requestedSchema": {"type": "object", "properties": {}},
        }, server="demo")
        task = asyncio.create_task(bridge(request))
        await wait_pending(broker)
        assert events[0].options == ("accept", "decline", "cancel")
        broker.resolve("cancel")
        assert await task == ElicitationResult(action="cancel")
        assert events[-1].answered is False
        broker.close()

        broker2 = UserInputBroker()

        async def emit2(_event):
            return None

        bridge2 = MCPElicitationBridge(broker2, emit2)
        task2 = asyncio.create_task(bridge2(request))
        await wait_pending(broker2)
        broker2.resolve("accept")
        assert await task2 == ElicitationResult(action="accept", content={})
        broker2.close()

    asyncio.run(go())


def test_request_schema_limits():
    with pytest.raises(ElicitationValidationError):
        parse_elicitation_request({
            "message": "x",
            "requestedSchema": {
                "type": "object",
                "properties": {"password": {"type": "string"}},
            },
        }, server="demo")

    with pytest.raises(ElicitationValidationError):
        parse_elicitation_request({
            "message": "x",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "default": "attacker-chosen"},
                },
            },
        }, server="demo")


def test_elicitation_rate_limit_declines_excess_requests():
    async def go():
        called = 0

        async def elicitor(_request):
            nonlocal called
            called += 1
            return {"action": "decline"}

        client = make_client(elicitor=elicitor)
        await client.start()
        try:
            for _ in range(11):
                assert json.loads(await call_text(client)) == {
                    "action": "decline"}
            assert called == 10
        finally:
            await client.close()

    asyncio.run(go())
