"""Attachment and control surface for one durable query task."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Callable, Mapping, Optional, Sequence

from krelcode.contracts.runtime import RuntimeTaskHandle, RuntimeTaskState
from krelcode.contracts.task_lifecycle import TaskAttachment, TaskEventCursor
from krelcode.core.session_events import SessionEvent


class DetachControl:
    def __init__(self) -> None:
        self.detached = False


class QuerySession:
    """One query task with a detachable event attachment."""

    def __init__(
        self,
        handle: RuntimeTaskHandle,
        task: asyncio.Task[str],
        events: AsyncIterator[SessionEvent],
        detach_control: Optional[DetachControl] = None,
        read_events: Optional[
            Callable[[int, int], Sequence[Mapping[str, object]]]
        ] = None,
        attach: Optional[Callable[[str], TaskAttachment]] = None,
    ) -> None:
        self.runtime_handle = handle
        self.task = task
        self.events = events
        self._detach_control = detach_control or DetachControl()
        self._read_events = read_events
        self._attach = attach

    @property
    def task_id(self) -> str:
        return self.runtime_handle.task_id

    @property
    def turn_id(self) -> str:
        return self.runtime_handle.turn_id

    @property
    def state(self) -> RuntimeTaskState:
        return self.runtime_handle.state

    @property
    def done(self) -> bool:
        return self.task.done()

    @property
    def cursor(self) -> TaskEventCursor:
        return self.runtime_handle.state.event_cursor

    @property
    def detached(self) -> bool:
        return self._detach_control.detached

    async def wait(self) -> str:
        return await self.task

    async def cancel(self) -> None:
        await self.runtime_handle.cancel()
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def pause(self) -> bool:
        return await self.runtime_handle.pause()

    async def resume(self) -> bool:
        return await self.runtime_handle.resume()

    def detach(self) -> TaskAttachment:
        """Disconnect the caller without cancelling the durable task."""
        self._detach_control.detached = True
        if self._attach is not None:
            return self._attach(self.task_id)
        return TaskAttachment(
            task_id=self.task_id,
            cursor=self.cursor,
            status=self.state.status.value,
        )

    def read_events(
        self,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> tuple[Mapping[str, object], ...]:
        if self._read_events is None:
            return ()
        return tuple(self._read_events(after_sequence, limit))


__all__ = ["DetachControl", "QuerySession"]
