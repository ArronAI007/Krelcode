"""Step 13 测试：子代理/后台任务。

子代理 provider 用无状态实现（按收到的历史决定回复），
避免共享脚本 MockProvider 在并发下竞争消费。
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus, SubagentFinished, SubagentSpawned
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import ProviderCapabilities, StreamFinished, TextDelta
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SubagentManager
from krelcode.subagent.tools import (
    ListAgentsTool, SendToAgentTool, SpawnAgentTool, WaitAgentTool,
)
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry

CTX = ToolContext(workspace_root=Path("."))


# -- 测试替身 ------------------------------------------------------------------------------

class EchoProvider:
    """无状态：回复 = 前缀 + 最后一条 user 消息。并发安全。"""
    name = "echo-child"
    model = "echo"
    capabilities = ProviderCapabilities()

    def __init__(self, prefix: str = "完成: "):
        self.prefix = prefix

    async def stream(self, messages, tools=None):
        users = [m for m in messages if m.role == "user" and m.content]
        last = users[-1].content if users else "?"
        text = self.prefix + last
        yield TextDelta(text)
        yield StreamFinished(message=Message.assistant(text), finish_reason="stop", usage={})


class SleepyProvider:
    """无状态：本轮历史里还没有 assistant 消息且注册了 sleepy 工具 → 先睡一觉，否则回 final。"""
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
        policy=AllowAllPolicy(),
        bus=bus or EventBus(),
        max_steps=10,
    )


# -- 基本生命周期 ----------------------------------------------------------------------------

def test_spawn_and_wait_completed():
    async def go():
        mgr = make_manager(EchoProvider())
        handle = await mgr.spawn("统计文件数")
        assert handle.id == "a1"
        result = await mgr.wait("a1", timeout=5)
        assert result.status == "completed"
        assert result.result == "完成: 统计文件数"
        assert result.turns == 1
        await mgr.shutdown()
    asyncio.run(go())


def test_wait_unknown_id():
    async def go():
        mgr = make_manager(EchoProvider())
        with pytest.raises(KeyError):
            await mgr.wait("ghost")
    asyncio.run(go())


def test_wait_timeout_returns_running_then_completes():
    async def go():
        mgr = make_manager(SleepyProvider(seconds=0.4))
        await mgr.spawn("慢任务")
        h = await mgr.wait("a1", timeout=0.05)
        assert h.status == "running"          # 超时不取消任务
        h = await mgr.wait("a1", timeout=5)
        assert h.status == "completed" and h.result == "done"
        await mgr.shutdown()
    asyncio.run(go())


def test_send_input_continues_child_context():
    async def go():
        mgr = make_manager(EchoProvider())
        await mgr.spawn("任务一")
        await mgr.wait("a1", timeout=5)
        handle, action = await mgr.send_input("a1", "任务二")
        assert action == "started"          # 空闲 → 立即开跑
        await mgr.wait("a1", timeout=5)
        assert handle.result == "完成: 任务二"
        assert handle.turns == 2
        # 子代理历史延续：两轮 user 消息都在
        users = [m.content for m in handle.loop.history if m.role == "user"]
        assert users == ["任务一", "任务二"]
        await mgr.shutdown()
    asyncio.run(go())


def test_send_input_while_running_queues():
    """Step 23 语义：运行中 send → 排队（不再是拒绝），排空后 wait 返回。"""
    async def go():
        mgr = make_manager(SleepyProvider(seconds=0.2))
        await mgr.spawn("慢任务")
        await asyncio.sleep(0.02)
        handle, action = await mgr.send_input("a1", "排队消息")
        assert action == "queued"
        assert handle.queue == ["排队消息"]
        assert "排队 1" in mgr.status_lines()[0]      # 观测面显示队列
        # wait 的语义 = 队列排空：两轮都完成
        handle = await mgr.wait("a1", timeout=10)
        assert handle.status == "completed" and handle.turns == 2
        users = [m.content for m in handle.loop.history if m.role == "user"]
        assert users == ["慢任务", "排队消息"]
        await mgr.shutdown()
    asyncio.run(go())


def test_parallel_children():
    async def go():
        mgr = make_manager(SleepyProvider(seconds=0.25))
        start = time.monotonic()
        for i in range(3):
            await mgr.spawn(f"并行任务{i}")
        for aid in ("a1", "a2", "a3"):
            h = await mgr.wait(aid, timeout=5)
            assert h.status == "completed"
        elapsed = time.monotonic() - start
        assert elapsed < 0.7, f"三个 0.25s 子任务应并行，实际 {elapsed:.2f}s"
        await mgr.shutdown()
    asyncio.run(go())


def test_shutdown_cancels_running():
    async def go():
        mgr = make_manager(SleepyProvider(seconds=5))
        await mgr.spawn("长任务")
        await asyncio.sleep(0.1)
        await mgr.shutdown()
        assert mgr.get("a1").status == "cancelled"
    asyncio.run(go())


def test_child_registry_has_no_recursion():
    async def go():
        mgr = make_manager(EchoProvider())
        await mgr.spawn("x")
        await mgr.wait("a1", timeout=5)
        child_names = mgr.get("a1").loop.registry.names()
        assert "spawn_agent" not in child_names
        assert "wait_agent" not in child_names
        assert child_names == ["sleepy"]  # 只拿到 tool_factory 给的工具
        await mgr.shutdown()
    asyncio.run(go())


def test_events_emitted():
    async def go():
        bus = EventBus()
        events = []
        bus.subscribe(events.append)
        mgr = make_manager(EchoProvider(), bus=bus)
        await mgr.spawn("事件任务")
        await mgr.wait("a1", timeout=5)
        spawned = [e for e in events if isinstance(e, SubagentSpawned)]
        finished = [e for e in events if isinstance(e, SubagentFinished)]
        assert len(spawned) == 1 and spawned[0].agent_id == "a1"
        assert len(finished) == 1 and finished[0].status == "completed"
        assert "完成: 事件任务" in finished[0].preview
        await mgr.shutdown()
    asyncio.run(go())


# -- 工具表面 --------------------------------------------------------------------------------

def test_tool_surface_spawn_wait_list_send():
    async def go():
        mgr = make_manager(EchoProvider())
        spawn = SpawnAgentTool(mgr)
        wait = WaitAgentTool(mgr)
        listing = ListAgentsTool(mgr)
        send = SendToAgentTool(mgr)

        r = await spawn.execute({"task": "工具链任务"}, CTX)
        assert not r.is_error and r.data["agent_id"] == "a1"

        r = await wait.execute({"agent_id": "a1", "timeout": 5}, CTX)
        assert not r.is_error and "[a1] completed" in r.content and "工具链任务" in r.content

        r = await listing.execute({}, CTX)
        assert "a1 [completed]" in r.content

        # 未知 id / 空参数 → is_error 而非异常
        assert (await wait.execute({"agent_id": "ghost"}, CTX)).is_error
        assert (await spawn.execute({"task": "  "}, CTX)).is_error
        assert (await send.execute({"agent_id": "a1", "message": ""}, CTX)).is_error

        r = await send.execute({"agent_id": "a1", "message": "追加"}, CTX)
        assert not r.is_error
        r = await wait.execute({"agent_id": "a1"}, CTX)
        assert "完成: 追加" in r.content
        await mgr.shutdown()
    asyncio.run(go())


def test_wait_tool_timeout_reports_running():
    async def go():
        mgr = make_manager(SleepyProvider(seconds=0.5))
        await mgr.spawn("慢")
        r = await WaitAgentTool(mgr).execute({"agent_id": "a1", "timeout": 0.05}, CTX)
        assert not r.is_error and "仍在运行" in r.content
        await mgr.shutdown()
    asyncio.run(go())


# -- 端到端：父代理委派 -------------------------------------------------------------------------

def test_parent_delegates_via_tool_calls():
    async def go():
        bus = EventBus()
        manager = make_manager(SleepyProvider(seconds=0.05), bus=bus)
        parent_llm = MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("s1", "spawn_agent", {"task": "后台计算"})]),
            Message.assistant(tool_calls=[
                ToolCall("w1", "wait_agent", {"agent_id": "a1", "timeout": 5})]),
            "委派完成：子代理报告 done。",
        ])
        registry = ToolRegistry(manager.build_tools())
        parent = AgentLoop(
            llm=parent_llm, registry=registry, tool_context=CTX,
            policy=AllowAllPolicy(), bus=bus, system_prompt="SYS",
        )
        parent_history_len_before = len(parent.history)

        reply = await parent.run("把计算委派给子代理")

        assert reply == "委派完成：子代理报告 done。"
        # 父代理历史里能看到 spawn 与 wait 的工具结果
        tool_msgs = [m for m in parent.history if m.role == "tool"]
        assert "已启动子代理 a1" in tool_msgs[0].content
        assert "[a1] completed" in tool_msgs[1].content and "done" in tool_msgs[1].content
        # 子代理确实跑了自己的循环（独立历史）
        child = manager.get("a1")
        assert child.turns == 1 and child.loop.history is not parent.history
        assert parent_history_len_before == 1  # 父历史未被子代理污染（只有 system）
        await manager.shutdown()
    asyncio.run(go())
