"""Step 17 测试：UsageTracker（单元 + MockProvider 集成）。"""

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.usage import UsageTracker
from krelcode.core.events import EventBus, LLMResponseReceived, RunStarted
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def test_tracker_accumulates_and_resets_per_turn():
    bus = EventBus()
    tracker = UsageTracker()
    tracker.attach(bus)

    async def go():
        await bus.emit(RunStarted("第一轮"))
        await bus.emit(LLMResponseReceived(
            Message.assistant("a"), {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}))
        await bus.emit(LLMResponseReceived(
            Message.assistant("b"), {"input_tokens": 20, "output_tokens": 7, "total_tokens": 27}))
        assert tracker.turn.calls == 2 and tracker.turn.total == 42
        await bus.emit(RunStarted("第二轮"))
        assert tracker.turn.calls == 0                 # 新一轮清零
        await bus.emit(LLMResponseReceived(
            Message.assistant("c"), {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}))

    asyncio.run(go())
    assert tracker.session.calls == 3 and tracker.session.total == 44
    assert tracker.turn.total == 2
    assert "3 次" in tracker.summary() and "本轮" in tracker.summary()


def test_tracker_tolerates_missing_usage_keys():
    bus = EventBus()
    tracker = UsageTracker()
    tracker.attach(bus)
    asyncio.run(bus.emit(LLMResponseReceived(Message.assistant("x"), {})))
    assert tracker.session.calls == 1 and tracker.session.total == 0


def test_integration_with_loop_and_mock_usage(tmp_path):
    """provider 上报 usage → LLMResponseReceived → tracker 累计（全链路）。"""
    provider = MockProvider(
        scripted=["你好", "第二条回复"],
        usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
    )
    bus = EventBus()
    tracker = UsageTracker()
    tracker.attach(bus)
    loop = AgentLoop(
        llm=provider, registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), bus=bus, system_prompt="SYS",
    )
    asyncio.run(loop.run("hi"))
    asyncio.run(loop.run("again"))
    assert tracker.session.calls == 2
    assert tracker.session.input == 10 and tracker.session.output == 6
    assert tracker.turn.calls == 1  # 只算最近一轮
