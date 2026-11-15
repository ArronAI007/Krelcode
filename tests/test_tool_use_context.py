"""ToolUseContext：会话/turn、权限、服务索引、取消信号。"""

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.runtime import AgentRuntime
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.tools.base import (
    PermissionMode,
    RunMode,
    RuntimeSnapshot,
    ToolContext,
    ToolResult,
)
from krelcode.tools.registry import ToolRegistry
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.write_file import WriteFileTool


class InspectTool:
    name = "inspect"
    description = "inspect context"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def __init__(self):
        self.context = None

    async def execute(self, args, ctx):
        self.context = ctx
        return ToolResult(content="inspected")


def make_loop(provider, tool, snapshot=None):
    return AgentLoop(
        llm=provider,
        registry=ToolRegistry([tool]),
        tool_context=ToolContext(
            workspace_root=Path("."),
            sandbox_enabled=False,
            runtime=snapshot or RuntimeSnapshot(),
        ),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_turn_context_carries_session_mode_services_and_descriptor():
    tool = InspectTool()
    call = ToolCall("c1", "inspect", {})
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[call]),
        "done",
    ])
    loop = make_loop(provider, tool, RuntimeSnapshot(
        session_id="old",
        run_mode=RunMode.PRINT,
        permission_mode=PermissionMode.READ_ONLY,
        features=frozenset({"streaming-tools"}),
        disabled_tools=frozenset({"write_file"}),
    ))
    runtime = AgentRuntime(loop)
    runtime.bind_session("session-42")

    async def scenario():
        query = runtime.start_query("go")
        _ = [item async for item in query.events]
        return await query.wait(), query.turn_id

    reply, turn_id = asyncio.run(scenario())

    assert reply == "done"
    ctx = tool.context
    assert ctx.runtime.session_id == "session-42"
    assert ctx.runtime.turn_id == turn_id == "turn-1"
    assert ctx.runtime.run_mode is RunMode.PRINT
    assert ctx.runtime.permission_mode is PermissionMode.READ_ONLY
    assert "streaming-tools" in ctx.runtime.features
    assert ctx.tool_descriptor is not None and ctx.tool_descriptor.read_only


class CancellableTool(InspectTool):
    name = "cancellable"

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.saw_abort = False

    async def execute(self, args, ctx):
        self.context = ctx
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.saw_abort = ctx.abort.is_cancelled
            raise
        return ToolResult(content="never")


class BlockingProvider:
    name = "blocking"
    model = "test"

    def __init__(self, tool):
        self.tool = tool

    async def stream(self, messages, tools=None):
        from krelcode.llm.base import ToolCallReady

        call = ToolCall("cancel-1", self.tool.name, {})
        yield ToolCallReady(0, call)
        await asyncio.wait_for(self.tool.started.wait(), timeout=1)
        await asyncio.sleep(30)
        yield


def test_runtime_stop_sets_cooperative_abort_signal_before_task_cancel():
    tool = CancellableTool()
    loop = make_loop(BlockingProvider(tool), tool)

    async def scenario():
        runtime = AgentRuntime(loop)
        runtime.start_query("stop me")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        assert await runtime.stop() is True
        return tool

    asyncio.run(scenario())
    assert tool.saw_abort is True


def test_session_controller_exposes_readonly_command_directory():
    loop = make_loop(MockProvider(scripted=["ok"]), InspectTool())
    controller = SessionController(loop)

    assert controller.loop.tool_context.services.has_command("/help")
    assert any(
        command.name == "/help"
        for command in controller.loop.tool_context.services.commands
    )


def test_query_tool_allowlist_restricts_schema_and_execution(tmp_path):
    call = ToolCall("w1", "write_file", {"path": "blocked.txt", "content": "x"})
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[call]),
        "blocked",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([ReadFileTool(), WriteFileTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path,
            sandbox_enabled=False,
        ),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    runtime = AgentRuntime(loop)

    async def scenario():
        query = runtime.start_query("write", tool_allowlist=["Read"])
        _ = [event async for event in query.events]
        return await query.wait()

    assert asyncio.run(scenario()) == "blocked"
    assert [spec["name"] for spec in provider.tools_seen[0]] == ["read_file"]
    assert "allowed-tools" in loop.history[3].content
    assert not (tmp_path / "blocked.txt").exists()
