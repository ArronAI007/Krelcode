"""Background compact scheduler safety and commit tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import List

from krelcode.agent.context.compact_scheduler import BackgroundCompactScheduler
from krelcode.core.events import ContextCompressed, EventBus, RunStarted
from krelcode.core.messages import Message


class FakeCompactManager:
    high_watermark_tokens = 100

    def __init__(self) -> None:
        self.last_compact_result = None
        self.calls = 0

    async def maybe_compress(
        self,
        history: List[Message],
        force: bool = False,
        *,
        current_turn_id: str = "",
        microcompact: bool = True,
    ) -> bool:
        self.calls += 1
        history[:] = [Message.user("[compact] summary")]
        self.last_compact_result = SimpleNamespace(
            before_tokens=120,
            after_tokens=10,
            high_watermark_tokens=100,
            low_watermark_tokens=50,
            source_savings=(("history", 110),),
            summary=SimpleNamespace(source="model", generation=2),
        )
        return True


def test_background_compact_commits_unchanged_idle_history():
    async def scenario() -> None:
        history = [Message.user("a"), Message.assistant("b")]
        manager = FakeCompactManager()
        bus = EventBus()
        events = []
        bus.subscribe(events.append)
        scheduler = BackgroundCompactScheduler(
            history,
            manager,
            bus=bus,
            trigger_ratio=0.01,
        )
        assert scheduler.schedule(current_turn_id="turn-1")
        report = await scheduler.wait()
        assert report is not None and report.committed
        assert history == [Message.user("[compact] summary")]
        assert any(isinstance(event, ContextCompressed) for event in events)
        await scheduler.close()

    asyncio.run(scenario())


def test_background_compact_discards_when_history_changes():
    async def scenario() -> None:
        gate = asyncio.Event()

        class SlowManager(FakeCompactManager):
            async def maybe_compress(
                self,
                history: List[Message],
                force: bool = False,
                *,
                current_turn_id: str = "",
                microcompact: bool = True,
            ) -> bool:
                await gate.wait()
                return await super().maybe_compress(
                    history,
                    current_turn_id=current_turn_id,
                    microcompact=microcompact,
                )

        history = [Message.user("a"), Message.assistant("b")]
        manager = SlowManager()
        scheduler = BackgroundCompactScheduler(
            history,
            manager,
            trigger_ratio=0.01,
        )
        assert scheduler.schedule()
        history.append(Message.user("new turn"))
        gate.set()
        report = await scheduler.wait()
        assert report is not None
        assert not report.committed
        assert report.discarded_reason == "history_changed"
        assert history[-1] == Message.user("new turn")
        await scheduler.close()

    asyncio.run(scenario())


def test_run_started_cancels_background_compact():
    async def scenario() -> None:
        history = [Message.user("a"), Message.assistant("b")]
        manager = FakeCompactManager()
        scheduler = BackgroundCompactScheduler(
            history,
            manager,
            trigger_ratio=0.01,
        )
        scheduler.schedule()
        await scheduler.on_event(RunStarted(
            "next",
            session_id="s",
            turn_id="turn-2",
        ))
        report = await scheduler.wait()
        assert report is None
        await scheduler.close()

    asyncio.run(scenario())
