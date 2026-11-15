"""Provider 工具调用增量协议：Started → ArgumentsDelta → Ready。"""

import asyncio

from fakes import FakeTransport, sse
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import (
    StreamFinished,
    ToolCallArgumentsDelta,
    ToolCallReady,
    ToolCallStartedDelta,
)
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.gemini import GeminiProvider
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.providers.openai import OpenAIProvider


TOOLS = [{
    "name": "read_file",
    "description": "read",
    "parameters": {"type": "object"},
}]


def events(provider):
    async def collect_events():
        return [
            event async for event in provider.stream(
                [Message.user("test")], TOOLS)
        ]

    return asyncio.run(collect_events())


def assert_tool_stream(stream, *, index=0, tool_call=None):
    started = [e for e in stream if isinstance(e, ToolCallStartedDelta)]
    deltas = [e for e in stream if isinstance(e, ToolCallArgumentsDelta)]
    ready = [e for e in stream if isinstance(e, ToolCallReady)]

    assert started == [ToolCallStartedDelta(index, tool_call.id, tool_call.name)]
    assert "".join(delta.partial_json for delta in deltas)
    assert ready == [ToolCallReady(index, tool_call)]
    assert stream.index(ready[0]) < stream.index(
        next(e for e in stream if isinstance(e, StreamFinished)))


def test_openai_emits_incremental_tool_events_and_ready_before_finish():
    body = sse(
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call-1",
            "function": {"name": "read_file", "arguments": '{"path":'},
        }]}}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "function": {"arguments": ' "README.md"}'},
        }]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    provider = OpenAIProvider(
        model="m", api_key="k", transport=FakeTransport(body))

    stream = events(provider)
    call = ToolCall("call-1", "read_file", {"path": "README.md"})
    assert_tool_stream(stream, tool_call=call)
    assert len([e for e in stream if isinstance(e, ToolCallArgumentsDelta)]) == 2


def test_anthropic_ready_on_content_block_stop():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu-1", "name": "read_file", "input": {},
        }},
        {"type": "content_block_delta", "index": 0, "delta": {
            "type": "input_json_delta", "partial_json": '{"path": "README.md"}',
        }},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    )
    provider = AnthropicProvider(
        model="m", api_key="k", transport=FakeTransport(body))

    stream = events(provider)
    call = ToolCall("toolu-1", "read_file", {"path": "README.md"})
    assert_tool_stream(stream, tool_call=call)


def test_anthropic_normalizes_wire_block_index_to_tool_ordinal():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 7, "content_block": {
            "type": "tool_use", "id": "toolu-7", "name": "read_file", "input": {},
        }},
        {"type": "content_block_delta", "index": 7, "delta": {
            "type": "input_json_delta", "partial_json": '{"path": "README.md"}',
        }},
        {"type": "content_block_stop", "index": 7},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    )
    provider = AnthropicProvider(
        model="m", api_key="k", transport=FakeTransport(body))

    stream = events(provider)
    call = ToolCall("toolu-7", "read_file", {"path": "README.md"})
    assert_tool_stream(stream, index=0, tool_call=call)


def test_gemini_complete_function_call_emits_ready_immediately():
    body = sse({"candidates": [{
        "content": {"parts": [{"functionCall": {
            "name": "read_file", "args": {"path": "README.md"},
        }}]},
        "finishReason": "STOP",
    }]})
    provider = GeminiProvider(
        model="m", api_key="k", transport=FakeTransport(body))

    stream = events(provider)
    call = ToolCall("gem-1", "read_file", {"path": "README.md"})
    assert_tool_stream(stream, tool_call=call)


def test_mock_emits_ready_for_scripted_tool_message():
    call = ToolCall("mock-1", "read_file", {"path": "README.md"})
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[call]),
    ])

    stream = events(provider)
    assert_tool_stream(stream, tool_call=call)
    assert stream[-1].finish_reason == "tool_calls"
