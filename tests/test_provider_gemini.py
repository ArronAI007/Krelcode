"""providers/gemini 测试：第三种 wire format 的翻译正确性（统一抽象的再验证）。"""

import asyncio
import json

import pytest

from fakes import FakeTransport, sse
from krelcode.agent.loop import AgentLoop
from krelcode.core.errors import ConfigError, LLMHTTPError
from krelcode.core.messages import Message, ToolCall
from krelcode.llm import collect
from krelcode.llm.base import FINISH_LENGTH, FINISH_STOP, FINISH_TOOLS, StreamFinished, TextDelta
from krelcode.llm.providers.gemini import GeminiProvider, to_gemini_contents
from krelcode.llm.registry import PROVIDERS, create_provider, resolve_settings
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.time_tool import GetTimeTool
from krelcode.tools.registry import ToolRegistry

TOOLS = [{"name": "get_time", "description": "查时间", "parameters": {"type": "object"}}]


def make_provider(body, status: int = 200, **kw):
    transport = FakeTransport(body, status)
    provider = GeminiProvider(model="gemini-test", api_key="gk-test",
                              transport=transport, **kw)
    return provider, transport


# -- 历史转换 --------------------------------------------------------------------------------------

def test_history_conversion_all_shapes():
    history = [
        Message.system("S1"),
        Message.system("S2"),
        Message.user("几点"),
        Message.assistant("我查一下", tool_calls=[ToolCall("gem-1", "get_time", {"tz": "UTC"})]),
        Message.tool_result("gem-1", "12:00"),
        Message.tool_result("gem-2", "第二个结果"),      # 连续 tool → 合并
        Message.assistant("中午了"),
    ]
    system_instruction, contents = to_gemini_contents(history)

    assert system_instruction == {"parts": [{"text": "S1\n\nS2"}]}
    assert contents == [
        {"role": "user", "parts": [{"text": "几点"}]},
        {"role": "model", "parts": [
            {"text": "我查一下"},
            {"functionCall": {"name": "get_time", "args": {"tz": "UTC"}}},
        ]},
        # 两个 tool 结果合并进一条 user 消息；name 由 id→name 映射还原，
        # 映射不到的（gem-2）落到 unknown_function
        {"role": "user", "parts": [
            {"functionResponse": {"name": "get_time", "response": {"content": "12:00"}}},
            {"functionResponse": {"name": "unknown_function", "response": {"content": "第二个结果"}}},
        ]},
        {"role": "model", "parts": [{"text": "中午了"}]},
    ]


def test_history_without_system():
    si, contents = to_gemini_contents([Message.user("hi")])
    assert si is None
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


# -- 请求形态 --------------------------------------------------------------------------------------

def test_request_shape():
    provider, transport = make_provider(sse(
        {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]},
    ))
    asyncio.run(collect(provider, [Message.system("S"), Message.user("hi")], TOOLS))

    req = transport.last
    assert req.url == ("https://generativelanguage.googleapis.com/v1beta/"
                       "models/gemini-test:streamGenerateContent?alt=sse")
    assert req.headers["x-goog-api-key"] == "gk-test"
    payload = req.json
    assert payload["systemInstruction"] == {"parts": [{"text": "S"}]}
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert payload["tools"] == [{"functionDeclarations": [
        {"name": "get_time", "description": "查时间", "parameters": {"type": "object"}}]}]


def test_no_tools_key_when_empty():
    provider, transport = make_provider(sse(
        {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}))
    asyncio.run(collect(provider, [Message.user("q")], None))
    assert "tools" not in transport.last.json


# -- 流式解析 --------------------------------------------------------------------------------------

def test_text_streaming_and_usage_normalization():
    body = sse(
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "你"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "好"}]}, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3,
                            "totalTokenCount": 8}},
    )
    provider, _ = make_provider(body)

    async def go():
        return [e async for e in provider.stream([Message.user("hi")])]

    events = asyncio.run(go())
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["你", "好"]
    finished = events[-1]
    assert isinstance(finished, StreamFinished)
    assert finished.message.content == "你好"
    assert finished.finish_reason == FINISH_STOP
    assert finished.usage == {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}


