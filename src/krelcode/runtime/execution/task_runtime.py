"""In-process production runtime for one submitted Agent task.

The runtime owns physical execution concerns only: task identity, leases,
asyncio task creation, cancellation and lifecycle state.  It knows nothing
about prompts, tools, context assembly or session commands.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from itertools import count
from typing import Optional

from krelcode.contracts.runtime import (
    RuntimeCheckpointRef,
    RuntimeLease,
    RuntimeTask,
    RuntimeTaskExecutorPort,
    RuntimeTaskHandle,
    RuntimeTaskRuntimePort,
    RuntimeStateRef,
    RuntimeTaskState,
    RuntimeTaskStatus,
)


class LocalRuntimeTaskHandle:
    """Control surface for one asyncio-backed task."""

    def __init__(
        self,
        runtime: "LocalTaskRuntime",
        task_id: str,
        turn_id: str,
        task: asyncio.Task[str],
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
        return self._task.done()

    @property
    def task(self) -> asyncio.Task[str]:
        """Compatibility view for event consumers that wait on asyncio tasks."""
        return self._task

    async def wait(self) -> str:
        return await self._task

    async def cancel(self) -> None:
        await self._runtime.cancel(self._task_id)


class LocalTaskRuntime(RuntimeTaskRuntimePort):
    """Single-process task runtime with explicit task/state semantics."""

    def __init__(
        self,
        *,
        lease_seconds: float = 3600.0,
        owner: Optional[str] = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        self.lease_seconds = float(lease_seconds)
        self.owner = owner or f"local:{os.getpid()}:{id(self):x}"
        self._counter = count(1)
        self._states: dict[str, RuntimeTaskState] = {}
        self._tasks: dict[str, asyncio.Task[str]] = {}
        self._handles: dict[str, LocalRuntimeTaskHandle] = {}
        self._active_task_id = ""
        self._closed = False

    @property
    def active_task_id(self) -> str:
        return self._active_task_id

    def submit(
        self,
        task: RuntimeTask,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle:
        if self._closed:
            raise RuntimeError("LocalTaskRuntime 已关闭")
        if self._active_task_id:
            active = self._states.get(self._active_task_id)
            if active is not None and not active.status.terminal:
                raise RuntimeError("已有任务正在运行")
            self._active_task_id = ""

        task_id = f"task-{next(self._counter)}"
        now = time.time()
        lease = RuntimeLease(
            lease_id=f"lease-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            owner=self.owner,
            acquired_at=now,
            expires_at=now + self.lease_seconds,
        )
        self._states[task_id] = RuntimeTaskState(
            task_id=task_id,
            turn_id=task.turn_id,
            status=RuntimeTaskStatus.QUEUED,
            lease=lease,
            sequence=1,
            updated_at=now,
        )

        async def run() -> str:
            self._transition(
                task_id,
                RuntimeTaskStatus.RUNNING,
                lease=lease,
            )
            return await executor.execute(task)

        asyncio_task = asyncio.get_running_loop().create_task(
            run(), name=f"krelcode-task-{task_id}")
        self._tasks[task_id] = asyncio_task
        self._active_task_id = task_id
        handle = LocalRuntimeTaskHandle(
            self,
            task_id=task_id,
            turn_id=task.turn_id,
            task=asyncio_task,
        )
        self._handles[task_id] = handle
        asyncio_task.add_done_callback(
            lambda finished: self._finish(task_id, finished))
        return handle

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.done():
            return False
        self._transition(task_id, RuntimeTaskStatus.CANCELLING)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def inspect(self, task_id: str) -> RuntimeTaskState:
        try:
            return self._states[task_id]
        except KeyError as exc:
            raise KeyError(f"未知 runtime task: {task_id}") from exc

    def state_ref(self, task_id: str) -> RuntimeStateRef:
        state = self.inspect(task_id)
        return RuntimeStateRef(
            state_id=f"{task_id}:{state.sequence}",
            task_id=task_id,
            turn_id=state.turn_id,
            sequence=state.sequence,
            updated_at=state.updated_at,
            checkpoint=state.checkpoint,
        )

    def checkpoint(self, task_id: str) -> Optional[RuntimeCheckpointRef]:
        return self.inspect(task_id).checkpoint

    def record_checkpoint(
        self,
        task_id: str,
        checkpoint: RuntimeCheckpointRef,
    ) -> RuntimeTaskState:
        state = self.inspect(task_id)
        updated = RuntimeTaskState(
            task_id=state.task_id,
            turn_id=state.turn_id,
            status=state.status,
            lease=state.lease,
            checkpoint=checkpoint,
            sequence=state.sequence + 1,
            error=state.error,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        return updated

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        active = self._active_task_id
        if active:
            await self.cancel(active)
        self._active_task_id = ""

    def _finish(self, task_id: str, finished: asyncio.Task[str]) -> None:
        current = self._states.get(task_id)
        if current is None:
            return
        if finished.cancelled():
            status = RuntimeTaskStatus.CANCELLED
            error = ""
        else:
            exception = finished.exception()
            if exception is None:
                status = RuntimeTaskStatus.COMPLETED
                error = ""
            else:
                status = RuntimeTaskStatus.FAILED
                error = f"{type(exception).__name__}: {exception}"
        self._transition(
            task_id,
            status,
            lease=None,
            error=error,
        )
        if self._active_task_id == task_id:
            self._active_task_id = ""

    def _transition(
        self,
        task_id: str,
        status: RuntimeTaskStatus,
        *,
        lease: Optional[RuntimeLease] = None,
        error: str = "",
    ) -> None:
        current = self._states[task_id]
        self._states[task_id] = RuntimeTaskState(
            task_id=current.task_id,
            turn_id=current.turn_id,
            status=status,
            lease=lease,
            checkpoint=current.checkpoint,
            sequence=current.sequence + 1,
            error=error,
            updated_at=time.time(),
        )


__all__ = ["LocalRuntimeTaskHandle", "LocalTaskRuntime"]
