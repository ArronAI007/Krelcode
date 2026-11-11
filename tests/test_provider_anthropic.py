"""providers/anthropic 测试：异构 wire format 的翻译正确性（统一抽象的证明）。"""

import asyncio
import json

import pytest

from fakes import FakeTransport, sse
from krelcode.core.errors import ConfigError, LLMHTTPError
from krelcode.core.messages import Message, ToolCall
from krelcode.llm import collect
from krelcode.llm.base import FINISH_STOP, FINISH_TOOLS, StreamFinished, TextDelta
from krelcode.llm.providers.anthropic import (
    ANTHROPIC_VERSION, AnthropicProvider, to_anthropic_request_messages,
)

TOOLS = [{"name": "get_time", "description": "查时间", "parameters": {"type": "object"}}]


def make_provider(body: bytes, status: int = 200, **kw):
    transport = FakeTransport(body, status)
    kw.setdefault("prompt_cache", False)   # wire 形状断言默认走无缓存路径
    provider = AnthropicProvider(model="claude-x", api_key="sk-ant", transport=transport, **kw)
    return provider, transport


# -- 历史转换：system 抽离 / tool_result 合并 / tool_use blocks --------------------

def test_history_conversion():
    history = [
        Message.system("S1"),
        Message.system("S2"),
        Message.user("hi"),
        Message.assistant(tool_calls=[
            ToolCall("t1", "get_time", {"tz": "UTC"}),
            ToolCall("t2", "echo", {"m": "x"}),
        ]),
        Message.tool_result("t1", "12:00"),
        Message.tool_result("t2", "echo: x"),
        Message.assistant("done"),
    ]
    system, wire = to_anthropic_request_messages(history)

    assert system == "S1\n\nS2"
    assert wire == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "get_time", "input": {"tz": "UTC"}},
            {"type": "tool_use", "id": "t2", "name": "echo", "input": {"m": "x"}},
        ]},
        # 两个连续 tool 结果合并进同一条 user 消息
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "12:00"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "echo: x"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]


# -- 请求构造 ---------------------------------------------------------------------

def test_request_shape():
    provider, transport = make_provider(sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    ))
    asyncio.run(collect(provider, [Message.system("S"), Message.user("hi")], TOOLS))

    req = transport.last
    assert req.url == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant"
    assert req.headers["anthropic-version"] == ANTHROPIC_VERSION
    payload = req.json
    assert payload["system"] == "S"
    assert payload["max_tokens"] == AnthropicProvider.DEFAULT_MAX_TOKENS
    assert payload["stream"] is True
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["tools"] == [
        {"name": "get_time", "description": "查时间", "input_schema": {"type": "object"}}
    ]


# -- SSE 流式翻译 -----------------------------------------------------------------

def test_text_streaming_and_usage_merge():
    body = sse(
        {"type": "message_start",
         "message": {"id": "m1", "usage": {"input_tokens": 5, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )
    provider, _ = make_provider(body)
    events = asyncio.run(_events(provider))

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    finished = events[-1]
    assert finished.message.content == "Hello"
    assert finished.finish_reason == FINISH_STOP
    assert finished.usage == {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}


def test_tool_use_streaming_with_partial_json():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 2}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "tu1", "name": "get_time", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"time'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": 'zone": "UTC"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("几点")], TOOLS))

    assert finished.finish_reason == FINISH_TOOLS
    assert finished.message.tool_calls == [
        ToolCall(id="tu1", name="get_time", arguments={"timezone": "UTC"})
    ]
    assert finished.usage["input_tokens"] == 2


def test_text_and_tool_use_blocks_combined():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "让我查一下"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "tu1", "name": "get_time", "input": {}}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    )
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("几点")], TOOLS))
    assert finished.message.content == "让我查一下"
    assert finished.message.tool_calls[0].name == "get_time"


# -- 错误路径 ---------------------------------------------------------------------

