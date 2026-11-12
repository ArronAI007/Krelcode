"""Step 43 测试：子代理上下文管理缺口修复 + /model 热切换同步子代理。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.compression import DEFAULT_MAX_TOKENS
from krelcode.agent.loop import AgentLoop
from krelcode.app import build_loop, build_model_switcher, build_parser
from krelcode.agent.usage import UsageTracker
from krelcode.core.events import EventBus
from krelcode.core.messages import Message
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import ToolContext


class EchoProvider:
    name = "echo-child"
    model = "echo"
    capabilities = ProviderCapabilities()

    async def stream(self, messages, tools=None):
        yield StreamFinished(message=Message.assistant("done"),
                             finish_reason="stop", usage={})


def make_manager(tmp_path, context_budget=None, llm=None):
    return SubagentManager(
        llm=llm or EchoProvider(), tool_factory=list,
        tool_context=ToolContext(workspace_root=tmp_path),
        bus=EventBus(), context_budget=context_budget)


# -- 子代理上下文管理 -------------------------------------------------------------------------

def test_child_gets_context_manager_when_budget_set(tmp_path):
    async def go():
        mgr = make_manager(tmp_path, context_budget=12345)
        handle = await mgr.spawn("长任务")
        await mgr.wait("a1", timeout=5)
        cm = handle.loop.context_manager
        assert cm is not None and cm.max_tokens == 12345
        await mgr.shutdown()
    asyncio.run(go())


def test_child_no_context_manager_by_default(tmp_path):
    async def go():
        mgr = make_manager(tmp_path)          # 未配置预算 → 向后兼容
        handle = await mgr.spawn("任务")
        await mgr.wait("a1", timeout=5)
        assert handle.loop.context_manager is None
        await mgr.shutdown()
    asyncio.run(go())


def test_child_context_manager_uses_spawn_time_llm(tmp_path):
    """/model 热切换后新 spawn 的子代理压缩器绑定新 llm。"""
    async def go():
        old_llm = EchoProvider()
        mgr = make_manager(tmp_path, context_budget=1000, llm=old_llm)
        new_llm = MockProvider(fallback="new")
        mgr.llm = new_llm                      # 模拟 switcher 更新
        handle = await mgr.spawn("任务")
        await mgr.wait("a1", timeout=5)
        assert handle.loop.llm is new_llm
        assert handle.loop.context_manager.llm is new_llm
        await mgr.shutdown()
    asyncio.run(go())


def test_build_loop_wires_budget_into_manager(tmp_path):
    """app 组装：mock（未知模型）→ 子代理预算 = 兜底默认。"""
    args = build_parser().parse_args(
        ["--mock", "--workspace", str(tmp_path), "--no-context"])
    loop, manager = asyncio.run(build_loop(args, {}))
    assert manager.context_budget == DEFAULT_MAX_TOKENS
    # 显式 --context-tokens 同样传导
    args2 = build_parser().parse_args(
        ["--mock", "--workspace", str(tmp_path), "--no-context",
         "--context-tokens", "4321"])
    _, manager2 = asyncio.run(build_loop(args2, {}))
    assert manager2.context_budget == 4321
    # --no-compress → 子代理也不启用
    args3 = build_parser().parse_args(
        ["--mock", "--workspace", str(tmp_path), "--no-context", "--no-compress"])
    _, manager3 = asyncio.run(build_loop(args3, {}))
    assert manager3.context_budget is None


# -- /model 热切换同步子代理 ---------------------------------------------------------------------

def test_model_switch_updates_subagent_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    args = build_parser().parse_args(["--mock", "--workspace", str(tmp_path)])
    manager = make_manager(tmp_path)
    loop = AgentLoop(
        llm=MockProvider(), registry=__import__("krelcode.tools.registry", fromlist=["ToolRegistry"]).ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), system_prompt="SYS")
    usage = UsageTracker(model="mock-model")
    switch = build_model_switcher(args, {}, loop, usage, subagents=manager)

    msg = switch("openai/gpt-4o-mini")
    assert "已切换" in msg
    assert loop.llm.model == "gpt-4o-mini"
    # 子代理 manager 的 llm 同步更新——之后 spawn 的子代理用新模型
    assert manager.llm is loop.llm
