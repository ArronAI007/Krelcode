"""Control surface for one logical runtime task."""

from __future__ import annotations

import asyncio
from typing import Optional, Protocol

from ...contracts.runtime import RuntimeTaskHandle, RuntimeTaskState


class _RuntimeControl(Protocol):
    def inspect(self, task_id: str) -> RuntimeTaskState: ...
    def is_done(self, task_id: str) -> bool: ...
    async def wait(self, task_id: str) -> str: ...
    async def cancel(self, task_id: str) -> bool: ...
    async def pause(self, task_id: str) -> bool: ...
    async def resume(self, task_id: str) -> bool: ...


class LocalRuntimeTaskHandle(RuntimeTaskHandle):
    """Tasks survive queued, paused, waiting and recovery cycles."""

    def __init__(
        self,
        runtime: _RuntimeControl,
        task_id: str,
        turn_id: str,
        task: Optional[asyncio.Task[str]] = None,
    ) -> None:
        self._runtime = runtime
        self._task_id = task_id
        self._turn_id = turn_id
        self._task = task

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def state(self) -> RuntimeTaskState:
        return self._runtime.inspect(self._task_id)

    @property
    def done(self) -> bool:
        return self._runtime.is_done(self._task_id)

    @property
    def task(self) -> Optional[asyncio.Task[str]]:
        """Current physical task, absent while queued, waiting or paused."""
        return self._task

    def set_task(self, task: Optional[asyncio.Task[str]]) -> None:
        self._task = task

    async def wait(self) -> str:
        return await self._runtime.wait(self._task_id)

    async def cancel(self) -> None:
        await self._runtime.cancel(self._task_id)

    async def pause(self) -> bool:
        return await self._runtime.pause(self._task_id)

    async def resume(self) -> bool:
        return await self._runtime.resume(self._task_id)


__all__ = ["LocalRuntimeTaskHandle"]
