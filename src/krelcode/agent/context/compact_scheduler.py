"""Background structured compaction with revision-safe commit."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Protocol

from ...core.events import (
    AgentEvent,
    ContextCompressed,
    EventBus,
    RunFinished,
    RunStarted,
)
from ...core.messages import Message
from ...core.tokens import estimate_history_tokens
from .context_plan import history_digest


class CompactManagerPort(Protocol):
    @property
    def high_watermark_tokens(self) -> int: ...

    @property
    def last_compact_result(self) -> object: ...

    async def maybe_compress(
        self,
        history: List[Message],
        force: bool = False,
        *,
        current_turn_id: str = "",
        microcompact: bool = True,
    ) -> bool: ...


@dataclass(frozen=True)
class CompactScheduleReport:
    scheduled: bool
    committed: bool = False
    discarded_reason: str = ""
    before_tokens: int = 0
    after_tokens: int = 0


class BackgroundCompactScheduler:
    """Precompact an idle history snapshot without racing the foreground turn."""

    def __init__(
        self,
        history: List[Message],
        manager: CompactManagerPort,
        *,
        history_provider: Optional[Callable[[], List[Message]]] = None,
        bus: Optional[EventBus] = None,
        trigger_ratio: float = 0.72,
        on_committed: Optional[
            Callable[[object, tuple[Message, ...], tuple[Message, ...]], Awaitable[None]]
        ] = None,
    ) -> None:
        self.history = history
        self._history_provider = history_provider
        self.manager = manager
        self.bus = bus
        self.trigger_ratio = min(0.99, max(0.1, float(trigger_ratio)))
        self.on_committed = on_committed
        self._running_turn = False
        self._task: Optional[asyncio.Task[CompactScheduleReport]] = None
        self._last_attempt_digest = ""
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._closed = False

    def attach(self, bus: EventBus) -> Callable[[], None]:
        if self._unsubscribe is not None:
            return self._unsubscribe
        self.bus = bus
        self._unsubscribe = bus.subscribe(self.on_event)
        return self._unsubscribe

    def detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            self._running_turn = True
            task = self._task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._task = None
            return
        if isinstance(event, RunFinished):
            self._running_turn = False
            self.schedule(current_turn_id=event.turn_id)

    def schedule(self, *, current_turn_id: str = "") -> bool:
        history = self._current_history()
        if self._closed or self._running_turn or not history:
            return False
        task = self._task
        if task is not None and not task.done():
            return False
        threshold = max(
            1,
            int(self.manager.high_watermark_tokens * self.trigger_ratio),
        )
        before_tokens = estimate_history_tokens(history)
        if before_tokens < threshold:
            return False
        base_digest = history_digest(history)
        if base_digest == self._last_attempt_digest:
            return False
        snapshot = tuple(deepcopy(history))
        self._task = asyncio.create_task(
            self._run(snapshot, base_digest, current_turn_id),
            name="krelcode-background-compact",
        )
        return True

    async def _run(
        self,
        snapshot: tuple[Message, ...],
        base_digest: str,
        turn_id: str,
    ) -> CompactScheduleReport:
        before_tokens = estimate_history_tokens(snapshot)
        self._last_attempt_digest = base_digest
        worker = list(deepcopy(snapshot))
        try:
            changed = await self.manager.maybe_compress(
                worker,
                current_turn_id=turn_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return CompactScheduleReport(scheduled=True)
        if not changed:
            return CompactScheduleReport(scheduled=True)
        current_history = self._current_history()
        if self._running_turn or history_digest(current_history) != base_digest:
            return CompactScheduleReport(
                scheduled=True,
                discarded_reason="history_changed",
                before_tokens=before_tokens,
            )
        post_digest = history_digest(worker)
        if not post_digest or post_digest == base_digest:
            return CompactScheduleReport(
                scheduled=True,
                discarded_reason="no_change",
                before_tokens=before_tokens,
            )
        current_history[:] = worker
        result = self.manager.last_compact_result
        after_tokens = estimate_history_tokens(current_history)
        if self.bus is not None and result is not None:
            summary = getattr(result, "summary", None)
            await self.bus.emit(ContextCompressed(
                before_tokens=int(getattr(result, "before_tokens", before_tokens)),
                after_tokens=int(getattr(result, "after_tokens", after_tokens)),
                strategy="background_structured_summary",
                summary_source=str(getattr(summary, "source", "model") or "model"),
                generation=int(getattr(summary, "generation", 0) or 0),
                high_watermark_tokens=int(getattr(
                    result, "high_watermark_tokens",
                    self.manager.high_watermark_tokens)),
                low_watermark_tokens=int(getattr(
                    result, "low_watermark_tokens", 0)),
                source_savings=tuple(getattr(result, "source_savings", ())),
            ))
        if self.on_committed is not None and result is not None:
            await self.on_committed(
                result, snapshot, tuple(current_history))
        return CompactScheduleReport(
            scheduled=True,
            committed=True,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    async def wait(self) -> Optional[CompactScheduleReport]:
        task = self._task
        if task is None:
            return None
        values = await asyncio.gather(task, return_exceptions=True)
        value = values[0]
        return value if isinstance(value, CompactScheduleReport) else None

    async def close(self) -> None:
        self._closed = True
        self.detach()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    def _current_history(self) -> List[Message]:
        if self._history_provider is not None:
            return self._history_provider()
        return self.history


__all__ = [
    "BackgroundCompactScheduler",
    "CompactManagerPort",
    "CompactScheduleReport",
]
