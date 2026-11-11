"""Step 23 测试：子代理增强——历史消毒、interrupt 打断、wait_many、多 id 工具。"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from krelcode.core.events import EventBus, SubagentFinished
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.subagent.manager import SubagentManager, sanitize_history
from krelcode.subagent.tools import SendToAgentTool, WaitAgentTool
from krelcode.tools.base import ToolContext, ToolResult

CTX = ToolContext(workspace_root=Path("."))


# -- 测试替身 --------------------------------------------------------------------------------

class EchoProvider:
    name = "echo-child"
    model = "echo"
    capabilities = ProviderCapabilities()

    def __init__(self, prefix: str = "完成: "):
        self.prefix = prefix

    async def stream(self, messages, tools=None):
        users = [m for m in messages if m.role == "user" and m.content]
        last = users[-1].content if users else "?"
        text = self.prefix + last
        yield StreamFinished(message=Message.assistant(text), finish_reason="stop", usage={})


class SleepyProvider:
    """第一轮调用 sleepy 工具（需注册），之后回 final。无状态、并发安全。"""
    name = "sleepy-child"
    model = "sleepy"
    capabilities = ProviderCapabilities()

    def __init__(self, seconds: float = 0.2, final: str = "done"):
        self.seconds = seconds
        self.final = final

    async def stream(self, messages, tools=None):
        n_assistant = sum(1 for m in messages if m.role == "assistant")
        tool_names = {(t or {}).get("name") for t in (tools or [])}
        if n_assistant == 0 and "sleepy" in tool_names:
            msg = Message.assistant(
                tool_calls=[ToolCall("sc1", "sleepy", {"s": self.seconds})])
        else:
            msg = Message.assistant(self.final)
        yield StreamFinished(message=msg, finish_reason="stop", usage={})


class SlowFirstProvider:
    """第一次调用卡 5 秒（模拟长任务），之后立即返回。用于 interrupt 测试。"""
    name = "slow-first"
    model = "slow"
    capabilities = ProviderCapabilities()

    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(5)
        yield StreamFinished(message=Message.assistant("打断后完成"),
                             finish_reason="stop", usage={})


class SleepyTool:
    name = "sleepy"
    description = "sleep s seconds"
    parameters: Dict[str, Any] = {
        "type": "object", "properties": {"s": {"type": "number"}}, "required": ["s"]}

    async def execute(self, args, ctx) -> ToolResult:
        await asyncio.sleep(float(args.get("s", 0)))
        return ToolResult(content="slept")


def make_manager(llm, tools=None, bus=None) -> SubagentManager:
    return SubagentManager(
        llm=llm,
        tool_factory=lambda: list(tools if tools is not None else [SleepyTool()]),
        tool_context=CTX,
        bus=bus or EventBus(),
        max_steps=10,
    )


# -- sanitize_history 单元测试 ------------------------------------------------------------------

def test_sanitize_pops_dangling_tool_and_toolcall_messages():
    history = [
        Message.system("S"),
        Message.user("u1"),
        Message.assistant(tool_calls=[ToolCall("c1", "t", {})]),
        Message.tool_result("c1", "r1"),
        Message.assistant(tool_calls=[ToolCall("c2", "t", {})]),  # 打断点：结果缺失
    ]
    sanitize_history(history)
    # 级联弹出直到安全边界：孤儿 assistant(tc) → 其下 tool → 再其下 assistant(tc)
    # 全部弹出（保留半对消息会造成新的不配对），落在最后的纯 user 消息上
    assert [m.role for m in history] == ["system", "user"]

    history2 = [
        Message.system("S"),
        Message.user("u1"),
        Message.assistant("完整回复"),
        Message.user("u2"),
        Message.assistant(tool_calls=[ToolCall("c", "t", {})]),
    ]
    sanitize_history(history2)
    assert history2[-1] == Message.user("u2")   # 纯文本 assistant/user 是安全边界


def test_sanitize_noop_on_clean_history():
    history = [Message.system("S"), Message.user("u"), Message.assistant("a")]
    before = list(history)
    sanitize_history(history)
    assert history == before


# -- interrupt ------------------------------------------------------------------------------------

def test_interrupt_cancels_and_sanitizes_history():
    async def go():
        provider = SlowFirstProvider()
        bus = EventBus()
        events = []
        bus.subscribe(events.append)
        mgr = make_manager(provider, tools=[], bus=bus)

        await mgr.spawn("长任务")
        await asyncio.sleep(0.1)
        handle = mgr.get("a1")
        assert mgr.is_running(handle)

        handle, action = await mgr.send_input("a1", "别做了，回答新问题", interrupt=True)
        assert action == "interrupted"
        handle = await mgr.wait("a1", timeout=5)

        assert handle.status == "completed"
        assert handle.result == "打断后完成"
        assert provider.calls == 2

        # Step 26 起：取消时 AgentLoop._repair_after_cancel 消毒历史并补
        # "[已被用户中止]" assistant 消息 → 打断消息作为干净的新 user 轮追加
        roles = [m.role for m in handle.loop.history]
        assert roles == ["system", "user", "assistant", "user", "assistant"]
        assert handle.loop.history[1].content == "长任务"
        assert handle.loop.history[2].content == "[已被用户中止]"
        assert handle.loop.history[3].content == "别做了，回答新问题"

        # 取消也发了 SubagentFinished(cancelled) 事件
        finished = [e for e in events if isinstance(e, SubagentFinished)]
        assert any(e.status == "cancelled" for e in finished)
        assert finished[-1].status == "completed"
        await mgr.shutdown()
    asyncio.run(go())


def test_interrupt_preserves_queue():
    async def go():
        mgr = make_manager(SlowFirstProvider(), tools=[])
        await mgr.spawn("长任务")
        await asyncio.sleep(0.1)
        # 先排一条，再打断：队列消息保留，打断消息先处理
        await mgr.send_input("a1", "排队A")
        handle, action = await mgr.send_input("a1", "插队B", interrupt=True)
        assert action == "interrupted" and handle.queue == ["排队A"]
        handle = await mgr.wait("a1", timeout=10)
        assert handle.status == "completed" and handle.turns == 2
        users = [m.content for m in handle.loop.history if m.role == "user"]
        assert any("插队B" in u for u in users)
        assert "排队A" in users
        await mgr.shutdown()
    asyncio.run(go())


def test_send_tool_interrupt_param():
    async def go():
        mgr = make_manager(SlowFirstProvider(), tools=[])
        await mgr.spawn("长任务")
        await asyncio.sleep(0.1)
        tool = SendToAgentTool(mgr)
        r = await tool.execute(
            {"agent_id": "a1", "message": "停", "interrupt": True}, CTX)
        assert not r.is_error and r.data["action"] == "interrupted"
        r2 = await WaitAgentTool(mgr).execute({"agent_id": "a1", "timeout": 5}, CTX)
        assert "[a1] completed" in r2.content
        await mgr.shutdown()
    asyncio.run(go())


# -- wait_many / 多 id 工具 -------------------------------------------------------------------------

def test_wait_many_parallel():
    async def go():
        mgr = make_manager(SleepyProvider(seconds=0.2))
        await mgr.spawn("t1")
        await mgr.spawn("t2")
        start = time.monotonic()
        handles = await mgr.wait_many(["a1", "a2"], timeout=5)
        elapsed = time.monotonic() - start
        assert [h.status for h in handles] == ["completed", "completed"]
        assert elapsed < 0.6, f"两个 0.2s 子代理应并行，实际 {elapsed:.2f}s"
        await mgr.shutdown()
    asyncio.run(go())


def test_wait_many_unknown_id():
    async def go():
        mgr = make_manager(EchoProvider())
        with pytest.raises(KeyError):
            await mgr.wait_many(["a1", "ghost"], timeout=1)
    asyncio.run(go())


def test_wait_tool_multi_id_sections():
    async def go():
        mgr = make_manager(SleepyProvider(seconds=0.05))
        await mgr.spawn("t1")
        await mgr.spawn("t2")
        tool = WaitAgentTool(mgr)
        r = await tool.execute({"agent_id": "a1, a2", "timeout": 5}, CTX)
        assert not r.is_error
        assert "[a1] completed" in r.content and "[a2] completed" in r.content
        assert r.data["statuses"] == {"a1": "completed", "a2": "completed"}
        await mgr.shutdown()
    asyncio.run(go())


def test_wait_tool_single_id_backward_compatible():
    async def go():
        mgr = make_manager(EchoProvider())
        await mgr.spawn("单个")
        r = await WaitAgentTool(mgr).execute({"agent_id": "a1", "timeout": 5}, CTX)
        assert r.content == "[a1] completed\n完成: 单个"     # 与旧版格式一致
        assert r.data["status"] == "completed"
        await mgr.shutdown()
    asyncio.run(go())
