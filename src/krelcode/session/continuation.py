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
        bundle = self._save_state_bundle(checkpoint, result, task_state)
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
            bundle = self._save_state_bundle(checkpoint, restored, task_state)
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
            },
        )
        commit = getattr(self.state_checkpoints, "commit", None)
        if callable(commit):
            try:
                return cast(StateCheckpointBundle, commit(bundle))
            except Exception:
                return None
        return bundle if self.state_checkpoints.save(bundle) else None


__all__ = ["SessionContinuationAdapter", "SessionContinuationError"]
