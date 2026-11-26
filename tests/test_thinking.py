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
from krelcode.ui.render import (
    THINKING_COLLAPSED,
    THINKING_COLLAPSED_CLICKABLE,
    THINKING_LIVE,
    StreamRenderer,
)


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
    assert buf.text == THINKING_LIVE                   # 只显示一行实时思考状态
    renderer(LLMStreamDelta("回复"))
    assert THINKING_COLLAPSED in buf.text
    assert buf.text.endswith("\n⏺ 回复")
    assert "想一下" not in buf.text and "再想" not in buf.text
    assert "正文已折叠" not in THINKING_COLLAPSED


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
    assert "✻ Thinking" in expanded
    assert "▌ 分析" in expanded
    assert "• 检查 config.toml" in expanded
    assert "  • 保留缩进" in expanded
    assert "• 结论" in expanded
    assert "  ┌─ python" in expanded
    assert "  │ print('ok')" in expanded
    assert "# 分析" not in expanded
    assert "**结论**" not in expanded


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
    assert "✻ Thinking" in buf.text
    assert "▌ 步骤" in buf.text
    assert "• 读取文件" in buf.text


def test_clickable_thinking_summary_registers_anchor():
    buf = Buf()
    clicked = []
    renderer = StreamRenderer(
        write=buf.write,
        color=False,
        on_thinking_line=lambda: clicked.append(True),
        tool_lines_clickable=True,
    )
    renderer(RunStarted("q"))
    renderer(LLMThinkingDelta("需要检查文件"))
    renderer(LLMStreamDelta("完成"))

    assert THINKING_COLLAPSED_CLICKABLE in buf.text
    assert clicked == [True]


def test_repeated_thinking_expand_is_idempotent_and_incremental():
    buf = Buf()
    renderer = StreamRenderer(write=buf.write, color=False)
    renderer(RunStarted("q"))
    renderer(LLMThinkingDelta("第一段推理"))
    renderer(LLMStreamDelta("回复"))

    assert renderer.expand_thinking()
    first = buf.text
    assert first.count("第一段推理") == 1
    assert renderer.expand_thinking()
    assert buf.text == first

    renderer(LLMThinkingDelta("第二段推理"))
    assert renderer.expand_thinking()
    assert "第一段推理" in buf.text
    assert "第二段推理" in buf.text
    assert buf.text.count("第一段推理") == 1
    assert "✻ Thinking（继续）" in buf.text


def test_assistant_markdown_is_rendered_not_echoed_as_source():
    buf = Buf()
    renderer = StreamRenderer(write=buf.write, color=False)
    renderer(RunStarted("q"))
    renderer(LLMStreamDelta("# 结果\n\n- **测试**通过\n- 使用 `pytest`\n"))
    renderer(RunFinished("# 结果\n\n- **测试**通过\n- 使用 `pytest`\n"))

    text = buf.text
    assert "⏺ ▌ 结果" in text
    assert "• 测试通过" in text
    assert "• 使用 pytest" in text
    assert "# 结果" not in text
    assert "**测试**" not in text
    assert "`pytest`" not in text
