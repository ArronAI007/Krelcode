"""Connection-independent task identity, cursors, cancellation and recovery."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest

from krelcode.agent.turn.query import QueryEngine
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.runtime import AgentRuntime
from krelcode.contracts.application import ApplicationRequestFactory, TaskBudget
from krelcode.contracts.runtime import (
    RuntimeCheckpointRef,
    RuntimeTask,
    RuntimeTaskExecutorPort,
    RuntimeTaskState,
    RuntimeTaskStatus,
)
from krelcode.contracts.state import StateCheckpointBundle
from krelcode.contracts.plan import PlanControl, PlanNode
from krelcode.contracts.task_lifecycle import (
    SideEffectStatus,
    TaskContinuation,
    TaskErrorClass,
    TaskRecoveryCheck,
    TaskRecoveryDecision,
    TaskSideEffectRecord,
)
from krelcode.core.messages import Message
from krelcode.core.messages import ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.runtime.execution import LocalTaskRuntime
from krelcode.runtime.execution.task_runtime import RuntimeTaskConflict
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.state_checkpoint import LocalStateCheckpointStore
from krelcode.session.continuation import SessionContinuationAdapter
from krelcode.session.task_recovery import SessionTaskRecoveryVerifier
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.tools.builtin.write_file import WriteFileTool


def _task(
    *,
    task_id: str = "",
    parent_task_id: str = "",
    idempotency_key: str = "",
) -> RuntimeTask:
    return RuntimeTask(
        task_id=task_id,
        parent_task_id=parent_task_id,
        idempotency_key=idempotency_key,
        turn_id=f"turn-{task_id or 'new'}",
        message="inspect durable task",
        application=ApplicationRequestFactory.from_input_source(
            "headless", goal="inspect durable task"),
    )


class _ImmediateExecutor(RuntimeTaskExecutorPort):
    async def execute(self, task: RuntimeTask) -> str:
        return f"done:{task.task_id}"


class _SlowExecutor(RuntimeTaskExecutorPort):
    async def execute(self, task: RuntimeTask) -> str:
        await asyncio.sleep(30)
        return task.message


def test_stable_task_id_and_idempotent_submission(tmp_path: Path) -> None:
    async def scenario() -> None:
        state_path = tmp_path / "runtime" / "idempotent.json"
        runtime = LocalTaskRuntime(state_path=state_path)
        task = _task(task_id="fix-123", idempotency_key="request-1")
        first = runtime.submit(task, _ImmediateExecutor())
        second = runtime.submit(task, _ImmediateExecutor())
        assert first.task_id == "fix-123"
        assert second.task_id == first.task_id
        assert await first.wait() == "done:fix-123"

        with pytest.raises(RuntimeTaskConflict):
            runtime.submit(
                replace(task, idempotency_key="request-2"),
                _ImmediateExecutor(),
            )
        restarted = LocalTaskRuntime(state_path=state_path)
        replay = restarted.submit(task, _ImmediateExecutor())
        assert replay.task_id == first.task_id
        assert restarted.inspect(replay.task_id).status is RuntimeTaskStatus.COMPLETED

    asyncio.run(scenario())


def test_query_session_detaches_and_events_are_replayable(tmp_path: Path) -> None:
    async def scenario() -> None:
        loop = AgentLoop(
            llm=MockProvider(scripted=["durable answer"]),
            registry=ToolRegistry([]),
            tool_context=ToolContext(
                workspace_root=tmp_path,
                sandbox_enabled=False,
            ),
            policy=AllowAllPolicy(),
            system_prompt="SYS",
        )
        runtime = LocalTaskRuntime(
            state_path=tmp_path / "runtime" / "tasks.json",
        )
        engine = QueryEngine(loop, task_runtime=runtime)
        query = engine.start(
            "work independently",
            task_id="detached-task",
            idempotency_key="detach-1",
        )
        attached = query.detach()
        assert attached.task_id == "detached-task"
        await query.events.aclose()
        assert await query.wait() == "durable answer"

        state = runtime.inspect("detached-task")
        assert state.status is RuntimeTaskStatus.COMPLETED
        assert state.event_cursor.sequence > 0
        assert state.result is not None
        events = runtime.read_events("detached-task", after_sequence=0)
        assert events
        assert any(
            item.get("event_type") in {"RunStarted", "RunFinished"}
            for item in events
        )

        restarted = LocalTaskRuntime(
            state_path=tmp_path / "runtime" / "tasks.json",
        )
        restored = restarted.attach("detached-task")
        assert restored.cursor.sequence == state.event_cursor.sequence
        assert restarted.read_events("detached-task")

    asyncio.run(scenario())


def test_cancel_propagates_and_preserves_side_effect_facts() -> None:
    async def scenario() -> None:
        runtime = LocalTaskRuntime(max_concurrency=4)
        parent = runtime.submit(
            _task(task_id="parent-task"), _SlowExecutor())
        child = runtime.submit(
            _task(task_id="child-task", parent_task_id="parent-task"),
            _SlowExecutor(),
        )
        runtime.record_side_effect(
            parent.task_id,
            TaskSideEffectRecord(
                effect_id="effect-1",
                task_id=parent.task_id,
                kind="file_mutation",
                resource="src/a.py",
                status=SideEffectStatus.COMMITTED,
                compensation="restore from checkpoint",
            ),
        )
        assert await runtime.cancel(parent.task_id) is True
        await asyncio.gather(parent.wait(), child.wait(), return_exceptions=True)

        parent_state = runtime.inspect(parent.task_id)
        child_state = runtime.inspect(child.task_id)
        assert parent_state.status is RuntimeTaskStatus.CANCELLED
        assert child_state.status is RuntimeTaskStatus.CANCELLED
        assert parent_state.cancel_requested is True
        assert parent_state.side_effects[0].resource == "src/a.py"
        assert parent_state.error_info is not None
        assert parent_state.error_info.category is TaskErrorClass.CANCELLED
        assert parent_state.error_info.details["compensation_required"] is True
        compensated = runtime.update_side_effect(
            parent.task_id,
            "effect-1",
            SideEffectStatus.COMPENSATED,
            compensation="restored src/a.py",
        )
        assert compensated.side_effects[0].status is SideEffectStatus.COMPENSATED
        assert compensated.error_info is not None
        assert compensated.error_info.details[
            "compensation_required"] is False

    asyncio.run(scenario())


def test_recovery_preflight_blocks_stale_continuation() -> None:
    class BlockingVerifier:
        async def verify(self, task, state, continuation):
            return TaskRecoveryDecision(
                allowed=False,
                requires_user=True,
                checks=(TaskRecoveryCheck(
                    code="workspace",
                    passed=False,
                    reason="workspace changed",
                ),),
                continuation=continuation,
            )

    class CrashExecutor(RuntimeTaskExecutorPort):
        def __init__(self):
            self.started = asyncio.Event()

        async def execute(self, task: RuntimeTask) -> str:
            self.started.set()
            await asyncio.sleep(30)
            return "never"

    async def scenario() -> None:
        runtime = LocalTaskRuntime()
        continuation = TaskContinuation(
            objective="inspect durable task",
            recovery_checkpoint_id="ctx-1",
            permission_mode="default",
            remaining_budget=TaskBudget(max_steps=3),
        )
        task = replace(_task(task_id="recover-task"), continuation=continuation)
        executor = CrashExecutor()
        handle = runtime.submit(task, executor)
        await executor.started.wait()
        assert await runtime.pause(handle.task_id) is True
        runtime.record_continuation(handle.task_id, continuation)
        blocked = LocalTaskRuntime(recovery_verifier=BlockingVerifier())
        # Load equivalent persisted state into a runtime that applies preflight.
        blocked._task_specs[handle.task_id] = task
        blocked._states[handle.task_id] = replace(
            runtime.inspect(handle.task_id),
            status=RuntimeTaskStatus.PAUSED,
        )
        blocked._handles[handle.task_id] = blocked._handles.get(
            handle.task_id,
            __import__(
                "krelcode.runtime.execution.task_runtime",
                fromlist=["LocalRuntimeTaskHandle"],
            ).LocalRuntimeTaskHandle(
                blocked, handle.task_id, task.turn_id),
        )
        blocked._executors[handle.task_id] = _ImmediateExecutor()
        assert await blocked.resume(handle.task_id) is False
        state = blocked.inspect(handle.task_id)
        assert state.recovery_decision is not None
        assert state.recovery_decision.requires_user is True

    asyncio.run(scenario())


def test_graceful_shutdown_preserves_active_task_for_restart(
    tmp_path: Path,
) -> None:
    class SlowExecutor(RuntimeTaskExecutorPort):
        def __init__(self):
            self.started = asyncio.Event()

        async def execute(self, task: RuntimeTask) -> str:
            self.started.set()
            await asyncio.sleep(30)
            return "never"

    async def scenario() -> None:
        state_path = tmp_path / "runtime" / "tasks.json"
        runtime = LocalTaskRuntime(state_path=state_path)
        executor = SlowExecutor()
        handle = runtime.submit(
            replace(
                _task(task_id="preserved-task"),
                continuation=TaskContinuation(
                    objective="inspect durable task",
                    recovery_checkpoint_id="ctx-1",
                ),
            ),
            executor,
        )
        await executor.started.wait()
        assert await runtime.preserve_for_restart() == ("preserved-task",)
        assert (
            runtime.inspect(handle.task_id).status
            is RuntimeTaskStatus.INTERRUPTED
        )

        restarted = LocalTaskRuntime(state_path=state_path)
        restored = restarted.inspect("preserved-task")
        assert restored.status is RuntimeTaskStatus.INTERRUPTED
        assert restored.continuation is not None
        assert restored.continuation.recovery_checkpoint_id == "ctx-1"

    asyncio.run(scenario())


def test_harness_records_committed_side_effect_for_durable_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        loop = AgentLoop(
            llm=MockProvider(scripted=[
                Message.assistant(tool_calls=[
                    ToolCall(
                        "write-1",
                        "write_file",
                        {"path": "durable.txt", "content": "written"},
                    ),
                ]),
                "done",
            ]),
            registry=ToolRegistry([WriteFileTool()]),
            tool_context=ToolContext(
                workspace_root=tmp_path,
                sandbox_enabled=False,
            ),
            policy=AllowAllPolicy(),
            system_prompt="SYS",
        )
        runtime = LocalTaskRuntime()
        runtime_facade = AgentRuntime(loop, task_runtime=runtime)
        query = runtime_facade.start_query(
            "write durable file",
            task_id="side-effect-task",
        )
        assert await query.wait() == "done"
        state = runtime.inspect(query.task_id)
        assert state.side_effects
        assert state.side_effects[0].resource == "durable.txt"
        assert (
            state.side_effects[0].status
            is SideEffectStatus.COMMITTED
        )

    asyncio.run(scenario())


def test_task_errors_are_classified_and_result_location_is_recorded() -> None:
    class PermissionExecutor(RuntimeTaskExecutorPort):
        async def execute(self, task: RuntimeTask) -> str:
            raise PermissionError("permission denied")

    async def scenario() -> None:
        runtime = LocalTaskRuntime()
        failed = runtime.submit(
            _task(task_id="error-task"), PermissionExecutor())
        await asyncio.gather(failed.wait(), return_exceptions=True)
        state = runtime.inspect(failed.task_id)
        assert state.error_info is not None
        assert state.error_info.category is TaskErrorClass.PERMISSION

        completed = runtime.submit(
            _task(task_id="result-task"), _ImmediateExecutor())
        assert await completed.wait() == "done:result-task"
        result_state = runtime.inspect(completed.task_id)
        assert result_state.result is not None
        assert result_state.result.available is True

    asyncio.run(scenario())


def test_task_continuation_round_trips_through_state_store(tmp_path: Path) -> None:
    continuation = TaskContinuation(
        objective="finish durable task",
        plan={"mode": "planner_executor", "nodes": []},
        confirmed_facts=("input validated",),
        failed_attempts=("provider timeout",),
        artifacts=({"id": "artifact-1", "kind": "patch"},),
        waiting_items=("external_event: review",),
        remaining_budget=TaskBudget(max_steps=4, max_tokens=800),
        permission_mode="default",
        workspace_version={"head": "abc", "tree_digest": "tree"},
        recovery_checkpoint_id="ctx-1",
        instruction_ids=("core:base",),
    )
    async def scenario() -> None:
        state = LocalTaskRuntime()
        handle = state.submit(
            replace(_task(task_id="state-task"), continuation=continuation),
            _ImmediateExecutor(),
        )
        await handle.wait()
        state.record_event(
            handle.task_id,
            {"type": "test_event"},
            event_type="test_event",
        )
        task_state = state.inspect(handle.task_id)
        bundle = StateCheckpointBundle(
            bundle_id="bundle-task",
            task_id=handle.task_id,
            runtime_state=task_state,
        )
        store = LocalStateCheckpointStore(tmp_path)
        store.commit(bundle)
        loaded = store.latest_for_task(handle.task_id)
        assert loaded is not None
        assert loaded.runtime_state is not None
        assert loaded.runtime_state.continuation is not None
        assert loaded.runtime_state.continuation.confirmed_facts == (
            "input validated",
        )
        assert loaded.runtime_state.event_cursor.sequence > 0

    asyncio.run(scenario())


def test_continuation_capture_rebuilds_goal_plan_permission_and_workspace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request = ApplicationRequestFactory.from_input_source(
            "headless",
            goal="capture continuation state",
            budget=TaskBudget(max_steps=5, max_tokens=1_000),
        )
        loop = AgentLoop(
            llm=MockProvider(scripted=["captured"]),
            registry=ToolRegistry([]),
            tool_context=ToolContext(
                workspace_root=tmp_path,
                sandbox_enabled=False,
            ),
            policy=AllowAllPolicy(),
            system_prompt="SYS",
        )
        runtime = LocalTaskRuntime()
        engine = QueryEngine(loop, task_runtime=runtime)
        query = engine.start(
            "capture continuation state",
            application_request=request,
            task_id="continuation-task",
        )
        await query.wait()
        loop.tool_context.plan_control = PlanControl.from_nodes(
            (PlanNode(id="verify", objective="verify continuation"),),
            trigger_fact="continuation capture",
        )
        adapter = SessionContinuationAdapter(
            loop,
            task_runtime=runtime,
            state_checkpoints=LocalStateCheckpointStore(tmp_path),
        )
        continuation = adapter.capture_current(
            task_id=query.task_id,
            checkpoint_id="ctx-continuation",
        )
        assert continuation.objective == "capture continuation state"
        assert continuation.permission_mode == "default"
        assert continuation.workspace_version
        assert continuation.plan["mode"] == "todo"
        assert continuation.remaining_budget.max_steps is not None
        assert continuation.recovery_checkpoint_id == "ctx-continuation"

    asyncio.run(scenario())


def test_recovery_verifier_detects_permission_and_workspace_changes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        loop = AgentLoop(
            llm=MockProvider(scripted=["unused"]),
            registry=ToolRegistry([]),
            tool_context=ToolContext(
                workspace_root=tmp_path,
                sandbox_enabled=False,
            ),
            policy=AllowAllPolicy(),
            system_prompt="SYS",
        )
        task = _task(task_id="verify-recovery")
        state = RuntimeTaskState(
            task_id=task.task_id,
            turn_id=task.turn_id,
            status=RuntimeTaskStatus.INTERRUPTED,
        )
        continuation = TaskContinuation(
            objective="inspect durable task",
            permission_mode="read-only",
            workspace_version={
                "head": "different-head",
                "tree_digest": "different-tree",
            },
            remaining_budget=TaskBudget(max_steps=1),
            recovery_checkpoint_id="ctx-1",
        )
        decision = await SessionTaskRecoveryVerifier(loop).verify(
            task, state, continuation)
        assert decision.allowed is False
        assert decision.requires_user is True
        assert {
            item.code for item in decision.failed_checks
        } >= {"permission", "workspace"}

    asyncio.run(scenario())


__all__ = []
