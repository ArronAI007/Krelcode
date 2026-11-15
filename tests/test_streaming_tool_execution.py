"""流式工具执行：Ready 后提前执行、只读并发、写 barrier、流失败取消。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.errors import LLMError
from krelcode.core.events import EventBus, ToolCallCompleted
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import (
    FINISH_STOP,
    FINISH_TOOLS,
    ProviderCapabilities,
    StreamFinished,
    TextDelta,
    ToolCallReady,
)
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


CTX = ToolContext(workspace_root=Path("."), sandbox_enabled=False)


class StreamingToolProvider:
    capabilities = ProviderCapabilities(supports_tools=True, supports_streaming=True)
    name = "streaming-test"
    model = "test"

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    async def stream(self, messages, tools=None):
        round_factory = self.rounds[self.calls]
        self.calls += 1
        async for event in round_factory():
            yield event


class EarlyTool:
    name = "early"
    description = "record early execution"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def __init__(self):
        self.started = asyncio.Event()
        self.completed = asyncio.Event()
        self.cancelled = False

    async def execute(self, args, ctx):
        self.started.set()
        self.completed.set()
        return ToolResult(content="early ok")


def make_loop(provider, tools):
    return AgentLoop(
        llm=provider,
        registry=ToolRegistry(tools),
        tool_context=CTX,
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_tool_executes_before_model_stream_finishes():
    tool = EarlyTool()
    call = ToolCall("early-1", "early", {})
    observed_early = False

    async def first_round():
        nonlocal observed_early
        yield ToolCallReady(0, call)
        await asyncio.wait_for(tool.completed.wait(), timeout=0.5)
        observed_early = True
        yield StreamFinished(
            Message.assistant(tool_calls=[call]), FINISH_TOOLS)

    async def second_round():
        yield TextDelta("完成")
        yield StreamFinished(Message.assistant("完成"), FINISH_STOP)

    loop = make_loop(StreamingToolProvider([first_round, second_round]), [tool])
    assert asyncio.run(loop.run("test")) == "完成"
    assert observed_early is True
    assert loop.history[3].content == "early ok"


class BarrierReadTool:
    def __init__(self, name, state):
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}
        self.read_only = True
        self.state = state

    async def execute(self, args, ctx):
        self.state[self.name].set()
        await asyncio.wait_for(
            asyncio.gather(
                self.state["read-a"].wait(),
                self.state["read-b"].wait(),
            ),
            timeout=0.5,
        )
        return ToolResult(content=self.name)


def test_ready_readonly_tools_execute_concurrently_before_stream_finishes():
    state = {"read-a": asyncio.Event(), "read-b": asyncio.Event()}
    tools = [BarrierReadTool("read-a", state), BarrierReadTool("read-b", state)]
    call_a = ToolCall("a", "read-a", {})
    call_b = ToolCall("b", "read-b", {})
    calls = [call_a, call_b]

    async def first_round():
        yield ToolCallReady(0, call_a)
        yield ToolCallReady(1, call_b)
        await asyncio.wait_for(
            asyncio.gather(state["read-a"].wait(), state["read-b"].wait()),
            timeout=0.5,
        )
        yield StreamFinished(Message.assistant(tool_calls=calls), FINISH_TOOLS)

    async def second_round():
        yield StreamFinished(Message.assistant("done"), FINISH_STOP)

    loop = make_loop(StreamingToolProvider([first_round, second_round]), tools)
    assert asyncio.run(loop.run("parallel")) == "done"
    assert [m.content for m in loop.history if m.role == "tool"] == [
        "read-a", "read-b"]


class CancellableTool(EarlyTool):
    name = "cancellable"

    async def execute(self, args, ctx):
        self.started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult(content="never")


def test_stream_failure_cancels_early_tools_and_does_not_append_results():
    tool = CancellableTool()
    call = ToolCall("cancel-1", "cancellable", {})
    bus = EventBus()
    completed = []
    bus.subscribe(lambda event: completed.append(event)
                  if isinstance(event, ToolCallCompleted) else None)

    async def broken_round():
        yield ToolCallReady(0, call)
        await asyncio.wait_for(tool.started.wait(), timeout=0.5)
        raise LLMError("stream broke")

    loop = AgentLoop(
        llm=StreamingToolProvider([broken_round]),
        registry=ToolRegistry([tool]),
        tool_context=CTX,
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
    )

    with pytest.raises(LLMError, match="stream broke"):
        asyncio.run(loop.run("fail"))
    assert tool.cancelled is True
    assert completed == []
    assert all(message.role != "tool" for message in loop.history)
