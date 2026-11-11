"""Step 7 测试：token 估算、上下文压缩（边界安全/失败安全）、大输出落盘。"""

import asyncio
from pathlib import Path
from typing import Any, Dict

import pytest

from krelcode.agent.compression import (
    EVICTION_MARKER, SUMMARY_PREFIX, ContextManager, MIN_EVICT_CHARS,
    MIN_MESSAGES_TO_COMPRESS, render_messages,
)
from krelcode.agent.loop import AgentLoop
from krelcode.agent.tokens import (
    estimate_history_tokens, estimate_message_tokens, estimate_text_tokens,
)
from krelcode.core.events import ContextCompressed, EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.core.errors import LLMError
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


# -- token 估算 ----------------------------------------------------------------------

def test_estimate_cjk_and_ascii():
    assert estimate_text_tokens("中文中文") == 4          # CJK ≈ 1 token/字
    assert estimate_text_tokens("abcd") == 1              # ASCII ≈ 4 字符/token
    assert estimate_text_tokens("abcde") == 2             # 向上取整
    assert estimate_text_tokens("") == 0
    mixed = estimate_text_tokens("你好 world!")
    assert 2 <= mixed <= 5


def test_estimate_message_includes_tool_calls():
    plain = estimate_message_tokens(Message.user("hi"))
    with_calls = estimate_message_tokens(
        Message.assistant(tool_calls=[ToolCall("c", "grep", {"pattern": "x" * 400})]))
    assert with_calls > plain
    assert estimate_history_tokens([Message.user("a"), Message.assistant("b")]) > 0


# -- 压缩 --------------------------------------------------------------------------

def long_history(n=12):
    h = [Message.system("SYS")]
    for i in range(n):
        h.append(Message.user(f"问题{i} " + "x" * 200))
        h.append(Message.assistant(f"回答{i} " + "y" * 200))
    return h


def test_no_compression_under_budget():
    h = long_history(4)
    cm = ContextManager(MockProvider(), max_tokens=10**9)
    assert asyncio.run(cm.maybe_compress(h)) is False
    assert len(h) == 9


def test_too_short_history_not_compressed():
    h = [Message.system("S"), Message.user("a"), Message.assistant("b")]
    cm = ContextManager(MockProvider(), max_tokens=1)
    assert asyncio.run(cm.maybe_compress(h)) is False  # < MIN_MESSAGES_TO_COMPRESS


def test_compression_replaces_middle_with_summary():
    h = long_history(12)  # sys + 24 = 25 条
    provider = MockProvider(scripted=["这是摘要：完成了 A、B、C。"])
    cm = ContextManager(provider, max_tokens=10, keep_recent=6)

    assert asyncio.run(cm.maybe_compress(h)) is True

    assert h[0] == Message.system("SYS")                       # 头部保留
    assert h[1].role == "user" and h[1].content.startswith(SUMMARY_PREFIX)
    assert "完成了 A、B、C" in h[1].content
    assert len(h) == 1 + 1 + 6                                  # system + 摘要 + keep_recent
    assert h[-1].content.startswith("回答11")                    # 最新消息保留
    # 摘要调用的 prompt 结构
    summary_request = provider.calls[0]
    assert summary_request[0].role == "system"
    assert "[user]" in summary_request[1].content               # 中段被渲染进摘要请求


def test_safe_cut_never_starts_with_orphan_tool():
    # 构造：保留区切点恰好落在 tool 消息上
    h = [
        Message.system("SYS"),
        Message.user("u1"),
        Message.assistant(tool_calls=[ToolCall("c1", "echo", {"m": "hi"})]),
        Message.tool_result("c1", "echo: hi"),
        Message.assistant("好了"),
        Message.user("u2"),
        Message.assistant("a2"),
    ]
    provider = MockProvider(scripted=["摘要"])
    cm = ContextManager(provider, max_tokens=1, keep_recent=4)  # cut=len-4=3 → tool 消息
    assert asyncio.run(cm.maybe_compress(h)) is True
    kept = h[2:]  # system + 摘要之后
    assert all(m.role != "tool" for m in kept[:1])  # 保留区不以孤儿 tool 开头
    assert kept[0].role in ("user", "assistant")


def test_summary_failure_falls_back_to_placeholder():
    class BoomProvider:
        name = "boom"; model = "boom"
        async def stream(self, messages, tools=None):
            raise LLMError("摘要服务不可用")
            yield  # pragma: no cover

    h = long_history(10)
    cm = ContextManager(BoomProvider(), max_tokens=1, keep_recent=4)
    assert asyncio.run(cm.maybe_compress(h)) is True
    assert "摘要失败" in h[1].content  # 占位摘要，主流程不崩


def test_render_messages_truncates_long_content():
    rendered = render_messages([Message.user("z" * 5000)], max_chars=100)
    assert "截断" in rendered and len(rendered) < 300


# -- 主循环集成 ----------------------------------------------------------------------

class BigOutputTool:
    name = "bigout"
    description = "returns huge output"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, args, ctx) -> ToolResult:
        return ToolResult(content="Q" * 50_000)