def test_http_error():
    provider, _ = make_provider(b'{"error": "overloaded"}', status=529)
    with pytest.raises(LLMHTTPError) as ei:
        asyncio.run(collect(provider, [Message.user("x")]))
    assert ei.value.status == 529


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider(api_key=None, transport=FakeTransport(b""))
    provider.api_key = None
    with pytest.raises(ConfigError, match="API key"):
        asyncio.run(collect(provider, [Message.user("x")]))


async def _events(provider):
    return [e async for e in provider.stream([Message.user("hi")])]


# -- prompt caching（Step 30）------------------------------------------------------------------

def test_prompt_cache_default_enabled_and_breakpoints():
    """默认开启：tools 末尾 / system / 最后一条消息末尾共 3 个 ephemeral 断点。"""
    provider = AnthropicProvider(model="claude-x", api_key="k")
    assert provider.prompt_cache is True
    req = provider.build_request(
        [Message.system("S"), Message.user("hi")], TOOLS)
    import json as _json
    payload = _json.loads(req["body"].decode("utf-8"))
    # tools 最后一个声明带断点
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # system 转 block 形式并带断点
    assert payload["system"] == [{
        "type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}]
    # 最后一条消息（str content → block 化）带断点
    assert payload["messages"][-1]["content"] == [{
        "type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]


def test_prompt_cache_marks_last_block_of_tool_result_message():
    """最后一条是 tool 结果（blocks 列表）→ 断点打在最后一个 block 上。"""
    provider = AnthropicProvider(model="claude-x", api_key="k")
    history = [
        Message.user("几点"),
        Message.assistant(tool_calls=[ToolCall("t1", "get_time", {})]),
        Message.tool_result("t1", "12:00"),
        Message.tool_result("t2", "13:00"),
    ]
    import json as _json
    req = provider.build_request(history, None)
    payload = _json.loads(req["body"].decode("utf-8"))
    last = payload["messages"][-1]
    assert last["role"] == "user"
    assert len(last["content"]) == 2                     # 两个 tool_result 合并
    assert last["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in last["content"][0]     # 只标最后一个
    assert "tools" not in payload


def test_prompt_cache_disabled_no_markers():
    provider = AnthropicProvider(model="claude-x", api_key="k", prompt_cache=False)
    req = provider.build_request([Message.system("S"), Message.user("hi")], TOOLS)
    assert b"cache_control" not in req["body"]


def test_cache_usage_normalized():
    body = sse(
        {"type": "message_start", "message": {"usage": {
            "input_tokens": 10, "output_tokens": 1,
            "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 200}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 4}},
    )
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("q")]))
    assert finished.usage["cache_read"] == 5000
    assert finished.usage["cache_creation"] == 200
    assert finished.usage["input_tokens"] == 10
    assert finished.usage["output_tokens"] == 4


# -- 扩展思考回传（Step 34）---------------------------------------------------------------------------

def _thinking_sse(with_tool_use=True):
    """thinking(带签名) → text → [tool_use] 的完整 SSE 流。"""
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "先想第一步，"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "再想第二步。"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "sig-abc"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "结论来了"}},
        {"type": "content_block_stop", "index": 1},
    ]
    if with_tool_use:
        events += [
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "id": "tu9", "name": "get_time", "input": {}}},
            {"type": "content_block_delta", "index": 2,
             "delta": {"type": "input_json_delta", "partial_json": '{"tz": "UTC"}'}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        ]
    else:
        events.append({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
    return sse(*events)


def test_thinking_captured_into_provider_meta():
    provider, _ = make_provider(_thinking_sse())
    finished = asyncio.run(collect(provider, [Message.user("想一下")], TOOLS))

    meta = finished.message.provider_meta
    assert meta is not None and "anthropic_thinking" in meta
    thinking = meta["anthropic_thinking"]
    assert thinking == [{"type": "thinking",
                         "thinking": "先想第一步，再想第二步。",
                         "signature": "sig-abc"}]
    # 正文与工具调用不受影响
    assert finished.message.content == "结论来了"
    assert finished.message.tool_calls[0].name == "get_time"


def test_thinking_meta_roundtrips_into_next_request():
    """带 thinking meta 的 assistant 消息回传时，thinking 块必须在 content 最前。"""
    assistant_msg = Message.assistant(
        content="结论来了",
        tool_calls=[ToolCall("tu9", "get_time", {"tz": "UTC"})])
    assistant_msg.provider_meta = {"anthropic_thinking": [
        {"type": "thinking", "thinking": "先想第一步，再想第二步。", "signature": "sig-abc"}]}
    history = [
        Message.user("想一下"),
        assistant_msg,
        Message.tool_result("tu9", "12:00"),
    ]

    provider, transport = make_provider(_thinking_sse(with_tool_use=False))
    asyncio.run(collect(provider, history, TOOLS))

    wire = transport.last.json["messages"]
    assistant_wire = wire[1]
    blocks = assistant_wire["content"]
    # 顺序强制：thinking → text → tool_use
    assert blocks[0] == {"type": "thinking",
                         "thinking": "先想第一步，再想第二步。", "signature": "sig-abc"}
    assert blocks[1] == {"type": "text", "text": "结论来了"}
    assert blocks[2]["type"] == "tool_use" and blocks[2]["id"] == "tu9"


def test_redacted_thinking_passthrough():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "redacted_thinking", "data": "ENCRYPTED"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "好"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    )
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("x")]))
    assert finished.message.provider_meta["anthropic_thinking"] == [
        {"type": "redacted_thinking", "data": "ENCRYPTED"}]


