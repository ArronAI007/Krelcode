"""Runtime task/state contracts and the local production runtime."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from krelcode.contracts.application import ApplicationRequestFactory
from krelcode.contracts.runtime import (
    RuntimeCheckpointRef,
    HarnessReleaseIdentity,
    RuntimeIdentity,
    RuntimeRetryPolicy,
    RuntimeSandboxBinding,
    RuntimeTask,
    RuntimeTaskExecutorPort,
    RuntimeTaskStatus,
)
from krelcode.core.redact import SecretRedactor
from krelcode.runtime.execution import LocalTaskRuntime
from krelcode.runtime.execution.task_runtime import RuntimeTaskTimeout


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


def test_local_task_runtime_queues_above_concurrency_limit() -> None:
    class SlowExecutor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            await asyncio.sleep(30)
            return task.message

    async def scenario() -> None:
        runtime = LocalTaskRuntime(max_concurrency=1, max_queue_size=2)
        first = runtime.submit(make_task("turn-1"), SlowExecutor())
        second = runtime.submit(make_task("turn-2"), SlowExecutor())
        assert runtime.inspect(first.task_id).status in {
            RuntimeTaskStatus.QUEUED, RuntimeTaskStatus.RUNNING}
        assert runtime.inspect(second.task_id).status is RuntimeTaskStatus.QUEUED
        await runtime.cancel(first.task_id)
        await runtime.cancel(second.task_id)

    asyncio.run(scenario())


def test_local_task_runtime_persists_and_recovers_interrupted_task(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".krelcode" / "runtime" / "tasks.json"
    started = asyncio.Event()

    class FirstExecutor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            started.set()
            await asyncio.sleep(30)
            return task.message

    class RecoveryExecutor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            return f"recovered:{task.message}"

    async def scenario() -> None:
        first = LocalTaskRuntime(state_path=state_path)
        handle = first.submit(make_task("turn-persist"), FirstExecutor())
        await started.wait()
        crash_copy = tmp_path / "crash-state.json"
        crash_copy.write_bytes(state_path.read_bytes())
        await first.cancel(handle.task_id)

        resumed = LocalTaskRuntime(state_path=crash_copy)
        assert resumed.recovered_task_ids == (handle.task_id,)
        interrupted = resumed.inspect(handle.task_id)
        assert interrupted.status is RuntimeTaskStatus.INTERRUPTED
        assert interrupted.lease is None
        assert resumed.task(handle.task_id).turn_id == "turn-persist"

        recovered = await resumed.recover(handle.task_id, RecoveryExecutor())
        assert await recovered.wait() == "recovered:inspect runtime"
        assert resumed.inspect(handle.task_id).status is RuntimeTaskStatus.COMPLETED

    asyncio.run(scenario())


def test_local_task_runtime_redacts_persisted_task_spec(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime-state.json"

    class Executor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            return "ok"

    task = RuntimeTask(
        turn_id="turn-secret",
        message="use sk-abcdefghijklmnop",
        application=ApplicationRequestFactory.from_input_source(
            "headless",
            goal="use secret",
        ),
    )

    async def scenario() -> None:
        runtime = LocalTaskRuntime(
            state_path=state_path,
            redactor=SecretRedactor(),
        )
        handle = runtime.submit(task, Executor())
        assert await handle.wait() == "ok"

    asyncio.run(scenario())
    persisted = state_path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in persisted
    assert "***" in persisted


def test_runtime_task_persists_identity_release_and_sandbox_binding(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "runtime-state.json"

    class Executor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            return "ok"

    task = RuntimeTask(
        turn_id="turn-bind",
        message="bound",
        application=ApplicationRequestFactory.from_input_source(
            "headless", goal="bound"),
        identity=RuntimeIdentity(
            principal_id="alice",
            tenant_id="tenant-a",
            organization_id="org-a",
        ),
        harness_release=HarnessReleaseIdentity(
            release_id="runtime-1",
            release_hash="sha256:release",
        ),
        sandbox=RuntimeSandboxBinding(
            backend="seatbelt",
            isolation_level="os",
            memory_mb=512,
        ),
    )

    async def scenario() -> None:
        runtime = LocalTaskRuntime(state_path=state_path)
        handle = runtime.submit(task, Executor())
        assert await handle.wait() == "ok"

    asyncio.run(scenario())
    restored = LocalTaskRuntime(state_path=state_path)
    persisted = restored.task("task-1")
    assert persisted.identity.tenant_id == "tenant-a"
    assert persisted.harness_release.release_id == "runtime-1"
    assert persisted.sandbox.memory_mb == 512


def test_local_task_runtime_timeout_and_retry() -> None:
    class FlakyExecutor(RuntimeTaskExecutorPort):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, task: RuntimeTask) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return "retried"

    async def scenario() -> None:
        runtime = LocalTaskRuntime()
        retry_task = RuntimeTask(
            turn_id="turn-retry",
            message="retry",
            application=ApplicationRequestFactory.from_input_source(
                "headless", goal="retry"),
            retry_policy=RuntimeRetryPolicy(
                max_attempts=2,
                retry_on_failure=True,
            ),
        )
        handle = runtime.submit(retry_task, FlakyExecutor())
        assert await handle.wait() == "retried"
        assert runtime.inspect(handle.task_id).attempts == 2

        timeout_task = RuntimeTask(
            turn_id="turn-timeout",
            message="timeout",
            application=ApplicationRequestFactory.from_input_source(
                "headless", goal="timeout"),
            deadline_at=time.time() + 0.05,
        )

        class SlowExecutor(RuntimeTaskExecutorPort):
            async def execute(self, task: RuntimeTask) -> str:
                await asyncio.sleep(1)
                return "late"

        timed = runtime.submit(timeout_task, SlowExecutor())
        with pytest.raises(RuntimeTaskTimeout):
            await timed.wait()
        assert runtime.inspect(timed.task_id).status is RuntimeTaskStatus.TIMED_OUT

    asyncio.run(scenario())


def test_local_task_runtime_pause_and_resume() -> None:
    class PausableExecutor(RuntimeTaskExecutorPort):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, task: RuntimeTask) -> str:
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(30)
            return "resumed"

    async def scenario() -> None:
        runtime = LocalTaskRuntime()
        handle = runtime.submit(make_task("turn-pause"), PausableExecutor())
        await asyncio.sleep(0.02)
        assert await runtime.pause(handle.task_id) is True
        assert runtime.inspect(handle.task_id).status is RuntimeTaskStatus.PAUSED
        assert await runtime.resume(handle.task_id) is True
        assert await handle.wait() == "resumed"
        assert runtime.inspect(handle.task_id).status is RuntimeTaskStatus.COMPLETED

    asyncio.run(scenario())