def test_loop_emits_context_compressed(tmp_path):
    provider = MockProvider(scripted=["中段摘要内容", "最终回复"])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        system_prompt="SYS",
        context_manager=ContextManager(provider, max_tokens=1, keep_recent=2),
    )
    # 预填足够长的历史，让 run() 的第一次 _llm_turn 触发压缩
    for i in range(MIN_MESSAGES_TO_COMPRESS + 2):
        loop.history.append(Message.user(f"旧消息{i} " + "x" * 100))
        loop.history.append(Message.assistant(f"旧回复{i}"))

    events = []
    loop.bus.subscribe(events.append)
    reply = asyncio.run(loop.run("新问题"))

    assert reply == "最终回复"
    compressed = [e for e in events if isinstance(e, ContextCompressed)]
    assert len(compressed) == 1
    assert compressed[0].after_tokens < compressed[0].before_tokens
    assert any(m.role == "user" and m.content.startswith(SUMMARY_PREFIX) for m in loop.history)


def test_large_tool_output_persisted_to_disk(tmp_path):
    scripted = [
        Message.assistant(tool_calls=[ToolCall("call_big", "bigout", {})]),
        "处理完了",
    ]
    loop = AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([BigOutputTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        system_prompt="SYS",
        tool_output_inline_limit=5_000,
    )
    asyncio.run(loop.run("跑一下"))

    # 全文落盘
    out_files = list((tmp_path / ".krelcode" / "tool_outputs").glob("bigout_call_big*.txt"))
    assert len(out_files) == 1
    assert len(out_files[0].read_text(encoding="utf-8")) == 50_000

    # 历史里只有截断版 + 存盘引用
    tool_msg = loop.history[3]
    assert tool_msg.role == "tool"
    assert len(tool_msg.content) < 10_000
    assert "全文已存盘" in tool_msg.content
    assert str(out_files[0]) in tool_msg.content


def test_small_tool_output_not_persisted(tmp_path):
    class SmallTool(BigOutputTool):
        name = "small"
        async def execute(self, args, ctx):
            return ToolResult(content="just a little")

    scripted = [
        Message.assistant(tool_calls=[ToolCall("c1", "small", {})]),
        "ok",
    ]
    loop = AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([SmallTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
    )
    asyncio.run(loop.run("x"))
    # 该 loop 无 system prompt：history = user, assistant(tc), tool, assistant
    assert loop.history[2].content == "just a little"
    assert not (tmp_path / ".krelcode" / "tool_outputs").exists()


# -- 工具结果驱逐（第一道防线，Step 25）----------------------------------------------------------

def make_tool_history(n_tools=8, size=500):
    """system + n_tools 轮（user → assistant(tool_calls) → tool(大输出)）+ 收尾。"""
    h = [Message.system("SYS")]
    for i in range(n_tools):
        h.append(Message.user(f"q{i}"))
        h.append(Message.assistant(tool_calls=[ToolCall(f"c{i}", "t", {})]))
        h.append(Message.tool_result(f"c{i}", "x" * size))
    h.append(Message.assistant("done"))
    return h


def test_evict_keeps_recent_marks_old_and_preserves_structure():
    h = make_tool_history(8, size=500)
    roles_before = [m.role for m in h]
    cm = ContextManager(MockProvider(), max_tokens=10**9, keep_recent_tools=3)

    n = cm.evict_old_tool_results(h)
    assert n == 5
    tools = [m for m in h if m.role == "tool"]
    for m in tools[:5]:
        assert m.content.startswith("[旧工具输出已驱逐") and "500" in m.content
    for m in tools[5:]:
        assert m.content == "x" * 500                 # 最近 3 条原样保留
    assert [m.role for m in h] == roles_before        # 配对结构未动
    assert cm.evict_old_tool_results(h) == 0          # 幂等：已驱逐的不再处理


def test_evict_skips_short_results():
    h = make_tool_history(6, size=MIN_EVICT_CHARS - 1)
    cm = ContextManager(MockProvider(), max_tokens=10**9, keep_recent_tools=2)
    assert cm.evict_old_tool_results(h) == 0          # 太短，驱逐无收益


def test_eviction_resolves_budget_without_llm_call():
    h = make_tool_history(10, size=5000)              # ~12.5K tokens
    provider = MockProvider(scripted=["不该被消费的摘要"])
    cm = ContextManager(provider, max_tokens=6000, keep_recent_tools=3)

    assert asyncio.run(cm.maybe_compress(h)) is True
    assert provider.calls == []                       # 零 LLM 调用（免费防线生效）
    assert not cm.over_budget(h)


def test_evict_then_summarize_when_still_over_budget():
    h = make_tool_history(10, size=5000)
    provider = MockProvider(scripted=["兜底摘要"])
    cm = ContextManager(provider, max_tokens=100, keep_recent_tools=3, keep_recent=4)

    assert asyncio.run(cm.maybe_compress(h)) is True
    assert len(provider.calls) == 1                   # 驱逐不够 → LLM 摘要兜底
    assert h[1].content.startswith(SUMMARY_PREFIX)
    assert "兜底摘要" in h[1].content


def test_evicted_marker_mentions_reread():
    h = make_tool_history(4, size=1000)
    cm = ContextManager(MockProvider(), max_tokens=10**9, keep_recent_tools=1)
    cm.evict_old_tool_results(h)
    evicted = [m for m in h if m.role == "tool" and "已驱逐" in (m.content or "")]
    assert evicted and "read_file" in evicted[0].content   # 提示可重新获取
