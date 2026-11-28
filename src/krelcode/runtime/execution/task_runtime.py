"""In-process production runtime for submitted Agent tasks.

The runtime owns physical execution concerns only: identity, task lifecycle,
leases, bounded concurrency, deadlines, retries, pause/resume, cancellation,
checkpoint references and optional durable state.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from dataclasses import replace
from itertools import count
from pathlib import Path
from typing import Mapping, Optional

from krelcode.contracts.runtime import (
    RuntimeLease,
    RuntimeTask,
    RuntimeTaskExecutorPort,
    RuntimeTaskHandle,
    RuntimeTaskRuntimePort,
    RuntimeTaskState,
    RuntimeTaskStatus,
)
from krelcode.core.redact import SecretRedactor
from krelcode.contracts.task_lifecycle import (
    SideEffectStatus,
    TaskErrorClass,
    TaskErrorInfo,
    TaskRecoveryCheck,
    TaskRecoveryDecision,
    TaskRecoveryVerifierPort,
    TaskResultLocation,
    classify_task_error,
)
from .task_state_store import (
    RUNTIME_TASK_STATE_VERSION,
    build_runtime_task_payload,
    load_runtime_task_records,
    persist_runtime_task_payload,
)
from .task_events import LocalTaskEventJournal
from .task_errors import RuntimeSideEffectRecoveryRequired, RuntimeTaskConflict, RuntimeTaskRecoveryBlocked, RuntimeTaskTimeout
from .task_handle import LocalRuntimeTaskHandle
from .task_state_control import TaskStateControlMixin
from .task_identity import task_request_digest

class LocalTaskRuntime(TaskStateControlMixin, RuntimeTaskRuntimePort):
    """Durable, bounded-concurrency in-process runtime."""

    def __init__(
        self,
        *,
        lease_seconds: float = 3600.0,
        owner: Optional[str] = None,
        state_path: Optional[Path] = None,
        redactor: Optional[SecretRedactor] = None,
        autoload: bool = True,
        max_concurrency: int = 2,
        max_queue_size: int = 100,
        default_timeout_seconds: Optional[float] = None,
        event_journal_path: Optional[Path] = None,
        recovery_verifier: Optional[TaskRecoveryVerifierPort] = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        self.lease_seconds = float(lease_seconds)
        self.owner = owner or f"local:{os.getpid()}:{id(self):x}"
        self.state_path = Path(state_path) if state_path is not None else None
        self.redactor = redactor
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_queue_size = max(0, int(max_queue_size))
        self.default_timeout_seconds = (
            max(0.001, float(default_timeout_seconds))
            if default_timeout_seconds is not None else None
        )
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._states: dict[str, RuntimeTaskState] = {}
        self._task_specs: dict[str, RuntimeTask] = {}
        self._executors: dict[str, RuntimeTaskExecutorPort] = {}
        self._tasks: dict[str, asyncio.Task[str]] = {}
        self._completions: dict[str, asyncio.Future[str]] = {}
        self._handles: dict[str, LocalRuntimeTaskHandle] = {}
        self._idempotency: dict[str, str] = {}
        self._closed = False
        self.last_persist_error = ""
        self.recovered_task_ids: tuple[str, ...] = ()
        self._preserve_on_cancel = False
        self.recovery_verifier = recovery_verifier
        journal_path = (
            Path(event_journal_path)
            if event_journal_path is not None
            else (
                self.state_path.with_name(
                    self.state_path.name + ".events.jsonl")
                if self.state_path is not None else None
            )
        )
        self.events = LocalTaskEventJournal(
            journal_path, redactor=redactor)
        self._counter = count(1)
        if autoload:
            self._load()

    @property
    def active_task_id(self) -> str:
        running = [
            task_id for task_id, state in self._states.items()
            if state.status is RuntimeTaskStatus.RUNNING
        ]
        return running[0] if running else ""

    @property
    def active_task_ids(self) -> tuple[str, ...]:
        return tuple(
            task_id for task_id, state in self._states.items()
            if state.status is RuntimeTaskStatus.RUNNING
        )

    def submit(
        self,
        task: RuntimeTask,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle:
        if self._closed:
            raise RuntimeError("LocalTaskRuntime 已关闭")
        queued = sum(
            1 for state in self._states.values()
            if state.status in {
                RuntimeTaskStatus.QUEUED,
                RuntimeTaskStatus.RETRYING,
                RuntimeTaskStatus.RUNNING,
            }
        )
        if queued >= self.max_concurrency + self.max_queue_size:
            raise RuntimeError("Runtime task queue 已满")
        if task.idempotency_key:
            existing_id = self._idempotency.get(task.idempotency_key)
            if existing_id is not None:
                existing_handle = self._handles.get(existing_id)
                existing_task = self._task_specs.get(existing_id)
                if existing_handle is None:
                    raise RuntimeTaskConflict(
                        "idempotency_key 已映射到不可用 task")
                if (
                    existing_task is not None
                    and task_request_digest(existing_task)
                    != task_request_digest(task)
                ):
                    raise RuntimeTaskConflict(
                        "idempotency_key 已用于不同的 task payload")
                return existing_handle
        if task.task_id:
            task_id = task.task_id
        else:
            while True:
                task_id = f"task-{next(self._counter)}"
                if task_id not in self._task_specs:
                    break
        existing_task = self._task_specs.get(task_id)
        if existing_task is not None:
            if (
                task.idempotency_key
                and existing_task.idempotency_key == task.idempotency_key
            ):
                handle = self._handles.get(task_id)
                if handle is not None:
                    return handle
            raise RuntimeTaskConflict(
                f"task_id 已存在且 idempotency_key 不一致: {task_id}")
        root_task_id = (
            task.root_task_id
            or (
                self._task_specs[task.parent_task_id].root_task_id
                if task.parent_task_id in self._task_specs
                else task_id
            )
        )
        task = replace(
            task,
            task_id=task_id,
            root_task_id=root_task_id,
            creator=task.creator or task.identity.principal_id,
            executor=task.executor or self.owner,
        )
        self._task_specs[task_id] = task
        self._executors[task_id] = executor
        self._completions[task_id] = (
            asyncio.get_running_loop().create_future())
        self._states[task_id] = self._new_queued_state(task_id, task)
        if task.idempotency_key:
            self._idempotency[task.idempotency_key] = task_id
        handle = LocalRuntimeTaskHandle(self, task_id, task.turn_id)
        self._handles[task_id] = handle
        self._persist()
        self._schedule(handle, task)
        return handle

    async def recover(
        self,
        task_id: str,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle:
        """Resume an interrupted task from its last checkpoint reference."""
        if self._closed:
            raise RuntimeError("LocalTaskRuntime 已关闭")
        task = self._task_specs.get(task_id)
        if task is None:
            raise KeyError(f"未知 runtime task: {task_id}")
        state = self.inspect(task_id)
        if state.status not in {
            RuntimeTaskStatus.INTERRUPTED,
            RuntimeTaskStatus.PAUSED,
        }:
            raise RuntimeError(
                f"只有 interrupted/paused task 可恢复，当前为 {state.status.value}")
        decision = await self._verify_recovery(task_id, task, state)
        if not decision.allowed:
            self._states[task_id] = replace(
                state,
                recovery_decision=decision,
                sequence=state.sequence + 1,
                updated_at=time.time(),
            )
            self._persist()
            raise RuntimeTaskRecoveryBlocked(decision)
        self._executors[task_id] = executor
        self._reset_completion(task_id)
        self._states[task_id] = replace(
            state,
            status=RuntimeTaskStatus.QUEUED,
            lease=self._new_lease(task_id),
            sequence=state.sequence + 1,
            error="",
            paused_at=None,
            updated_at=time.time(),
            resume_from_checkpoint_id=(
                state.checkpoint.checkpoint_id if state.checkpoint else ""),
            continuation=decision.continuation or state.continuation,
            recovery_decision=decision,
        )
        handle = self._handles.get(task_id)
        if handle is None:
            handle = LocalRuntimeTaskHandle(self, task_id, task.turn_id)
            self._handles[task_id] = handle
        self._persist()
        self._schedule(handle, task)
        return handle

    async def recover_latest(
        self,
        executor: RuntimeTaskExecutorPort,
    ) -> Optional[RuntimeTaskHandle]:
        interrupted = [
            state for state in self._states.values()
            if state.status is RuntimeTaskStatus.INTERRUPTED
        ]
        if not interrupted:
            return None
        latest = max(interrupted, key=lambda item: item.updated_at)
        return await self.recover(latest.task_id, executor)

    async def pause(self, task_id: str) -> bool:
        state = self.inspect(task_id)
        if state.status in {
            RuntimeTaskStatus.PAUSED,
            RuntimeTaskStatus.CANCELLING,
        } or state.status.terminal:
            return False
        self._transition(
            task_id,
            RuntimeTaskStatus.PAUSED,
            lease=None,
            paused_at=time.time(),
        )
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def resume(self, task_id: str) -> bool:
        state = self.inspect(task_id)
        if state.status is not RuntimeTaskStatus.PAUSED:
            return False
        task_spec = self._task_specs.get(task_id)
        executor = self._executors.get(task_id)
        if task_spec is None or executor is None:
            return False
        decision = await self._verify_recovery(task_id, task_spec, state)
        if not decision.allowed:
            self._states[task_id] = replace(
                state,
                recovery_decision=decision,
                sequence=state.sequence + 1,
                updated_at=time.time(),
            )
            self._persist()
            return False
        handle = self._handles.get(task_id)
        if handle is None:
            handle = LocalRuntimeTaskHandle(self, task_id, task_spec.turn_id)
            self._handles[task_id] = handle
        self._ensure_completion(task_id)
        self._states[task_id] = replace(
            state,
            status=RuntimeTaskStatus.QUEUED,
            lease=self._new_lease(task_id),
            sequence=state.sequence + 1,
            paused_at=None,
            updated_at=time.time(),
            resume_from_checkpoint_id=(
                state.checkpoint.checkpoint_id if state.checkpoint else ""),
            continuation=decision.continuation or state.continuation,
            recovery_decision=decision,
        )
        self._persist()
        self._schedule(handle, task_spec)
        return True

    async def cancel(self, task_id: str) -> bool:
        state = self.inspect(task_id)
        if state.status.terminal:
            return False
        self._states[task_id] = replace(
            state,
            cancel_requested=True,
            sequence=state.sequence + 1,
            updated_at=time.time(),
        )
        children = [
            child.task_id for child in self._states.values()
            if child.parent_task_id == task_id
            and not child.status.terminal
        ]
        for child_id in children:
            await self.cancel(child_id)
        self._transition(task_id, RuntimeTaskStatus.CANCELLING)
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._transition(
            task_id,
            RuntimeTaskStatus.CANCELLED,
            lease=None,
        )
        cancelled = self._states[task_id]
        self._states[task_id] = replace(
            cancelled,
            error_info=TaskErrorInfo(
                category=TaskErrorClass.CANCELLED,
                message="task cancelled",
                details={
                    "propagated_children": children,
                    "side_effect_ids": [
                        item.effect_id for item in cancelled.side_effects
                    ],
                    "compensation_required": any(
                        SideEffectStatus(item.status)
                        is SideEffectStatus.COMMITTED
                        for item in cancelled.side_effects
                    ),
                },
            ),
            sequence=cancelled.sequence + 1,
            updated_at=time.time(),
        )
        self._persist()
        self._cancel_completion(task_id)
        return True

    def inspect(self, task_id: str) -> RuntimeTaskState:
        try:
            return self._states[task_id]
        except KeyError as exc:
            raise KeyError(f"未知 runtime task: {task_id}") from exc

    def task(self, task_id: str) -> RuntimeTask:
        try:
            return self._task_specs[task_id]
        except KeyError as exc:
            raise KeyError(f"未知 runtime task: {task_id}") from exc

    def list_tasks(self) -> tuple[RuntimeTaskState, ...]:
        return tuple(sorted(
            self._states.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        ))

    async def wait(self, task_id: str) -> str:
        completion = self._completions.get(task_id)
        if completion is None:
            state = self.inspect(task_id)
            completion = asyncio.get_running_loop().create_future()
            if state.status.terminal:
                completion.set_result("")
            self._completions[task_id] = completion
        return await asyncio.shield(completion)

    def is_done(self, task_id: str) -> bool:
        completion = self._completions.get(task_id)
        if completion is not None:
            return completion.done()
        state = self._states.get(task_id)
        return state is not None and state.status.terminal

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task_id, state in list(self._states.items()):
            if not state.status.terminal:
                await self.cancel(task_id)
        self._persist()

    async def preserve_for_restart(self) -> tuple[str, ...]:
        """Interrupt active tasks without treating process shutdown as user cancel."""
        interrupted: list[str] = []
        self._preserve_on_cancel = True
        try:
            for task_id, task in list(self._tasks.items()):
                if task.done():
                    continue
                interrupted.append(task_id)
                task.cancel()
            if interrupted:
                await asyncio.gather(
                    *[
                        task for task in self._tasks.values()
                        if not task.done()
                    ],
                    return_exceptions=True,
                )
        finally:
            self._preserve_on_cancel = False
            self._persist()
        return tuple(sorted(interrupted))

    def _schedule(
        self,
        handle: LocalRuntimeTaskHandle,
        task: RuntimeTask,
    ) -> None:
        existing = self._tasks.get(handle.task_id)
        if existing is not None and not existing.done():
            return
        asyncio_task = asyncio.get_running_loop().create_task(
            self._run_task(handle.task_id, task),
            name=f"krelcode-task-{handle.task_id}",
        )
        self._tasks[handle.task_id] = asyncio_task
        handle.set_task(asyncio_task)

    async def _run_task(self, task_id: str, task: RuntimeTask) -> str:
        acquired = False
        executor = self._executors.get(task_id)
        if executor is None:
            self._fail_task(task_id, RuntimeError("Executor 未注册"))
            return ""
        try:
            await self._semaphore.acquire()
            acquired = True
            state = self.inspect(task_id)
            if state.status is RuntimeTaskStatus.PAUSED:
                return ""
            self._transition(
                task_id,
                RuntimeTaskStatus.RUNNING,
                lease=state.lease,
                started_at=state.started_at or time.time(),
            )
            max_attempts = max(1, task.retry_policy.max_attempts)
            while True:
                state = self.inspect(task_id)
                attempt = state.attempts + 1
                self._states[task_id] = replace(
                    state,
                    attempts=attempt,
                    sequence=state.sequence + 1,
                    updated_at=time.time(),
                )
                effective_task = replace(
                    task,
                    resume_from=state.checkpoint or task.resume_from,
                )
                try:
                    timeout = self._remaining_timeout(state)
                    result = await asyncio.wait_for(
                        executor.execute(effective_task),
                        timeout=timeout,
                    )
                except asyncio.CancelledError:
                    current = self.inspect(task_id)
                    if current.status is RuntimeTaskStatus.PAUSED:
                        return ""
                    if self._preserve_on_cancel:
                        self._transition(
                            task_id,
                            RuntimeTaskStatus.INTERRUPTED,
                            lease=None,
                            error="daemon shutdown preserved task for recovery",
                        )
                        self._cancel_completion(task_id)
                        raise
                    if current.status is not RuntimeTaskStatus.CANCELLING:
                        self._transition(
                            task_id,
                            RuntimeTaskStatus.CANCELLED,
                            lease=None,
                        )
                    self._cancel_completion(task_id)
                    raise
                except (asyncio.TimeoutError, RuntimeTaskTimeout) as exc:
                    if (
                        attempt < max_attempts
                        and task.retry_policy.retry_on_timeout
                    ):
                        blocked = self.side_effect_retry_error(task_id)
                        if blocked is not None:
                            self._fail_task(task_id, blocked)
                            return ""
                        self._transition(
                            task_id,
                            RuntimeTaskStatus.RETRYING,
                            lease=None,
                            error=str(exc),
                        )
                        await asyncio.sleep(task.retry_policy.delay_for(attempt))
                        self._transition(
                            task_id,
                            RuntimeTaskStatus.RUNNING,
                            lease=self._new_lease(task_id),
                        )
                        continue
                    error = RuntimeTaskTimeout(
                        f"task exceeded deadline after {attempt} attempt(s)")
                    self._transition(
                        task_id,
                        RuntimeTaskStatus.TIMED_OUT,
                        lease=None,
                        error=str(error),
                    )
                    self._set_error_info(task_id, error)
                    self._set_completion_exception(task_id, error)
                    return ""
                except Exception as exc:
                    if (
                        attempt < max_attempts
                        and task.retry_policy.retry_on_failure
                    ):
                        blocked = self.side_effect_retry_error(task_id)
                        if blocked is not None:
                            self._fail_task(task_id, blocked)
                            return ""
                        self._transition(
                            task_id,
                            RuntimeTaskStatus.RETRYING,
                            lease=None,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        await asyncio.sleep(task.retry_policy.delay_for(attempt))
                        self._transition(
                            task_id,
                            RuntimeTaskStatus.RUNNING,
                            lease=self._new_lease(task_id),
                        )
                        continue
                    self._transition(
                        task_id,
                        RuntimeTaskStatus.FAILED,
                        lease=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self._set_error_info(task_id, exc)
                    self._set_completion_exception(task_id, exc)
                    return ""
                self._transition(
                    task_id,
                    RuntimeTaskStatus.COMPLETED,
                    lease=None,
                )
                digest = hashlib.sha256(
                    str(result).encode("utf-8")).hexdigest()
                self.record_result(task_id, TaskResultLocation(
                    kind="inline",
                    uri=f"task:{task_id}:result",
                    digest=digest,
                    summary=str(result)[:2_000],
                ))
                self._set_completion_result(task_id, result)
                return result
        except asyncio.CancelledError:
            current = self.inspect(task_id)
            if current.status is RuntimeTaskStatus.PAUSED:
                return ""
            raise
        finally:
            if acquired:
                self._semaphore.release()
            if self.is_done(task_id):
                self._tasks.pop(task_id, None)

    def _new_queued_state(self, task_id: str, task: RuntimeTask) -> RuntimeTaskState:
        now = time.time()
        deadline_at = task.deadline_at
        budget_timeout = task.budget.timeout_seconds
        if deadline_at is None and budget_timeout is not None:
            deadline_at = now + budget_timeout
        if deadline_at is None and self.default_timeout_seconds is not None:
            deadline_at = now + self.default_timeout_seconds
        return RuntimeTaskState(
            task_id=task_id,
            turn_id=task.turn_id,
            status=RuntimeTaskStatus.QUEUED,
            lease=self._new_lease(task_id),
            sequence=1,
            deadline_at=deadline_at,
            parent_task_id=task.parent_task_id,
            root_task_id=task.root_task_id,
            creator=task.creator,
            executor=task.executor,
            continuation=task.continuation,
            cancel_mode=task.cancel_mode,
            updated_at=now,
        )

    def _new_lease(self, task_id: str) -> RuntimeLease:
        now = time.time()
        return RuntimeLease(
            lease_id=f"lease-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            owner=self.owner,
            acquired_at=now,
            expires_at=now + self.lease_seconds,
        )

    def _remaining_timeout(self, state: RuntimeTaskState) -> Optional[float]:
        if state.deadline_at is None:
            return None
        remaining = state.deadline_at - time.time()
        if remaining <= 0:
            raise RuntimeTaskTimeout("task deadline exceeded")
        return remaining

    async def _verify_recovery(
        self,
        task_id: str,
        task: RuntimeTask,
        state: RuntimeTaskState,
    ) -> TaskRecoveryDecision:
        continuation = state.continuation or task.continuation
        if self.recovery_verifier is not None:
            return await self.recovery_verifier.verify(
                task, state, continuation)
        checks: list[TaskRecoveryCheck] = []
        if not task.message and not task.application.goal:
            checks.append(TaskRecoveryCheck(
                code="objective",
                passed=False,
                reason="task objective missing",
            ))
        if state.deadline_at is not None and state.deadline_at <= time.time():
            checks.append(TaskRecoveryCheck(
                code="budget.deadline",
                passed=False,
                reason="task deadline already exhausted",
            ))
        failed = any(not item.passed for item in checks)
        return TaskRecoveryDecision(
            allowed=not failed,
            checks=tuple(checks),
            continuation=continuation,
            metadata={"task_id": task_id},
        )

    def _transition(
        self,
        task_id: str,
        status: RuntimeTaskStatus,
        *,
        lease: Optional[RuntimeLease] = None,
        error: str = "",
        started_at: Optional[float] = None,
        paused_at: Optional[float] = None,
    ) -> None:
        current = self._states[task_id]
        self._states[task_id] = replace(
            current,
            status=status,
            lease=lease,
            sequence=current.sequence + 1,
            error=error,
            started_at=current.started_at if started_at is None else started_at,
            paused_at=paused_at,
            updated_at=time.time(),
        )
        self._persist()

    def _fail_task(self, task_id: str, error: Exception) -> None:
        self._transition(
            task_id,
            RuntimeTaskStatus.FAILED,
            lease=None,
            error=f"{type(error).__name__}: {error}",
        )
        self._set_error_info(task_id, error)
        self._set_completion_exception(task_id, error)

    def _set_error_info(
        self,
        task_id: str,
        error: BaseException | str,
    ) -> None:
        state = self.inspect(task_id)
        info = classify_task_error(error)
        self._states[task_id] = replace(
            state,
            error_info=info,
            sequence=state.sequence + 1,
            updated_at=time.time(),
        )
        self._persist()

    def _reset_completion(self, task_id: str) -> None:
        self._completions[task_id] = asyncio.get_running_loop().create_future()

    def _ensure_completion(self, task_id: str) -> None:
        if task_id not in self._completions:
            self._reset_completion(task_id)

    def _set_completion_result(self, task_id: str, result: str) -> None:
        completion = self._completions.get(task_id)
        if completion is not None and not completion.done():
            completion.set_result(result)

    def _set_completion_exception(
        self,
        task_id: str,
        error: BaseException,
    ) -> None:
        completion = self._completions.get(task_id)
        if completion is not None and not completion.done():
            completion.set_exception(error)

    def _cancel_completion(self, task_id: str) -> None:
        completion = self._completions.get(task_id)
        if completion is not None and not completion.done():
            completion.cancel()

    def _load(self) -> None:
        if self.state_path is None:
            return
        tasks, loaded_states, next_id = load_runtime_task_records(self.state_path)
        recovered: list[str] = []
        for task_id, task in tasks.items():
            state = loaded_states[task_id]
            journal_cursor = self.events.cursor(task_id)
            if journal_cursor.sequence > state.event_cursor.sequence:
                state = replace(state, event_cursor=journal_cursor)
            if state.status is RuntimeTaskStatus.PAUSED:
                state = replace(state, lease=None)
            elif not state.status.terminal:
                state = replace(
                    state,
                    status=RuntimeTaskStatus.INTERRUPTED,
                    lease=None,
                    sequence=state.sequence + 1,
                    error="runtime restarted before task completed",
                    updated_at=time.time(),
                )
                recovered.append(task_id)
            self._task_specs[task_id] = task
            self._states[task_id] = state
            if task.idempotency_key:
                self._idempotency[task.idempotency_key] = task_id
            self._handles[task_id] = LocalRuntimeTaskHandle(
                self, task_id, state.turn_id)
        self._counter = count(next_id)
        self.recovered_task_ids = tuple(sorted(recovered))
        if recovered:
            self._persist()

    def _persist(self) -> bool:
        if self.state_path is None:
            return True
        payload = build_runtime_task_payload(
            self.owner, self._task_specs, self._states)
        self.last_persist_error = persist_runtime_task_payload(
            self.state_path, payload, self.redactor)
        return not self.last_persist_error


__all__ = ["LocalRuntimeTaskHandle", "LocalTaskRuntime", "RUNTIME_TASK_STATE_VERSION", "RuntimeTaskTimeout"]
