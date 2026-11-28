"""State mutations and reconnect operations for LocalTaskRuntime."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Mapping

from ...contracts.runtime import (
    RuntimeCheckpointRef,
    RuntimeStateRef,
    RuntimeTask,
    RuntimeTaskState,
    RuntimeTaskStatus,
)
from ...contracts.loop import WaitCondition, WaitKind
from ...contracts.task_lifecycle import (
    SideEffectStatus,
    TaskAttachment,
    TaskContinuation,
    TaskErrorClass,
    TaskEventCursor,
    TaskResultLocation,
    TaskSideEffectRecord,
    evaluate_side_effect_replay,
)
from .task_events import LocalTaskEventJournal
from .task_errors import RuntimeSideEffectRecoveryRequired


class TaskStateControlMixin:
    events: LocalTaskEventJournal
    _states: dict[str, RuntimeTaskState]

    def task(self, task_id: str) -> RuntimeTask:
        raise NotImplementedError

    def inspect(self, task_id: str) -> RuntimeTaskState:
        raise NotImplementedError

    def _persist(self) -> bool:
        raise NotImplementedError

    def side_effect_retry_error(
        self,
        task_id: str,
    ) -> RuntimeSideEffectRecoveryRequired | None:
        replay = evaluate_side_effect_replay(
            self.inspect(task_id).side_effects)
        if replay.allowed:
            return None
        return RuntimeSideEffectRecoveryRequired(replay)

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

    def checkpoint(self, task_id: str) -> RuntimeCheckpointRef | None:
        return self.inspect(task_id).checkpoint

    def record_checkpoint(
        self,
        task_id: str,
        checkpoint: RuntimeCheckpointRef,
    ) -> RuntimeTaskState:
        state = self.inspect(task_id)
        updated = replace(
            state,
            checkpoint=checkpoint,
            sequence=state.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        self._persist()
        return updated

    def record_waiting(
        self,
        task_id: str,
        condition: WaitCondition,
    ) -> RuntimeTaskState:
        state = self.inspect(task_id)
        if state.status.terminal:
            return state
        updated = replace(
            state,
            status=RuntimeTaskStatus.WAITING,
            sequence=state.sequence + 1,
            wait_reason=condition.reason,
            wait_kind=WaitKind(condition.kind).value,
            resume_token=condition.resume_token or condition.request_id,
            waiting_since=time.time(),
            resumed_at=None,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        self._persist()
        return updated

    def record_resumed(self, task_id: str) -> RuntimeTaskState:
        state = self.inspect(task_id)
        if state.status is not RuntimeTaskStatus.WAITING:
            return state
        updated = replace(
            state,
            status=RuntimeTaskStatus.RUNNING,
            sequence=state.sequence + 1,
            resumed_at=time.time(),
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        self._persist()
        return updated

    def record_event(
        self,
        task_id: str,
        payload: Mapping[str, object],
        *,
        event_type: str = "",
    ) -> TaskEventCursor:
        task = self.task(task_id)
        cursor = self.events.append(
            task_id,
            payload,
            event_type=event_type,
            parent_task_id=task.parent_task_id,
        )
        state = self.inspect(task_id)
        self._states[task_id] = replace(
            state,
            event_cursor=cursor,
            sequence=state.sequence + 1,
            updated_at=time.time(),
        )
        return cursor

    def read_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> tuple[Mapping[str, object], ...]:
        self.task(task_id)
        return tuple(self.events.read(
            task_id,
            after_sequence=after_sequence,
            limit=limit,
        ))

    def attach(self, task_id: str) -> TaskAttachment:
        state = self.inspect(task_id)
        return TaskAttachment(
            task_id=task_id,
            cursor=state.event_cursor,
            status=state.status.value,
            result=state.result,
            error=state.error_info,
            continuation=state.continuation,
            side_effects=state.side_effects,
            recovery_decision=state.recovery_decision,
            metadata={
                "parent_task_id": state.parent_task_id,
                "root_task_id": state.root_task_id,
                "creator": state.creator,
                "executor": state.executor,
                "cancel_requested": state.cancel_requested,
            },
        )

    def record_result(
        self,
        task_id: str,
        result: TaskResultLocation,
    ) -> RuntimeTaskState:
        state = self.inspect(task_id)
        updated = replace(
            state,
            result=result,
            error="",
            error_info=None,
            sequence=state.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        self._persist()
        return updated

    def record_side_effect(
        self,
        task_id: str,
        side_effect: TaskSideEffectRecord,
    ) -> RuntimeTaskState:
        state = self.inspect(task_id)
        effects = tuple(
            side_effect if item.effect_id == side_effect.effect_id else item
            for item in state.side_effects
        )
        if not any(
            item.effect_id == side_effect.effect_id
            for item in state.side_effects
        ):
            effects = (*state.side_effects, side_effect)
        updated = replace(
            state,
            side_effects=effects,
            sequence=state.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        self._persist()
        return updated

    def update_side_effect(
        self,
        task_id: str,
        effect_id: str,
        status: SideEffectStatus | str,
        *,
        compensation: str = "",
        evidence: Mapping[str, object] | None = None,
        preflight: Mapping[str, object] | None = None,
    ) -> RuntimeTaskState:
        state = self.inspect(task_id)
        effects: list[TaskSideEffectRecord] = []
        matched = False
        for item in state.side_effects:
            if item.effect_id != effect_id:
                effects.append(item)
                continue
            matched = True
            effects.append(replace(
                item,
                status=SideEffectStatus(status),
                compensation=compensation or item.compensation,
                preflight={
                    **dict(item.preflight),
                    **dict(preflight or {}),
                },
                evidence={
                    **dict(item.evidence),
                    **dict(evidence or {}),
                },
            ))
        if not matched:
            raise KeyError(f"未知 side effect: {effect_id}")
        error_info = state.error_info
        if (
            error_info is not None
            and TaskErrorClass(error_info.category)
            is TaskErrorClass.CANCELLED
        ):
            error_info = replace(
                error_info,
                details={
                    **dict(error_info.details),
                    "compensation_required": any(
                        SideEffectStatus(item.status)
                        is SideEffectStatus.COMMITTED
                        for item in effects
                    ),
                },
            )
        updated = replace(
            state,
            side_effects=tuple(effects),
            error_info=error_info,
            sequence=state.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        self._persist()
        return updated

    def record_continuation(
        self,
        task_id: str,
        continuation: TaskContinuation,
    ) -> RuntimeTaskState:
        state = self.inspect(task_id)
        updated = replace(
            state,
            continuation=continuation,
            sequence=state.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        self._persist()
        return updated


__all__ = ["TaskStateControlMixin"]
