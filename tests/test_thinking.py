"""Step 21 测试：思考模型统一支持（ThinkingDelta 全链路）。

覆盖：OpenAI 兼容 reasoning_content/reasoning 解析、Anthropic thinking 块解析、
AgentLoop 事件转发、渲染器 dim 思考流与收束、思考文本不进 content/history。
"""

import asyncio
from pathlib import Path

from fakes import FakeTransport, sse

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus, LLMThinkingDelta
from krelcode.core.messages import Message
from krelcode.llm import collect
from krelcode.llm.base import StreamFinished, TextDelta, ThinkingDelta
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.openai import OpenAIProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.render import StreamRenderer


# -- OpenAI 兼容：reasoning_content / reasoning -------------------------------------------------

def test_openai_reasoning_content_becomes_thinking_delta():
    body = sse(
        {"choices": [{"delta": {"reasoning_content": "让我想想，"}}]},
        {"choices": [{"delta": {"reasoning_content": "先查时间。"}}]},
        {"choices": [{"delta": {"content": "答案是42"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    transport = FakeTransport(body)
    provider = OpenAIProvider(model="deepseek-r1", api_key="k", transport=transport)

    async def go():
        return [e async for e in provider.stream([Message.user("q")])]

    events = asyncio.run(go())
    thinking = "".join(e.text for e in events if isinstance(e, ThinkingDelta))
    texts = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert thinking == "让我想想，先查时间。"
    assert texts == "答案是42"
    finished = [e for e in events if isinstance(e, StreamFinished)][0]
    # 思考绝不进入 message.content
    assert finished.message.content == "答案是42"
    assert "想想" not in finished.message.content


def test_openai_reasoning_key_also_supported():
    body = sse(
        {"choices": [{"delta": {"reasoning": "hmm"}}]},
        {"choices": [{"delta": {"content": "ok"}}]},
    )
    transport = FakeTransport(body)
    provider = OpenAIProvider(api_key="k", transport=transport)

    async def go():
        return [e async for e in provider.stream([Message.user("q")])]

    events = asyncio.run(go())
    assert any(isinstance(e, ThinkingDelta) and e.text == "hmm" for e in events)


# -- Anthropic：thinking 块 ----------------------------------------------------------------------

def test_anthropic_thinking_blocks():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "需要先读文件，"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "再做修改。"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "sig"}},   # 忽略
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "已完成"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 9}},
    )
    transport = FakeTransport(body)
    provider = AnthropicProvider(api_key="k", transport=transport)

    async def go():
        return [e async for e in provider.stream([Message.user("q")])]

    events = asyncio.run(go())
    thinking = "".join(e.text for e in events if isinstance(e, ThinkingDelta))
    assert thinking == "需要先读文件，再做修改。"
    finished = [e for e in events if isinstance(e, StreamFinished)][0]
    assert finished.message.content == "已完成"     # thinking 不混入 content
    assert finished.message.tool_calls is None


# -- AgentLoop 转发 --------------------------------------------------------------------------------

class ThinkingMockProvider:
    name = "think-mock"
    model = "m"
    from krelcode.llm.base import ProviderCapabilities
    capabilities = ProviderCapabilities()

    async def stream(self, messages, tools=None):
        yield ThinkingDelta("思考中…")
        yield TextDelta("最终回复")
        yield StreamFinished(message=Message.assistant("最终回复"),
                             finish_reason="stop", usage={})


def test_loop_forwards_thinking_events(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    loop = AgentLoop(
        llm=ThinkingMockProvider(), registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), bus=bus, system_prompt="SYS",
    )
    reply = asyncio.run(loop.run("q"))
    assert reply == "最终回复"
    thinking = [e for e in events if isinstance(e, LLMThinkingDelta)]
    assert len(thinking) == 1 and thinking[0].text == "思考中…"
    # 历史里只有最终回复，无思考文本
    assert loop.history[-1].content == "最终回复"


# -- 渲染器 ----------------------------------------------------------------------------------------

class Buf:
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    @property
    def text(self):
        return "".join(self.parts)


def test_renderer_thinking_stream_and_closing():
    from krelcode.core.events import LLMStreamDelta as EvDelta
    buf = Buf()
    r = StreamRenderer(write=buf.write, color=False)
    r(LLMThinkingDelta("想一下"))
    r(LLMThinkingDelta("再想"))
    assert buf.text == "✻ Thinking… 想一下再想"       # 前缀只打一次
    r(EvDelta("回复"))                                 # 思考行收束后接正文
    assert buf.text == "✻ Thinking… 想一下再想\n⏺ 回复"


def test_renderer_thinking_newlines_flattened():
    buf = Buf()
    r = StreamRenderer(write=buf.write, color=False)
    r(LLMThinkingDelta("第一行\n第二行"))
    assert "\n" not in buf.text                        # 思考内换行压平，防串行
    assert "第一行 第二行" in buf.text
