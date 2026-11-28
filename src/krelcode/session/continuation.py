"""Session continuation adapter for runtime checkpoint references."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, cast

from ..agent.context.context_checkpoint import (
    ContextCheckpoint,
    ContextCheckpointStore,
    restore_context_checkpoint,
)
from ..agent.context.context_plan import history_digest
from ..agent.context.context_checkpoint_ports import (
    CheckpointLoopPort,
    CompactStorePort,
    MemoryStorePort,
)
from ..contracts.runtime import (
    RuntimeCheckpointRef,
    RuntimeContinuationPort,
    RuntimeContinuationResult,
    RuntimeTaskRuntimePort,
    RuntimeTaskState,
)
from ..contracts.state import StateCheckpointBundle, StateCheckpointStorePort
from ..contracts.task_lifecycle import TaskContinuation
from ..agent.context.context_checkpoint_workspace import workspace_fingerprint
from .restore import refresh_skill_system_prompt, restore_session
from .state_checkpoint import LocalStateCheckpointStore


class SessionContinuationError(RuntimeError):
    """A checkpoint could not be mapped to the live session state."""


class SessionContinuationAdapter(RuntimeContinuationPort):
    """Apply persisted context checkpoints to one live Harness session."""

    def __init__(
        self,
        loop: CheckpointLoopPort,
        *,
        context_checkpoints: Optional[ContextCheckpointStore] = None,
        compact_checkpoints: Optional[CompactStorePort] = None,
        memory_store: Optional[MemoryStorePort] = None,
        task_runtime: Optional[RuntimeTaskRuntimePort] = None,
        state_checkpoints: Optional[StateCheckpointStorePort] = None,
    ) -> None:
        self.loop = loop
        services = loop.services
        self.context_checkpoints = (
            context_checkpoints or services.context.checkpoint)
        self.compact_checkpoints = (
            compact_checkpoints or services.context.compact_checkpoint)
        self.memory_store = memory_store or services.session.memory
        self.task_runtime = task_runtime
        self.state_checkpoints = (
            state_checkpoints
            or LocalStateCheckpointStore(
                loop.tool_context.workspace_root,
                redactor=loop.services.core.redactor,
            )
        )

    def resume(
        self,
        checkpoint: RuntimeCheckpointRef,
        *,
        refresh_skills: bool = True,
    ) -> RuntimeContinuationResult:
        restored_checkpoint = self._load(checkpoint)
        if restored_checkpoint is None:
            raise SessionContinuationError(
                f"找不到 continuation checkpoint: {checkpoint.checkpoint_id}")
        result = restore_context_checkpoint(
            self.loop,
            restored_checkpoint,
            compact_store=self.compact_checkpoints,
            memory_store=self.memory_store,
        )
        if refresh_skills:
            refresh_skill_system_prompt(self.loop)
        task_state = self._record_applied(checkpoint)
        continuation = self._continuation(
            checkpoint, result, task_state)
        record_continuation = (
            getattr(self.task_runtime, "record_continuation", None)
            if self.task_runtime is not None else None
        )
        if checkpoint.task_id and callable(record_continuation):
            try:
                task_state = record_continuation(
                    checkpoint.task_id, continuation)
            except KeyError:
                pass
        bundle = self._save_state_bundle(
            checkpoint, result, task_state, continuation)
        return RuntimeContinuationResult(
            checkpoint_id=result.checkpoint_id,
            session_id=result.session_id,
            turn_id=result.turn_id,
            history_messages=result.history_messages,
            compact_checkpoint_id=result.compact_checkpoint_id,
            working_context_entries=result.working_context_entries,
            stale_files=result.integrity.stale,
            memory_ids=result.memory_ids,
            workspace_refresh_pending=result.workspace_refresh_pending,
            state=task_state,
            state_bundle_id=bundle.bundle_id if bundle is not None else "",
            continuation=continuation,
        )

    def resume_session(
        self,
        path: Path,
        *,
        log: Callable[[str], None] = print,
    ) -> Optional[RuntimeContinuationResult]:
        """Restore a JSONL session through the same continuation boundary."""
        if not restore_session(self.loop, Path(path), log=log):
            return None
        restored = self.loop.last_context_restore
        if restored is not None:
            checkpoint = RuntimeCheckpointRef(
                checkpoint_id=restored.checkpoint_id,
                kind="context",
                session_id=restored.session_id,
                turn_id=restored.turn_id,
            )
            task_state = self._record_applied(checkpoint)
            continuation = self._continuation(
                checkpoint, restored, task_state)
            record_continuation = (
                getattr(self.task_runtime, "record_continuation", None)
                if self.task_runtime is not None else None
            )
            if checkpoint.task_id and callable(record_continuation):
                try:
                    task_state = record_continuation(
                        checkpoint.task_id, continuation)
                except KeyError:
                    pass
            bundle = self._save_state_bundle(
                checkpoint, restored, task_state, continuation)
            return RuntimeContinuationResult(
                checkpoint_id=restored.checkpoint_id,
                session_id=restored.session_id,
                turn_id=restored.turn_id,
                history_messages=restored.history_messages,
                compact_checkpoint_id=restored.compact_checkpoint_id,
                working_context_entries=restored.working_context_entries,
                stale_files=restored.integrity.stale,
                memory_ids=restored.memory_ids,
                workspace_refresh_pending=restored.workspace_refresh_pending,
                state=task_state,
                state_bundle_id=bundle.bundle_id if bundle is not None else "",
                continuation=continuation,
            )
        return RuntimeContinuationResult(
            checkpoint_id="",
            session_id=Path(path).stem,
            history_messages=len(self.loop.history),
            workspace_refresh_pending=bool(self.loop.pending_context_refresh),
        )

    def _load(
        self,
        checkpoint: RuntimeCheckpointRef,
    ) -> Optional[ContextCheckpoint]:
        if self.context_checkpoints is None:
            return None
        loaded = self.context_checkpoints.load(checkpoint.checkpoint_id)
        if loaded is not None:
            return loaded
        if checkpoint.path:
            candidate = Path(checkpoint.path)
            if candidate.is_file():
                identifier = candidate.stem
                return self.context_checkpoints.load(identifier)
        return None

    def _record_applied(
        self,
        checkpoint: RuntimeCheckpointRef,
    ) -> Optional[RuntimeTaskState]:
        if self.task_runtime is None or not checkpoint.task_id:
            return None
        try:
            return self.task_runtime.record_checkpoint(
                checkpoint.task_id, checkpoint)
        except KeyError:
            return None

    def _save_state_bundle(
        self,
        checkpoint: RuntimeCheckpointRef,
        result: object,
        task_state: Optional[RuntimeTaskState],
        continuation: Optional[TaskContinuation] = None,
    ) -> Optional[StateCheckpointBundle]:
        plan = self.loop.last_context_plan
        bundle_id = f"state-{checkpoint.checkpoint_id}"
        state_ref = None
        if self.task_runtime is not None and checkpoint.task_id:
            try:
                state_ref = self.task_runtime.state_ref(checkpoint.task_id)
            except KeyError:
                state_ref = None
        working_sequence = int(
            getattr(
                self.loop.working_context_store.snapshot(),
                "sequence",
                0,
            ) or 0
        )
        resolved_history_digest = history_digest(self.loop.history)
        event_sequence = 0
        event_digest = ""
        recorder = getattr(
            self.loop.services.session, "recorder", None)
        event_log = getattr(recorder, "event_log", None)
        if event_log is not None:
            event_sequence = int(
                getattr(event_log, "latest_sequence", 0) or 0)
            if event_sequence > 0:
                entries = event_log.read(
                    after_sequence=event_sequence - 1,
                    limit=1,
                )
                if entries:
                    event_digest = entries[-1].digest
        bundle = StateCheckpointBundle(
            bundle_id=bundle_id,
            session_id=str(getattr(result, "session_id", "") or ""),
            turn_id=str(getattr(result, "turn_id", "") or ""),
            task_id=checkpoint.task_id,
            context_checkpoint_id=checkpoint.checkpoint_id,
            compact_checkpoint_id=str(
                getattr(result, "compact_checkpoint_id", "") or ""),
            working_context_sequence=working_sequence,
            history_digest=resolved_history_digest,
            event_sequence=event_sequence,
            event_digest=event_digest,
            plan=(
                plan.as_dict()
                if plan is not None and hasattr(plan, "as_dict") else {}
            ),
            runtime_state=task_state,
            state_ref=state_ref,
            components={
                "context": {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "working_context_sequence": working_sequence,
                },
                "compact": {
                    "checkpoint_id": str(
                        getattr(result, "compact_checkpoint_id", "") or ""),
                },
                "history": {
                    "messages": int(
                        getattr(result, "history_messages", 0) or 0),
                    "digest": resolved_history_digest,
                },
                "event_log": {
                    "sequence": event_sequence,
                    "digest": event_digest,
                },
                "memory": {
                    "ids": list(
                        getattr(result, "memory_ids", ()) or ()),
                },
                "workspace": {
                    "stale_files": list(
                        getattr(result, "stale_files", ()) or ()),
                    "refresh_pending": bool(
                        getattr(
                            result,
                            "workspace_refresh_pending",
                            True,
                        )
                    ),
                },
                "runtime": {
                    "state_ref": (
                        {
                            "state_id": state_ref.state_id,
                            "sequence": state_ref.sequence,
                            "checkpoint_id": (
                                state_ref.checkpoint.checkpoint_id
                                if state_ref.checkpoint is not None else ""
                            ),
                        }
                        if state_ref is not None else None
                    ),
                },
                "agent_contract": (
                    self.loop.agent_contract.to_dict()
                    if self.loop.agent_contract is not None
                    else {}
                ),
                "continuation": (
                    continuation.to_dict()
                    if continuation is not None else {}
                ),
            },
        )
        commit = getattr(self.state_checkpoints, "commit", None)
        if callable(commit):
            try:
                return cast(StateCheckpointBundle, commit(bundle))
            except Exception:
                return None
        return bundle if self.state_checkpoints.save(bundle) else None

    def _continuation(
        self,
        checkpoint: RuntimeCheckpointRef,
        result: object | None,
        task_state: Optional[RuntimeTaskState],
    ) -> TaskContinuation:
        application = self.loop.application_request
        goal = str(application.goal if application is not None else "")
        tool_context = self.loop.tool_context
        plan_control = tool_context.plan_control
        plan = (
            plan_control.as_dict()
            if plan_control is not None and hasattr(plan_control, "as_dict")
            else {}
        )

        snapshot = self.loop.working_context_store.snapshot()
        confirmed = tuple(
            entry.content
            for entry in snapshot.entries
            if entry.status == "active" and entry.content
        )
        failed = tuple(
            f"{entry.kind}:{entry.content}"
            for entry in snapshot.entries
            if entry.status in {"failed", "error", "blocked"}
        )
        artifacts = tuple(
            {
                "id": str(
                    entry.metadata.get("artifact_id") or entry.id),
                "kind": entry.kind,
                "source": entry.source,
            }
            for entry in snapshot.entries
            if entry.metadata.get("artifact_id")
        )
        waiting: tuple[str, ...] = ()
        if task_state is not None and (
            task_state.wait_reason or task_state.wait_kind
        ):
            waiting = (
                f"{task_state.wait_kind}:{task_state.wait_reason}".rstrip(":"),
            )
        budget = None
        ledger = self.loop.budget_ledger
        if ledger is not None and hasattr(ledger, "remaining_task_budget"):
            budget = ledger.remaining_task_budget()
        if budget is None and application is not None:
            budget = application.budget
        permission_mode = ""
        permission_mode = tool_context.runtime.permission_mode.value
        workspace = workspace_fingerprint(
            self.loop.tool_context.workspace_root).as_dict()
        prompt_plan = self.loop.last_prompt_plan
        instruction_ids = tuple(
            fragment.id for fragment in (
                prompt_plan.fragments if prompt_plan is not None else ())
            if fragment.metadata.get("instruction")
        )
        stored = self._stored_continuation(checkpoint.task_id)
        return TaskContinuation(
            objective=goal or stored.objective,
            plan=plan or dict(stored.plan),
            confirmed_facts=confirmed or stored.confirmed_facts,
            failed_attempts=failed or stored.failed_attempts,
            artifacts=artifacts or stored.artifacts,
            waiting_items=waiting or stored.waiting_items,
            remaining_budget=budget or stored.remaining_budget,
            permission_mode=permission_mode or stored.permission_mode,
            workspace_version=workspace or dict(stored.workspace_version),
            recovery_checkpoint_id=checkpoint.checkpoint_id,
            instruction_ids=instruction_ids or stored.instruction_ids,
            metadata={
                "session_id": str(
                    getattr(result, "session_id", "") or ""),
                "turn_id": str(getattr(result, "turn_id", "") or ""),
            },
        )

    def capture_current(
        self,
        *,
        task_id: str,
        checkpoint_id: str = "",
    ) -> TaskContinuation:
        """Capture the semantic continuation without restoring a checkpoint."""
        return self._continuation(
            RuntimeCheckpointRef(
                checkpoint_id=checkpoint_id,
                kind="context",
                task_id=task_id,
            ),
            None,
            (
                self.task_runtime.inspect(task_id)
                if self.task_runtime is not None and task_id
                else None
            ),
        )

    def _stored_continuation(self, task_id: str) -> TaskContinuation:
        if not task_id:
            return TaskContinuation(objective="")
        latest = None
        latest_for_task = getattr(
            self.state_checkpoints, "latest_for_task", None)
        if callable(latest_for_task):
            latest = latest_for_task(task_id)
        if latest is not None and latest.runtime_state is not None:
            return latest.runtime_state.continuation or TaskContinuation(
                objective="")
        return TaskContinuation(objective="")


__all__ = ["SessionContinuationAdapter", "SessionContinuationError"]
