"""Runtime task/state contracts and the local production runtime."""

from __future__ import annotations

import asyncio

import pytest

from krelcode.contracts.application import ApplicationRequestFactory
from krelcode.contracts.runtime import (
    RuntimeCheckpointRef,
    RuntimeTask,
    RuntimeTaskExecutorPort,
    RuntimeTaskStatus,
)
from krelcode.runtime.execution import LocalTaskRuntime


def make_task(turn_id: str = "turn-test") -> RuntimeTask:
    return RuntimeTask(
        turn_id=turn_id,
        message="inspect runtime",
        application=ApplicationRequestFactory.from_input_source(
            "headless",
            goal="inspect runtime",
        ),
    )


def test_local_task_runtime_exposes_lease_and_terminal_state() -> None:
    started = asyncio.Event()

    class Executor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            started.set()
            await asyncio.sleep(0)
            return f"done:{task.turn_id}"

    async def scenario() -> None:
        runtime = LocalTaskRuntime()
        handle = runtime.submit(make_task(), Executor())

        initial = runtime.inspect(handle.task_id)
        assert initial.status is RuntimeTaskStatus.QUEUED
        assert initial.lease is not None

        await started.wait()
        running = runtime.inspect(handle.task_id)
        assert running.status is RuntimeTaskStatus.RUNNING
        assert running.lease is not None
        assert running.lease.task_id == handle.task_id

        assert await handle.wait() == "done:turn-test"
        completed = runtime.inspect(handle.task_id)
        assert completed.status is RuntimeTaskStatus.COMPLETED
        assert completed.lease is None
        ref = runtime.state_ref(handle.task_id)
        assert ref.task_id == handle.task_id
        assert ref.sequence == completed.sequence

    asyncio.run(scenario())


def test_local_task_runtime_cancellation_and_checkpoint_ref() -> None:
    class SlowExecutor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            await asyncio.sleep(30)
            return task.message

    async def scenario() -> None:
        runtime = LocalTaskRuntime()
        handle = runtime.submit(make_task("turn-cancel"), SlowExecutor())
        checkpoint = RuntimeCheckpointRef(
            checkpoint_id="ctx-1",
            kind="context",
            turn_id=handle.turn_id,
            task_id=handle.task_id,
        )
        updated = runtime.record_checkpoint(handle.task_id, checkpoint)
        assert updated.checkpoint == checkpoint

        assert await runtime.cancel(handle.task_id) is True
        with pytest.raises(asyncio.CancelledError):
            await handle.wait()
        state = runtime.inspect(handle.task_id)
        assert state.status is RuntimeTaskStatus.CANCELLED
        assert state.checkpoint == checkpoint
        assert state.lease is None

    asyncio.run(scenario())


def test_local_task_runtime_rejects_concurrent_tasks() -> None:
    class SlowExecutor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            await asyncio.sleep(30)
            return task.message

    async def scenario() -> None:
        runtime = LocalTaskRuntime()
        first = runtime.submit(make_task("turn-1"), SlowExecutor())
        with pytest.raises(RuntimeError, match="已有任务正在运行"):
            runtime.submit(make_task("turn-2"), SlowExecutor())
        await runtime.cancel(first.task_id)

    asyncio.run(scenario())
