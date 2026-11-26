"""ToolIntent and execution runtime boundaries below AgentLoop."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Mapping, Optional

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.contracts.tool import (
    ToolContext,
    ToolDescriptor,
    ToolExecutionRuntime,
    ToolResult,
    ToolSource,
)
from krelcode.contracts.tool_execution import (
    ToolExecutionResult,
    ToolIntent,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.registry import ToolRegistry
from krelcode.tools.builtin.run_command import RunCommandTool
from krelcode.tools.runtime import (
    ExecutionRuntimeRouter,
    LocalToolRuntime,
    MCPRuntime,
    RemoteRuntime,
    SandboxRuntime,
)


class EchoTool:
    name = "echo"
    description = "echo input"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    read_only = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def execute(self, args: Mapping[str, object], ctx: ToolContext) -> ToolResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("runtime boom")
        return ToolResult(content=f"echo: {args['text']}")


class Resolver:
    def __init__(self, tool: object | None) -> None:
        self.tool = tool

    def get_tool(self, name: str) -> object | None:
        if self.tool is not None and getattr(self.tool, "name", "") == name:
            return self.tool
        return None


class RecordingRuntime:
    def __init__(self) -> None:
        self.intents: list[ToolIntent] = []

    def supports(self, intent: ToolIntent) -> bool:
        return True

    async def execute(self, intent: ToolIntent) -> ToolExecutionResult:
        self.intents.append(intent)
        return ToolExecutionResult(
            content="standardized result",
            runtime=intent.runtime,
            diagnostics={"source": "recording"},
        )


def intent(
    *,
    descriptor: Optional[ToolDescriptor] = None,
) -> ToolIntent:
    resolved = descriptor or ToolDescriptor()
    return ToolIntent(
        call=ToolCall("call-1", "echo", {"text": "hi"}),
        descriptor=resolved,
        parameters=EchoTool.parameters,
        context=ToolContext(
            workspace_root=Path("."), sandbox_enabled=False),
    )


def test_tool_descriptor_resolves_execution_runtime_tiers() -> None:
    assert ToolDescriptor().resolved_execution_runtime is (
        ToolExecutionRuntime.LOCAL)
    assert ToolDescriptor(
        source=ToolSource.MCP,
    ).resolved_execution_runtime is ToolExecutionRuntime.MCP
    assert ToolDescriptor(
        source=ToolSource.SUBAGENT,
    ).resolved_execution_runtime is ToolExecutionRuntime.REMOTE
    assert ToolDescriptor(
        execution_runtime=ToolExecutionRuntime.SANDBOX,
    ).resolved_execution_runtime is ToolExecutionRuntime.SANDBOX
    assert ToolDescriptor.from_tool(
        RunCommandTool(),
    ).resolved_execution_runtime is ToolExecutionRuntime.SANDBOX


def test_execution_router_selects_the_declared_runtime() -> None:
    calls: list[ToolExecutionRuntime] = []

    class Probe:
        def __init__(self, runtime: ToolExecutionRuntime) -> None:
            self.runtime = runtime

        def supports(self, value: ToolIntent) -> bool:
            return value.runtime is self.runtime

        async def execute(self, value: ToolIntent) -> ToolExecutionResult:
            calls.append(self.runtime)
            return ToolExecutionResult(
                content=self.runtime.value,
                runtime=self.runtime,
            )

    router = ExecutionRuntimeRouter(
        Resolver(EchoTool()),
        runtimes={
            kind: Probe(kind)
            for kind in ToolExecutionRuntime
        },
    )

    for kind in ToolExecutionRuntime:
        result = asyncio.run(router.execute(intent(
            descriptor=ToolDescriptor(execution_runtime=kind))))
        assert result.runtime is kind
    assert calls == list(ToolExecutionRuntime)


@pytest.mark.parametrize("runtime, descriptor", [
    (
        LocalToolRuntime,
        ToolDescriptor(),
    ),
    (
        MCPRuntime,
        ToolDescriptor(
            source=ToolSource.MCP, mcp_server="docs"),
    ),
    (
        SandboxRuntime,
        ToolDescriptor(
            execution_runtime=ToolExecutionRuntime.SANDBOX),
    ),
    (
        RemoteRuntime,
        ToolDescriptor(source=ToolSource.SUBAGENT),
    ),
])
def test_concrete_runtimes_return_standard_results(
    runtime: type[LocalToolRuntime],
    descriptor: ToolDescriptor,
) -> None:
    tool = EchoTool()
    result = asyncio.run(runtime(Resolver(tool)).execute(
        intent(descriptor=descriptor)))

    assert result.content == "echo: hi"
    assert not result.is_error
    assert result.runtime is descriptor.resolved_execution_runtime
    assert result.to_tool_result().content == "echo: hi"
    assert tool.calls == 1


def test_local_runtime_normalizes_execution_exceptions() -> None:
    result = asyncio.run(LocalToolRuntime(
        Resolver(EchoTool(fail=True))).execute(intent()))

    assert result.is_error
    assert "runtime boom" in result.content
    assert result.diagnostics["error_kind"] == "runtime_exception"
    assert result.diagnostics["exception_type"] == "RuntimeError"


def test_agent_loop_only_consumes_execution_results(tmp_path: Path) -> None:
    tool = EchoTool()
    registry = ToolRegistry([tool])
    recording = RecordingRuntime()

    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("call-1", "echo", {"text": "hi"}),
            ]),
            "done",
        ]),
        registry=registry,
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        execution_runtime=recording,
    )

    assert asyncio.run(loop.run("execute")) == "done"
    assert tool.calls == 0
    assert len(recording.intents) == 1
    assert isinstance(recording.intents[0], ToolIntent)
    assert recording.intents[0].descriptor == registry.descriptor("echo")
    assert loop.history[3].content == "standardized result"


def test_agent_validation_runs_before_execution_runtime(tmp_path: Path) -> None:
    tool = EchoTool()
    recording = RecordingRuntime()
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("bad", "echo", {"text": 42}),
            ]),
            "recovered",
        ]),
        registry=ToolRegistry([tool]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        execution_runtime=recording,
    )

    assert asyncio.run(loop.run("validate")) == "recovered"
    assert recording.intents == []
    assert "工具参数校验失败" in loop.history[3].content
