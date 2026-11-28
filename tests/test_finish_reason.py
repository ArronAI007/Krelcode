"""Provider finish_reason 终态策略：截断、协议不一致、安全终止。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.turn.loop_state import AgentLoopTerminated
from krelcode.core.events import ErrorOccurred
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import (
    FINISH_LENGTH,
    FINISH_TOOLS,
    ProviderCapabilities,
    StreamFinished,
    TextDelta,
    ToolCallReady,
)
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class FinishProvider:
    name = "finish-test"
    model = "test"
    capabilities = ProviderCapabilities(supports_tools=True, supports_streaming=True)

    def __init__(self, events):
        self.events = events

    async def stream(self, messages, tools=None):
        for event in self.events:
            yield event


def make_loop(events, tools=None):
    return AgentLoop(
        llm=FinishProvider(events),
        registry=ToolRegistry(tools or []),
        tool_context=ToolContext(
            workspace_root=Path("."), sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_length_finish_is_visible_failure_not_success():
    loop = make_loop([
        TextDelta("半句"),
        StreamFinished(Message.assistant("半句"), FINISH_LENGTH),
    ])
    with pytest.raises(AgentLoopTerminated) as caught:
        asyncio.run(loop.run("long answer"))
    reply = str(caught.value)

    assert "输出长度上限" in reply
    assert "max_tokens" in reply
    assert reply in (loop.history[-1].content or "")


def test_finish_tools_without_calls_is_protocol_failure():
    loop = make_loop([
        StreamFinished(Message.assistant("no call"), FINISH_TOOLS),
    ])
    with pytest.raises(AgentLoopTerminated) as caught:
        asyncio.run(loop.run("tool call"))
    reply = str(caught.value)

    assert "协议不一致" in reply
    assert reply in (loop.history[-1].content or "")


def test_unknown_or_safety_finish_reason_is_not_silent():
    loop = make_loop([
        StreamFinished(Message.assistant("blocked"), "safety"),
    ])
    with pytest.raises(AgentLoopTerminated) as caught:
        asyncio.run(loop.run("unsafe"))
    reply = str(caught.value)

    assert "非正常原因" in reply
    assert "safety" in reply


class EchoTool:
    name = "echo"
    description = "echo"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def execute(self, args, ctx):
        return ToolResult(content=args["value"])


def test_length_with_tool_call_keeps_history_paired_and_reports_failure():
    call = ToolCall("c1", "echo", {"value": "ok"})
    loop = make_loop([
        ToolCallReady(0, call),
        StreamFinished(
            Message.assistant(tool_calls=[call]),
            FINISH_LENGTH,
        ),
    ], tools=[EchoTool()])

    with pytest.raises(AgentLoopTerminated) as caught:
        asyncio.run(loop.run("run tool"))
    reply = str(caught.value)
    assert "输出长度上限" in reply
    assert [message.role for message in loop.history] == [
        "system", "user", "assistant", "tool", "assistant"]
    assert reply in (loop.history[-1].content or "")
    assert loop.history[3].content == "ok"
