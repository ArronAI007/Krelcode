"""providers/openai 测试：SSE 流式、tool_call 增量聚合、wire 互转、错误路径。"""

import asyncio
import json

import pytest

from fakes import FakeTransport, sse
from krelcode.core.errors import ConfigError, LLMHTTPError
from krelcode.core.messages import INVALID_ARGUMENTS_KEY, Message, ToolCall
from krelcode.llm import collect
from krelcode.llm.base import FINISH_STOP, FINISH_TOOLS, StreamFinished, TextDelta
from krelcode.llm.providers.openai import OpenAIProvider, to_openai_messages

TOOLS = [{"name": "get_time", "description": "查时间", "parameters": {"type": "object"}}]


def make_provider(body: bytes, status: int = 200, **kw):
    transport = FakeTransport(body, status)
    provider = OpenAIProvider(model="m1", api_key="sk-test", transport=transport, **kw)
    return provider, transport


async def events_of(provider, messages, tools=None):
    return [e async for e in provider.stream(messages, tools)]


# -- 流式文本 ------------------------------------------------------------------

def test_text_streaming_deltas_and_finish():
    body = sse(
        {"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo "}}, {"delta": {"content": "世界"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
    )
    provider, _ = make_provider(body)
    events = asyncio.run(events_of(provider, [Message.user("hi")]))

    # 同一 chunk 里多个 choice 的 delta 也逐个产出
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo ", "世界"]
    finished = events[-1]
    assert isinstance(finished, StreamFinished)
    assert finished.message.content == "Hello 世界"
    assert finished.message.tool_calls is None
    assert finished.finish_reason == FINISH_STOP
    assert finished.usage == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}


# -- tool_call 增量聚合 ----------------------------------------------------------

def test_tool_call_arguments_assembled_across_chunks():
    body = sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "get_time", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"time'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'zone": "UTC"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("几点")], TOOLS))

    assert finished.finish_reason == FINISH_TOOLS
    assert finished.message.tool_calls == [
        ToolCall(id="c1", name="get_time", arguments={"timezone": "UTC"})
    ]


def test_multiple_parallel_tool_calls_by_index():
    body = sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "t1", "arguments": "{}"}},
            {"index": 1, "id": "b", "function": {"name": "t2", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("x")], TOOLS))
    assert [tc.name for tc in finished.message.tool_calls] == ["t1", "t2"]


def test_invalid_json_arguments_marked():
    body = sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "t", "arguments": "{oops"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("x")], TOOLS))
    tc = finished.message.tool_calls[0]
    assert tc.arguments == {INVALID_ARGUMENTS_KEY: "{oops"}
    assert tc.has_invalid_arguments


# -- 请求构造 / wire 转换 ---------------------------------------------------------

def test_request_shape_and_message_conversion():
    provider, transport = make_provider(sse({"choices": [{"delta": {"content": "ok"}}]}))
    history = [
        Message.system("SYS"),
        Message.user("hi"),
        Message.assistant(tool_calls=[ToolCall("c1", "get_time", {"tz": "UTC"})]),
        Message.tool_result("c1", "12:00"),
    ]
    asyncio.run(collect(provider, history, TOOLS))

    req = transport.last
    assert req.url == "https://api.openai.com/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"
    payload = req.json
    assert payload["model"] == "m1" and payload["stream"] is True
    assert payload["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_time", "arguments": '{"tz": "UTC"}'}}]},
        {"role": "tool", "content": "12:00", "tool_call_id": "c1"},
    ]
    assert payload["tools"] == [{
        "type": "function",
        "function": {"name": "get_time", "description": "查时间",
                     "parameters": {"type": "object"}},
    }]


def test_no_tools_field_when_empty():
    provider, transport = make_provider(sse({"choices": [{"delta": {"content": "x"}}]}))
    asyncio.run(collect(provider, [Message.user("q")], None))
    assert "tools" not in transport.last.json


def test_custom_base_url(monkeypatch):
    provider, transport = make_provider(sse({"choices": [{"delta": {"content": "x"}}]}),
                                        base_url="http://localhost:8000/v1/")
    asyncio.run(collect(provider, [Message.user("q")]))
    assert transport.last.url == "http://localhost:8000/v1/chat/completions"


# -- 错误路径 --------------------------------------------------------------------

def test_http_error_raises_with_body():
    provider, _ = make_provider(b'{"error": {"message": "rate limited"}}', status=429)
    with pytest.raises(LLMHTTPError) as ei:
        asyncio.run(collect(provider, [Message.user("x")]))
    assert ei.value.status == 429 and "rate limited" in str(ei.value)


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider(api_key=None, transport=FakeTransport(b""))
    provider.api_key = None
    with pytest.raises(ConfigError, match="API key"):
        asyncio.run(collect(provider, [Message.user("x")]))


def test_response_stream_closed_after_consumption():
    provider, transport = make_provider(sse({"choices": [{"delta": {"content": "x"}}]}))
    asyncio.run(collect(provider, [Message.user("q")]))
    assert transport.responses[0].closed
