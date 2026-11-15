"""AgentRuntime：统一 turn、steering、取消和资源释放边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.runtime import AgentRuntime
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.time_tool import GetTimeTool
from krelcode.tools.registry import ToolRegistry


def make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(scripted=["完成"]),
        registry=ToolRegistry([GetTimeTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_run_once_uses_agent_loop(tmp_path):
    runtime = AgentRuntime(make_loop(tmp_path))
    assert asyncio.run(runtime.run_once("hello")) == "完成"
    assert runtime.loop.history[1].content == "hello"


def test_background_turn_steering_and_stop(tmp_path):
    loop = make_loop(tmp_path)
    started = asyncio.Event()

    async def slow_run(message, images=None):
        started.set()
        await asyncio.sleep(30)
        return message

    loop.run = slow_run

    async def scenario():
        runtime = AgentRuntime(loop)
        task = runtime.start_turn("任务")
        await started.wait()
        assert runtime.active_task is task
        assert runtime.is_running is True
        assert runtime.steer("补充") == "补充"
        assert runtime.drain_steering() == ["补充"]
        assert await runtime.stop() is True
        assert runtime.is_running is False

    asyncio.run(scenario())


def test_close_releases_subagents_and_mcp_once(tmp_path):
    class Closable:
        def __init__(self, name):
            self.name = name
            self.calls = 0

        async def shutdown(self):
            self.calls += 1

        async def close_all(self):
            self.calls += 1

    subagents = Closable("subagents")
    supervisor = Closable("mcp")
    runtime = AgentRuntime(
        make_loop(tmp_path),
        subagents=subagents,
        mcp_supervisor=supervisor,
    )

    async def scenario():
        await runtime.close()
        await runtime.close()

    asyncio.run(scenario())
    assert subagents.calls == 1
    assert supervisor.calls == 1
