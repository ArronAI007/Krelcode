"""Session continuation adapter for runtime checkpoint references."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..agent.context.context_checkpoint import (
    ContextCheckpoint,
    ContextCheckpointStore,
    restore_context_checkpoint,
)
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
from .restore import refresh_skill_system_prompt, restore_session


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
    ) -> None:
        self.loop = loop
        services = loop.services
        self.context_checkpoints = (
            context_checkpoints or services.context.checkpoint)
        self.compact_checkpoints = (
            compact_checkpoints or services.context.compact_checkpoint)
        self.memory_store = memory_store or services.session.memory
        self.task_runtime = task_runtime

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
                state=self._record_applied(checkpoint),
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


__all__ = ["SessionContinuationAdapter", "SessionContinuationError"]
