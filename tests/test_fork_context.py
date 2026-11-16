"""Step 92：子代理只继承指令与筛选 Memory，不复制父对话历史。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SUBAGENT_SYSTEM_PROMPT, SubagentManager
from krelcode.subagent.tools import SpawnAgentTool, WaitAgentTool
from krelcode.tools.base import ToolContext, ToolResult


class EchoProvider:
    """无状态：回复 = 完成: <最后一条 user 消息>。并发安全。"""
    name = "echo-child"
    model = "echo"
    capabilities = ProviderCapabilities()

    async def stream(self, messages, tools=None):
        users = [m for m in messages if m.role == "user" and m.content]
        last = users[-1].content if users else "?"
        text = f"完成: {last}"
        yield StreamFinished(message=Message.assistant(text),
                             finish_reason="stop", usage={})


def make_env(tmp_path):
    """父 loop + manager（已接 get_parent_history），返回 (parent, manager)。"""
    bus = EventBus()
    manager = SubagentManager(
        llm=EchoProvider(), tool_factory=list,
        tool_context=ToolContext(workspace_root=tmp_path), bus=bus)
    parent = AgentLoop(
        llm=MockProvider(fallback="父回复"),
        registry=ToolRegistry_(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), bus=bus, system_prompt="PARENT-SYS")
    manager.get_parent_history = lambda: parent.history
    return parent, manager


def ToolRegistry_():
    from krelcode.tools.registry import ToolRegistry
    return ToolRegistry()


def seed_parent_history(parent):
    """模拟父对话进行到一半：含已完成的轮次 + 悬挂的 spawn 调用。"""
    parent.history.extend([
        Message.user("问题1"),
        Message.assistant("回答1"),
        Message.user("请帮我处理登录模块"),
        # spawn 执行时刻的真实形态：assistant(tool_calls) 悬挂，tool 结果未回填
        Message.assistant(tool_calls=[ToolCall("s1", "spawn_agent", {"task": "修复登录 bug"})]),
    ])


# -- 核心语义 ---------------------------------------------------------------------------------

def test_fork_context_never_copies_parent_history(tmp_path):
    async def go():
        parent, manager = make_env(tmp_path)
        seed_parent_history(parent)

        handle = await manager.spawn("修复登录 bug", fork_context=True)
        assert handle.forked is True
        handle = await manager.wait("a1", timeout=5)
        assert handle.status == "completed"

        child = handle.loop.history
        assert [message.role for message in child] == [
            "system", "user", "assistant"]
        assert "SUBAGENT" in child[0].content
        assert child[0].content != "PARENT-SYS"
        assert child[1].content == "修复登录 bug"
        rendered = "\n".join(str(message.content or "") for message in child)
        assert "问题1" not in rendered
        assert "请帮我处理登录模块" not in rendered
        assert handle.loop.parent_history_inherited is False

    asyncio.run(go())


def test_no_fork_by_default_starts_fresh(tmp_path):
    async def go():
        parent, manager = make_env(tmp_path)
        seed_parent_history(parent)
        handle = await manager.spawn("独立任务")
        handle = await manager.wait("a1", timeout=5)
        roles = [m.role for m in handle.loop.history]
        assert roles == ["system", "user", "assistant"]   # 全新上下文
        assert handle.forked is False
        assert handle.loop.history[1].content == "独立任务"

    asyncio.run(go())


def test_fork_context_with_complete_parent_tail_still_starts_fresh(tmp_path):
    async def go():
        parent, manager = make_env(tmp_path)
        parent.history.extend([Message.user("q"), Message.assistant("a")])
        handle = await manager.spawn("后续任务", fork_context=True)
        await manager.wait("a1", timeout=5)
        child = handle.loop.history
        assert [m.role for m in child] == ["system", "user", "assistant"]
        assert child[1].content == "后续任务"
        assert all(message.content not in {"q", "a"} for message in child)

    asyncio.run(go())


def test_child_history_is_isolated_from_parent(tmp_path):
    async def go():
        parent, manager = make_env(tmp_path)
        seed_parent_history(parent)
        handle = await manager.spawn("修复登录 bug", fork_context=True)
        await manager.wait("a1", timeout=5)

        child = handle.loop.history
        assert child[1] is not parent.history[1]
        child[1].content = "子代理侧任务"
        assert parent.history[1].content == "问题1"
        await manager.send_input("a1", "追加")
        await manager.wait("a1", timeout=5)
        assert len(parent.history) == 5

    asyncio.run(go())


def test_fork_context_is_compatibility_flag_not_history_source(tmp_path):
    async def go():
        manager = SubagentManager(
            llm=EchoProvider(), tool_factory=list,
            tool_context=ToolContext(workspace_root=tmp_path), bus=EventBus())
        handle = await manager.spawn("任务", fork_context=True)
        assert handle.forked is True
        await manager.wait("a1", timeout=5)
        assert [m.role for m in handle.loop.history] == ["system", "user", "assistant"]
        assert handle.loop.history[1].content == "任务"

    asyncio.run(go())


# -- 工具表面 -----------------------------------------------------------------------------------

def test_spawn_tool_fork_context_param(tmp_path):
    async def go():
        parent, manager = make_env(tmp_path)
        seed_parent_history(parent)
        spawn = SpawnAgentTool(manager)
        r = await spawn.execute(
            {"task": "修复登录 bug", "fork_context": True},
            ToolContext(workspace_root=tmp_path))
        assert not r.is_error
        assert r.data["forked"] is True
        assert "已继承指令与筛选 Memory" in r.content
        assert "不含完整历史" in r.content

        wait = WaitAgentTool(manager)
        r2 = await wait.execute({"agent_id": "a1", "timeout": 5},
                                ToolContext(workspace_root=tmp_path))
        assert "[a1] completed" in r2.content
        assert "委派的子任务" not in r2.content
        assert "修复登录 bug" in r2.content

        assert any("继承指令/Memory" in line for line in manager.status_lines())
        await manager.shutdown()

    asyncio.run(go())


def test_spawn_tool_schema_declares_fork_context():
    schema_props = SpawnAgentTool.parameters["properties"]
    assert "fork_context" in schema_props and "isolated" in schema_props
