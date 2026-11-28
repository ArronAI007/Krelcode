"""QueryEngine：turn 身份、事件顺序、异常和取消传播。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEngine
from krelcode.core.errors import LLMError
from krelcode.core.events import (
    LLMStreamDelta,
    RunCancelled,
    RunFailed,
    RunFinished,
    RunStarted,
)
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.base import StreamFinished, TextDelta
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def make_loop(provider, tmp_path):
    return AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_query_engine_emits_ordered_envelopes_and_result(tmp_path):
    loop = make_loop(MockProvider(scripted=["完成"]), tmp_path)
    engine = QueryEngine(loop)

    async def go():
        session = engine.start("执行")
        events = [item async for item in session.events]
        return session, events, await session.wait()

    session, events, result = asyncio.run(go())

    assert result == "完成"
    assert session.turn_id == "turn-1"
    assert [item.sequence for item in events] == list(range(len(events)))
    assert all(item.turn_id == "turn-1" for item in events)
    assert isinstance(events[0].event, RunStarted)
    assert isinstance(events[-1].event, RunFinished)
    assert events[-1].event.final_text == "完成"


def test_query_engine_propagates_provider_errors(tmp_path):
    class BoomProvider(MockProvider):
        async def stream(self, messages, tools=None):
            raise LLMError("boom")
            yield  # pragma: no cover

    loop = make_loop(BoomProvider(), tmp_path)

    async def go():
        session = QueryEngine(loop).start("失败")
        with pytest.raises(LLMError, match="boom"):
            async for _ in session.events:
                pass

    asyncio.run(go())


def test_query_engine_cancel_emits_run_cancelled(tmp_path):
    class SlowProvider(MockProvider):
        async def stream(self, messages, tools=None):
            await asyncio.sleep(10)
            yield  # pragma: no cover

    loop = make_loop(SlowProvider(), tmp_path)

    async def go():
        session = QueryEngine(loop).start("停止")
        consumed = []

        async def collect():
            async for item in session.events:
                consumed.append(item.event)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.02)
        await session.cancel()
        await task
        return consumed

    events = asyncio.run(go())

    assert any(isinstance(event, RunCancelled) for event in events)


def test_query_engine_applies_backpressure_to_fast_providers(tmp_path):
    class BurstProvider(MockProvider):
        def __init__(self):
            super().__init__()
            self.emitted = 0

        async def stream(self, messages, tools=None):
            text = ""
            for index in range(80):
                self.emitted += 1
                chunk = str(index % 10)
                text += chunk
                yield TextDelta(chunk)
            yield StreamFinished(
                message=Message.assistant(text),
                finish_reason="stop",
                usage={},
            )

    provider = BurstProvider()
    loop = make_loop(provider, tmp_path)

    async def go():
        session = QueryEngine(loop, queue_size=2).start("突发输出")
        stream = session.events
        first = await anext(stream)
        await asyncio.sleep(0.02)
        # renderer 停顿时，有界 QueryEvent 队列应阻止 provider 一次性跑完。
        assert provider.emitted < 80
        rest = [item async for item in stream]
        return first, [first, *rest], await session.wait()

    first, events, result = asyncio.run(go())
    assert isinstance(first.event, RunStarted)
    assert result == "0123456789" * 8
    assert [item.sequence for item in events] == list(range(len(events)))
    assert isinstance(events[-1].event, RunFinished)


def test_query_engine_drains_events_before_propagating_error(tmp_path):
    class PartialBoomProvider(MockProvider):
        async def stream(self, messages, tools=None):
            yield TextDelta("已输出")
            raise LLMError("after partial")

    loop = make_loop(PartialBoomProvider(), tmp_path)

    async def go():
        session = QueryEngine(loop, queue_size=2).start("部分输出")
        consumed = []
        with pytest.raises(LLMError, match="after partial"):
            async for item in session.events:
                consumed.append(item.event)
        return consumed

    consumed = asyncio.run(go())
    assert consumed[0] == RunStarted("部分输出", turn_id="turn-1")
    assert any(
        isinstance(event, LLMStreamDelta) and event.text == "已输出"
        for event in consumed
    )
    assert isinstance(consumed[-1], RunFailed)