def test_function_call_stream_synthetic_ids():
    body = sse(
        {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "get_time", "args": {"tz": "UTC"}}},
            {"functionCall": {"name": "echo", "args": {}}},
        ]}, "finishReason": "STOP"}]},
    )
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("x")], TOOLS))

    # Gemini 无 id → 合成 gem-N；有调用时 finishReason 规范化为 tool_calls
    assert finished.finish_reason == FINISH_TOOLS
    assert finished.message.tool_calls == [
        ToolCall("gem-1", "get_time", {"tz": "UTC"}),
        ToolCall("gem-2", "echo", {}),
    ]


def test_function_call_invalid_args_marked():
    body = sse({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "t", "args": "not-a-dict"}}]}, "finishReason": "STOP"}]})
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("x")], TOOLS))
    tc = finished.message.tool_calls[0]
    assert tc.has_invalid_arguments


def test_max_tokens_finish_mapping():
    body = sse({"candidates": [{"content": {"parts": [{"text": "半句"}]},
                                 "finishReason": "MAX_TOKENS"}]})
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("x")]))
    assert finished.finish_reason == FINISH_LENGTH


def test_safety_finish_passthrough_lowercase():
    body = sse({"candidates": [{"finishReason": "SAFETY"}]})
    provider, _ = make_provider(body)
    finished = asyncio.run(collect(provider, [Message.user("x")]))
    assert finished.finish_reason == "safety"


# -- 错误路径 ---------------------------------------------------------------------------------------

def test_http_error():
    provider, _ = make_provider(b'{"error": {"message": "quota"}}', status=429)
    with pytest.raises(LLMHTTPError) as ei:
        asyncio.run(collect(provider, [Message.user("x")]))
    assert ei.value.status == 429 and "quota" in str(ei.value)


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    provider = GeminiProvider(api_key=None, transport=FakeTransport(b""))
    provider.api_key = None
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        asyncio.run(collect(provider, [Message.user("x")]))


def test_google_api_key_env_fallback(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "gk-env")
    provider = GeminiProvider(api_key=None, transport=FakeTransport(sse(
        {"candidates": [{"content": {"parts": [{"text": "x"}]}}]})))
    assert provider.api_key == "gk-env"


# -- registry 接入 ------------------------------------------------------------------------------------

def test_registry_gemini(monkeypatch):
    assert "gemini" in PROVIDERS
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    settings = resolve_settings(provider="gemini", model="m")
    provider = create_provider(settings)
    assert provider.name == "gemini" and provider.model == "m"


# -- 与 AgentLoop 全链路集成（两轮：functionCall → 工具执行 → functionResponse → 文本）----------------

def test_loop_integration_full_wire_roundtrip(tmp_path):
    call_body = sse({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "get_time", "args": {"timezone": "UTC"}}}]},
        "finishReason": "STOP"}]})
    text_body = sse({"candidates": [{"content": {"parts": [{"text": "现在是 UTC 时间。"}]},
                                      "finishReason": "STOP"}]})
    transport = FakeTransport([call_body, text_body])
    provider = GeminiProvider(model="m", api_key="k", transport=transport)

    loop = AgentLoop(
        llm=provider, registry=ToolRegistry([GetTimeTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), system_prompt="SYS",
    )
    reply = asyncio.run(loop.run("现在 UTC 几点？"))

    assert reply == "现在是 UTC 时间。"
    # 历史：system, user, model(functionCall), tool(result), model(final)
    assert [m.role for m in loop.history] == \
        ["system", "user", "assistant", "tool", "assistant"]
    assert loop.history[2].tool_calls[0].id == "gem-1"

    # 第二次请求的 wire：functionResponse 用 name 配对（id→name 映射生效）
    second_payload = transport.requests[1].json
    fn_resp_part = second_payload["contents"][2]["parts"][0]
    assert fn_resp_part["functionResponse"]["name"] == "get_time"
    assert "T" in fn_resp_part["functionResponse"]["response"]["content"]  # ISO 时间结果
