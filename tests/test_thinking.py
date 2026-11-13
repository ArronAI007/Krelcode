"""Step 21 测试：思考模型统一支持（ThinkingDelta 全链路）。

覆盖：OpenAI 兼容 reasoning_content/reasoning 解析、Anthropic thinking 块解析、
AgentLoop 事件转发、渲染器 dim 思考流与收束、思考文本不进 content/history。
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fakes import FakeTransport, sse

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import (
    EventBus, LLMStreamDelta, LLMThinkingDelta, RunFinished, RunStarted,
)
from krelcode.core.messages import Message
from krelcode.llm import collect
from krelcode.llm.base import StreamFinished, TextDelta, ThinkingDelta
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.openai import OpenAIProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.render import THINKING_COLLAPSED, StreamRenderer


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


def test_renderer_thinking_is_collapsed_to_one_line():
    buf = Buf()
    renderer = StreamRenderer(write=buf.write, color=False)
    renderer(RunStarted("q"))
    renderer(LLMThinkingDelta("想一下"))
    renderer(LLMThinkingDelta("再想"))
    assert buf.text == ""                              # reasoning 正文不实时展开
    renderer(LLMStreamDelta("回复"))
    assert buf.text == f"{THINKING_COLLAPSED}\n⏺ 回复"
    assert "想一下" not in buf.text and "再想" not in buf.text
    assert buf.text.splitlines()[0] == THINKING_COLLAPSED


def test_expand_thinking_preserves_markdown_structure():
    thinking = (
        "# 分析\n\n"
        "- 检查 `config.toml`\n"
        "  - 保留缩进\n"
        "- **结论**\n\n"
        "```python\n"
        "print('ok')\n"
        "```"
    )
    buf = Buf()
    renderer = StreamRenderer(write=buf.write, color=False)
    renderer(RunStarted("q"))
    renderer(LLMThinkingDelta(thinking[:20]))
    renderer(LLMThinkingDelta(thinking[20:]))
    renderer(LLMStreamDelta("回复"))
    assert "# 分析" not in buf.text                    # 默认折叠
    assert renderer.expand_thinking() is True
    expanded = buf.text
    assert "✻ Thinking（Markdown）" in expanded
    assert thinking in expanded
    assert "\n\n- 检查" in expanded
    assert "```python\nprint('ok')\n```" in expanded


def test_thinking_aggregates_segments_without_extra_summary():
    buf = Buf()
    renderer = StreamRenderer(write=buf.write, color=False)
    renderer(RunStarted("q"))
    renderer(LLMThinkingDelta("第一段"))
    renderer(LLMStreamDelta("先调用工具"))
    renderer(LLMThinkingDelta("第二段"))
    renderer(RunFinished("完成"))
    assert buf.text.count(THINKING_COLLAPSED) == 1
    renderer.expand_thinking()
    assert "第一段\n\n第二段" in buf.text


def test_new_run_clears_previous_thinking():
    buf = Buf()
    renderer = StreamRenderer(write=buf.write, color=False)
    renderer(RunStarted("q1"))
    renderer(LLMThinkingDelta("旧思考"))
    renderer(RunFinished("a1"))
    assert renderer.expand_thinking() is True
    renderer(RunStarted("q2"))
    assert renderer.thinking_text == ""
    assert renderer.expand_thinking() is False


def test_thinking_slash_command_expands_cached_markdown(tmp_path):
    from krelcode.ui.repl import _handle_command

    buf = Buf()
    renderer = StreamRenderer(write=buf.write, color=False)
    renderer(RunStarted("q"))
    renderer(LLMThinkingDelta("# 步骤\n\n- 读取文件"))
    renderer(LLMStreamDelta("完成"))
    loop = SimpleNamespace()
    handled = asyncio.run(_handle_command(
        "/thinking", loop, renderer,
        session_path=None, sessions_dir=None, mcp_supervisor=None,
        mcp_reload=None, subagents=None, checkpoints=None, usage=None,
        restore_session=lambda *_: None, workspace_root=tmp_path,
        model_switch=None,
    ))
    assert handled is False
    assert "# 步骤\n\n- 读取文件" in buf.text