def test_thinking_budget_clamped_and_max_tokens_adjusted():
    # 低于 API 最小值 → 钳到 1024；默认 max_tokens(4096) 已足够 → 不调整
    provider = AnthropicProvider(api_key="k", thinking_budget=100)
    payload = json.loads(provider.build_request([Message.user("x")], None)["body"])
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert payload["max_tokens"] == 4096

    # max_tokens <= budget → 自动抬到 budget+1024
    provider2 = AnthropicProvider(api_key="k", thinking_budget=2000, max_tokens=512)
    payload2 = json.loads(provider2.build_request([Message.user("x")], None)["body"])
    assert payload2["thinking"]["budget_tokens"] == 2000
    assert payload2["max_tokens"] == 3024

    # 已足够大则不动
    provider3 = AnthropicProvider(api_key="k", thinking_budget=2000, max_tokens=8000)
    payload3 = json.loads(provider3.build_request([Message.user("x")], None)["body"])
    assert payload3["max_tokens"] == 8000


def test_no_thinking_param_by_default():
    provider, transport = make_provider(_thinking_sse(with_tool_use=False))
    asyncio.run(collect(provider, [Message.user("x")]))
    assert "thinking" not in transport.last.json


def test_thinking_budget_via_config_registry(monkeypatch):
    from krelcode.llm.registry import create_provider, resolve_settings
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config = {"providers": {"anthropic": {"thinking_budget": 4000}}}
    settings = resolve_settings(provider="anthropic", config=config)
    provider = create_provider(settings)
    assert provider.name == "anthropic" and provider.thinking_budget == 4000


def test_session_codec_roundtrips_provider_meta():
    from krelcode.session.codec import history_from_dicts, history_to_dicts
    msg = Message.assistant("结论", tool_calls=[ToolCall("t1", "get_time", {})])
    msg.provider_meta = {"anthropic_thinking": [
        {"type": "thinking", "thinking": "嗯…", "signature": "s1"}]}
    history = [Message.user("q"), msg]
    restored = history_from_dicts(history_to_dicts(history))
    assert restored == history
    assert restored[1].provider_meta["anthropic_thinking"][0]["signature"] == "s1"
