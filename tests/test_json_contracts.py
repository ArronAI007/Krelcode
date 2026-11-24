from __future__ import annotations

import asyncio

import pytest

from krelcode.contracts.json import (
    JsonValueError,
    ensure_json_object,
    ensure_json_value,
)
from krelcode.contracts.tool import ToolResult
from krelcode.mcp.client import MCPClient, MCPError, MCPServerConfig
from krelcode.mcp.streamable import StreamableHTTPTransport


def test_json_contract_normalizes_sequences_and_nested_objects():
    value = ensure_json_object({
        "name": "example",
        "items": (1, True, {"score": 1.5}),
    })

    assert value == {
        "name": "example",
        "items": [1, True, {"score": 1.5}],
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_json_contract_rejects_non_json_scalars(value):
    with pytest.raises(JsonValueError):
        ensure_json_value(value, path="$.value")


def test_json_contract_rejects_non_string_object_keys():
    with pytest.raises(JsonValueError, match="对象键必须是字符串"):
        ensure_json_object({1: "value"})  # type: ignore[dict-item]


def test_json_contract_rejects_binary_values():
    with pytest.raises(JsonValueError, match="不可序列化类型"):
        ensure_json_value(b"secret", path="$.payload")


def test_tool_result_validates_its_execution_data_at_the_boundary():
    with pytest.raises(JsonValueError, match="不可序列化类型"):
        ToolResult(content="bad", data={"callback": lambda: None})

    result = ToolResult(content="ok", data={"items": (1, 2)})
    assert result.data == {"items": [1, 2]}


def test_mcp_paginated_list_rejects_non_object_items():
    class InvalidClient(MCPClient):
        async def _request(self, method, params=None):
            return {"tools": [1], "nextCursor": ""}

    client = InvalidClient(MCPServerConfig(
        name="invalid", url="https://example.test/mcp"))
    with pytest.raises(MCPError, match="tools\\[0\\]"):
        asyncio.run(client.list_tools())


def test_streamable_batch_rejects_non_object_items():
    transport = StreamableHTTPTransport("https://example.test/mcp")
    with pytest.raises(MCPError, match="batch\\[1\\]"):
        transport._dispatch_incoming([
            {"jsonrpc": "2.0", "method": "notifications/message"},
            1,
        ])
