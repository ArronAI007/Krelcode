"""In-process production runtime for submitted Agent tasks.

The runtime owns physical execution concerns only: task identity, leases,
asyncio task creation, cancellation, lifecycle state and optional durable
state. It knows nothing about prompts, tools or session commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from itertools import count
from pathlib import Path
from typing import Mapping, Optional

from krelcode.contracts.application import ApplicationRequest
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
from krelcode.core.messages import ImageRef
from krelcode.core.redact import SecretRedactor

RUNTIME_TASK_STATE_VERSION = 1


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
    """Single-process task runtime with optional durable state."""

    def __init__(
        self,
        *,
        lease_seconds: float = 3600.0,
        owner: Optional[str] = None,
        state_path: Optional[Path] = None,
        redactor: Optional[SecretRedactor] = None,
        autoload: bool = True,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        self.lease_seconds = float(lease_seconds)
        self.owner = owner or f"local:{os.getpid()}:{id(self):x}"
        self.state_path = Path(state_path) if state_path is not None else None
        self.redactor = redactor
        self._states: dict[str, RuntimeTaskState] = {}
        self._task_specs: dict[str, RuntimeTask] = {}
        self._tasks: dict[str, asyncio.Task[str]] = {}
        self._handles: dict[str, LocalRuntimeTaskHandle] = {}
        self._active_task_id = ""
        self._closed = False
        self.last_persist_error = ""
        self.recovered_task_ids: tuple[str, ...] = ()
        self._counter = count(1)
        if autoload:
            self._load()

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
        self._task_specs[task_id] = task
        state = self._new_queued_state(task_id, task.turn_id)
        self._states[task_id] = state
        self._persist()
        return self._launch(task_id, task, executor)

    async def recover(
        self,
        task_id: str,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle:
        """Re-launch an interrupted task from its persisted specification."""
        if self._closed:
            raise RuntimeError("LocalTaskRuntime 已关闭")
        if self._active_task_id:
            raise RuntimeError("已有任务正在运行")
        task = self._task_specs.get(task_id)
        if task is None:
            raise KeyError(f"未知 runtime task: {task_id}")
        state = self.inspect(task_id)
        if state.status is not RuntimeTaskStatus.INTERRUPTED:
            raise RuntimeError(
                f"只有 interrupted task 可恢复，当前为 {state.status.value}")
        new_state = self._new_queued_state(task_id, state.turn_id)
        self._states[task_id] = RuntimeTaskState(
            task_id=new_state.task_id,
            turn_id=new_state.turn_id,
            status=new_state.status,
            lease=new_state.lease,
            checkpoint=state.checkpoint,
            sequence=state.sequence + 1,
            error="",
            updated_at=new_state.updated_at,
        )
        self._persist()
        return self._launch(task_id, task, executor)

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
        self._persist()
        return updated

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        active = self._active_task_id
        if active:
            await self.cancel(active)
        self._active_task_id = ""
        self._persist()

    def _new_queued_state(self, task_id: str, turn_id: str) -> RuntimeTaskState:
        now = time.time()
        return RuntimeTaskState(
            task_id=task_id,
            turn_id=turn_id,
            status=RuntimeTaskStatus.QUEUED,
            lease=RuntimeLease(
                lease_id=f"lease-{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                owner=self.owner,
                acquired_at=now,
                expires_at=now + self.lease_seconds,
            ),
            sequence=1,
            updated_at=now,
        )

    def _launch(
        self,
        task_id: str,
        task: RuntimeTask,
        executor: RuntimeTaskExecutorPort,
    ) -> LocalRuntimeTaskHandle:
        async def run() -> str:
            state = self.inspect(task_id)
            self._transition(
                task_id,
                RuntimeTaskStatus.RUNNING,
                lease=state.lease,
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
        self._persist()

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, Mapping):
            return
        if raw.get("version") != RUNTIME_TASK_STATE_VERSION:
            return
        records = raw.get("tasks")
        if not isinstance(records, Mapping):
            return
        recovered: list[str] = []
        maximum = 0
        for raw_id, record in records.items():
            if not isinstance(record, Mapping):
                continue
            try:
                task = _task_from_mapping(record.get("task"))
                state = _state_from_mapping(record.get("state"))
            except (TypeError, ValueError, KeyError):
                continue
            task_id = str(raw_id)
            if state.task_id != task_id:
                continue
            maximum = max(maximum, _task_number(task_id))
            if not state.status.terminal:
                state = RuntimeTaskState(
                    task_id=state.task_id,
                    turn_id=state.turn_id,
                    status=RuntimeTaskStatus.INTERRUPTED,
                    lease=None,
                    checkpoint=state.checkpoint,
                    sequence=state.sequence + 1,
                    error="runtime restarted before task completed",
                    updated_at=time.time(),
                )
                recovered.append(task_id)
            self._task_specs[task_id] = task
            self._states[task_id] = state
        self._counter = count(maximum + 1)
        self.recovered_task_ids = tuple(sorted(recovered))
        if recovered:
            self._persist()

    def _persist(self) -> bool:
        if self.state_path is None:
            return True
        payload = {
            "version": RUNTIME_TASK_STATE_VERSION,
            "owner": self.owner,
            "updated_at": time.time(),
            "tasks": {
                task_id: {
                    "task": _task_to_mapping(self._task_specs[task_id]),
                    "state": _state_to_mapping(state),
                }
                for task_id, state in self._states.items()
                if task_id in self._task_specs
            },
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(
                self.state_path.suffix + ".tmp")
            line = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if self.redactor is not None:
                line = self.redactor.redact(line)
            temporary.write_text(line + "\n", encoding="utf-8")
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            temporary.replace(self.state_path)
        except (OSError, TypeError, ValueError) as exc:
            self.last_persist_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_persist_error = ""
        return True


def _task_number(task_id: str) -> int:
    suffix = task_id.rsplit("-", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


def _task_to_mapping(task: RuntimeTask) -> dict[str, object]:
    return {
        "turn_id": task.turn_id,
        "message": task.message,
        "application": task.application.to_dict(),
        "images": [
            {
                "media_type": image.media_type,
                "data_b64": image.data_b64,
                "source_path": image.source_path,
            }
            for image in task.images
        ],
        "tool_allowlist": (
            list(task.tool_allowlist)
            if task.tool_allowlist is not None else None
        ),
        "reminder_ids": list(task.reminder_ids),
        "metadata": dict(task.metadata),
    }


def _task_from_mapping(raw: object) -> RuntimeTask:
    if not isinstance(raw, Mapping):
        raise TypeError("runtime task 记录缺失")
    application = raw.get("application")
    if not isinstance(application, Mapping):
        raise TypeError("runtime task application 缺失")
    images_raw = raw.get("images")
    images = tuple(
        ImageRef(
            media_type=str(item.get("media_type") or ""),
            data_b64=str(item.get("data_b64") or ""),
            source_path=(
                str(item["source_path"])
                if item.get("source_path") is not None else None
            ),
        )
        for item in (images_raw if isinstance(images_raw, list) else ())
        if isinstance(item, Mapping)
    )
    allowlist_raw = raw.get("tool_allowlist")
    metadata_raw = raw.get("metadata")
    return RuntimeTask(
        turn_id=str(raw.get("turn_id") or ""),
        message=str(raw.get("message") or ""),
        application=ApplicationRequest.from_mapping(application),
        images=images,
        tool_allowlist=(
            tuple(str(item) for item in allowlist_raw)
            if isinstance(allowlist_raw, list) else None
        ),
        reminder_ids=tuple(
            str(item) for item in (raw.get("reminder_ids") or ())
            if isinstance(item, (str, int, float))
        ),
        metadata=(
            dict(metadata_raw)
            if isinstance(metadata_raw, Mapping) else {}
        ),
    )


def _checkpoint_to_mapping(
    checkpoint: RuntimeCheckpointRef,
) -> dict[str, object]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "kind": checkpoint.kind,
        "session_id": checkpoint.session_id,
        "turn_id": checkpoint.turn_id,
        "task_id": checkpoint.task_id,
        "digest": checkpoint.digest,
        "path": checkpoint.path,
        "metadata": dict(checkpoint.metadata),
    }


def _checkpoint_from_mapping(raw: object) -> Optional[RuntimeCheckpointRef]:
    if not isinstance(raw, Mapping):
        return None
    metadata_raw = raw.get("metadata")
    return RuntimeCheckpointRef(
        checkpoint_id=str(raw.get("checkpoint_id") or ""),
        kind=str(raw.get("kind") or "context"),
        session_id=str(raw.get("session_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        digest=str(raw.get("digest") or ""),
        path=str(raw.get("path") or ""),
        metadata=(
            dict(metadata_raw)
            if isinstance(metadata_raw, Mapping) else {}
        ),
    )


def _state_to_mapping(state: RuntimeTaskState) -> dict[str, object]:
    lease = None
    if state.lease is not None:
        lease = {
            "lease_id": state.lease.lease_id,
            "task_id": state.lease.task_id,
            "owner": state.lease.owner,
            "acquired_at": state.lease.acquired_at,
            "expires_at": state.lease.expires_at,
        }
    return {
        "task_id": state.task_id,
        "turn_id": state.turn_id,
        "status": state.status.value,
        "lease": lease,
        "checkpoint": (
            _checkpoint_to_mapping(state.checkpoint)
            if state.checkpoint is not None else None
        ),
        "sequence": state.sequence,
        "error": state.error,
        "updated_at": state.updated_at,
    }


def _state_from_mapping(raw: object) -> RuntimeTaskState:
    if not isinstance(raw, Mapping):
        raise TypeError("runtime task state 缺失")
    lease_raw = raw.get("lease")
    lease = None
    if isinstance(lease_raw, Mapping):
        lease = RuntimeLease(
            lease_id=str(lease_raw.get("lease_id") or ""),
            task_id=str(lease_raw.get("task_id") or ""),
            owner=str(lease_raw.get("owner") or ""),
            acquired_at=float(lease_raw.get("acquired_at") or 0.0),
            expires_at=float(lease_raw.get("expires_at") or 0.0),
        )
    return RuntimeTaskState(
        task_id=str(raw.get("task_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        status=RuntimeTaskStatus(str(raw.get("status") or "interrupted")),
        lease=lease,
        checkpoint=_checkpoint_from_mapping(raw.get("checkpoint")),
        sequence=int(raw.get("sequence") or 0),
        error=str(raw.get("error") or ""),
        updated_at=float(raw.get("updated_at") or 0.0),
    )


__all__ = [
    "LocalRuntimeTaskHandle",
    "LocalTaskRuntime",
    "RUNTIME_TASK_STATE_VERSION",
]
