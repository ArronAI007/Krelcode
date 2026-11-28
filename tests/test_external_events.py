"""Generic external event resume adapter and waiting tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.external_events import LocalExternalEventBroker
from krelcode.contracts.external_event import ExternalEvent
from krelcode.contracts.tool import ToolContext, ToolServices
from krelcode.core.events import LoopPhaseChanged
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.builtin.external_event import ExternalEventWaitTool
from krelcode.tools.registry import ToolRegistry


def test_external_event_broker_resumes_waiter() -> None:
    async def go() -> None:
        broker = LocalExternalEventBroker()
        waiter = asyncio.create_task(broker.wait(
            "resume-1",
            event_type="webhook",
            timeout=1,
        ))
        await asyncio.sleep(0)
        delivered = await broker.publish(ExternalEvent(
            event_id="e1",
            event_type="webhook",
            resume_token="resume-1",
            payload={"ok": True},
        ))
        event = await waiter
        assert delivered == 1
        assert event is not None
        assert event.payload["ok"] is True
        broker.close()

    asyncio.run(go())


def test_external_event_buffers_before_waiter_and_tool_returns_payload(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        broker = LocalExternalEventBroker()
        await broker.publish(ExternalEvent(
            event_id="e2",
            event_type="queue",
            resume_token="resume-2",
            payload={"value": 42},
        ))
        tool = ExternalEventWaitTool()
        result = await tool.execute(
            {"resume_token": "resume-2", "timeout": 1},
            ToolContext(
                workspace_root=tmp_path,
                services=ToolServices(external_events=broker),
            ),
        )
        assert not result.is_error
        assert '"value": 42' in result.content
        assert result.data["event_type"] == "queue"
        broker.close()

    asyncio.run(go())


def test_loop_waiting_event_resumes_after_external_publication(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        broker = LocalExternalEventBroker()
        loop = AgentLoop(
            llm=MockProvider(scripted=[
                Message.assistant(tool_calls=[
                    ToolCall("e1", "wait_external_event", {
                        "resume_token": "resume-loop",
                        "timeout": 2,
                    }),
                ]),
                "resumed",
            ]),
            registry=ToolRegistry([ExternalEventWaitTool()]),
            tool_context=ToolContext(
                workspace_root=tmp_path,
                sandbox_enabled=False,
                services=ToolServices(external_events=broker),
            ),
            policy=AllowAllPolicy(),
            external_events=broker,
            system_prompt="SYS",
        )
        waiting = asyncio.Event()

        def observe(event) -> None:
            if (
                isinstance(event, LoopPhaseChanged)
                and event.phase == "waiting"
                and event.transition["waiting"]["kind"] == "external_event"
            ):
                waiting.set()

        loop.bus.subscribe(observe)
        task = asyncio.create_task(loop.run("wait"))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        await broker.publish(ExternalEvent(
            event_id="e-loop",
            event_type="webhook",
            resume_token="resume-loop",
            payload={"ready": True},
        ))
        assert await asyncio.wait_for(task, timeout=2) == "resumed"
        broker.close()

    asyncio.run(go())
