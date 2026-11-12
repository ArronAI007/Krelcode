"""Step 42 测试：子代理 fork_context（继承父对话上下文）。"""

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

def test_fork_copies_history_sanitizes_and_merges_pending_user(tmp_path):
    async def go():
        parent, manager = make_env(tmp_path)
        seed_parent_history(parent)

        handle = await manager.spawn("修复登录 bug", fork_context=True)
        assert handle.forked is True
        handle = await manager.wait("a1", timeout=5)
        assert handle.status == "completed"

        child = handle.loop.history
        # 结构：子代理 system + 父历史（去 system、消毒、弹出未答 user）+ 合并任务 + 回复
        assert child[0].role == "system"
        assert "SUBAGENT" in child[0].content            # 子代理自己的 system，不是父的
        assert child[0].content != "PARENT-SYS"
        assert child[1] == Message.user("问题1")          # 已完成轮次保留
        assert child[2] == Message.assistant("回答1")
        # 悬挂的 assistant(tool_calls) 被消毒；未答 user 与子任务合并
        assert not any(m.tool_calls for m in child)
        merged = child[3]
        assert merged.role == "user"
        assert "请帮我处理登录模块" in merged.content
        assert "[委派的子任务] 修复登录 bug" in merged.content
        assert child[4].role == "assistant" and child[4].content.startswith("完成:")
        # 角色严格交替（Anthropic 约束）
        roles = [m.role for m in child]
        assert roles == ["system", "user", "assistant", "user", "assistant"]

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


def test_fork_without_pending_user_tail(tmp_path):
    """父历史以完整 assistant 回复结尾（无悬挂）：不产生合并，任务独立成 user 消息。"""
    async def go():
        parent, manager = make_env(tmp_path)
        parent.history.extend([Message.user("q"), Message.assistant("a")])
        handle = await manager.spawn("后续任务", fork_context=True)
        await manager.wait("a1", timeout=5)
        child = handle.loop.history
        assert [m.role for m in child] == ["system", "user", "assistant", "user", "assistant"]
        assert child[3].content == "后续任务"              # 无需合并

    asyncio.run(go())


def test_fork_is_deep_copy_isolated_from_parent(tmp_path):
    async def go():
        parent, manager = make_env(tmp_path)
        seed_parent_history(parent)
        handle = await manager.spawn("修复登录 bug", fork_context=True)
        await manager.wait("a1", timeout=5)

        child = handle.loop.history
        # 子代理消息是独立对象：改写不影响父历史
        assert child[1] is not parent.history[1]
        child[1].content = "子代理侧篡改"
        assert parent.history[1].content == "问题1"
        # 子代理继续对话也不影响父
        await manager.send_input("a1", "追加")
        await manager.wait("a1", timeout=5)
        assert len(parent.history) == 5                    # 父历史原样

    asyncio.run(go())


def test_fork_without_history_source_is_noop(tmp_path):
    """未注入 get_parent_history（如独立使用 manager）→ fork_context 静默退化为全新上下文。"""
    async def go():
        manager = SubagentManager(
            llm=EchoProvider(), tool_factory=list,
            tool_context=ToolContext(workspace_root=tmp_path), bus=EventBus())
        handle = await manager.spawn("任务", fork_context=True)
        assert handle.forked is False                      # 无源不 fork
        await manager.wait("a1", timeout=5)
        assert [m.role for m in handle.loop.history] == ["system", "user", "assistant"]

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
        assert "已继承当前对话上下文" in r.content

        wait = WaitAgentTool(manager)
        r2 = await wait.execute({"agent_id": "a1", "timeout": 5},
                                ToolContext(workspace_root=tmp_path))
        assert "[a1] completed" in r2.content
        # 子代理确实看到了父上下文（回复引用合并后的任务文本）
        assert "委派的子任务" in r2.content

        # 状态行标注继承
        assert any("继承上下文" in line for line in manager.status_lines())
        await manager.shutdown()

    asyncio.run(go())


def test_spawn_tool_schema_declares_fork_context():
    schema_props = SpawnAgentTool.parameters["properties"]
    assert "fork_context" in schema_props and "isolated" in schema_props
