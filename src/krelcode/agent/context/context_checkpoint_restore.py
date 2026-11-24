"""Apply persisted full-context checkpoints to a live loop."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence, cast

from krelcode.agent.context.context_checkpoint_models import (
    ContextCheckpoint,
    ContextRestoreResult,
)
from krelcode.agent.context.context_checkpoint_ports import (
    CheckpointLoopPort,
    CompactStorePort,
    MemoryEntryPort,
    MemoryStorePort,
)
from krelcode.agent.context.context_plan import ContextPlanSnapshot
from krelcode.agent.context.memory_context_render import render_memory_context
from krelcode.core.messages import Message


def restore_context_checkpoint(
    loop: CheckpointLoopPort,
    checkpoint: ContextCheckpoint,
    *,
    compact_store: Optional[CompactStorePort] = None,
    memory_store: Optional[MemoryStorePort] = None,
    history_override: Optional[Sequence[Message]] = None,
) -> ContextRestoreResult:
    """Apply one checkpoint to a loop, refreshing volatile project facts."""
    restored = copy.deepcopy(list(
        history_override if history_override is not None else checkpoint.history))
    # Keep the current base/system shell. Workspace/rules/git text is refreshed
    # from live sources before the next provider request.
    current = loop.history
    if current and current[0].role == "system":
        system = copy.deepcopy(current[0])
        if restored and restored[0].role == "system":
            restored[0] = system
        else:
            restored.insert(0, system)
    loop.update_runtime_state(history=restored)

    compact = None
    if compact_store is not None and checkpoint.compact_checkpoint_id:
        compact = compact_store.load(checkpoint.compact_checkpoint_id)
    summary = getattr(compact, "summary", None) if compact is not None else None
    if summary is None and checkpoint.compact_summary:
        try:
            from krelcode.agent.context.compact_summary import CompactSummary
            summary = CompactSummary.from_dict(checkpoint.compact_summary)
        except (TypeError, ValueError):
            summary = None
    if compact is not None:
        loop.update_runtime_state(last_compact_checkpoint=compact)
    manager = loop.context_manager
    if manager is not None and summary is not None:
        manager.restore_summary(summary)

    working_store = loop.working_context_store
    if checkpoint.working_context.get("entries"):
        snapshot = working_store.restore_snapshot(checkpoint.working_context)
        if loop.context_manager is not None:
            loop.context_manager.rebuild_state(loop.history)
    else:
        snapshot = loop.rebuild_working_context()
    integrity = checkpoint.verify_files(Path(loop.tool_context.workspace_root))
    if integrity.stale:
        loop.working_context_store.invalidate_files(
            integrity.stale, reason="resume-file-hash-mismatch")
        snapshot = loop.working_context_store.snapshot()

    relevant: Sequence[MemoryEntryPort] = ()
    memory_query = checkpoint.memory_query.strip()
    if memory_store is not None and memory_query:
        relevant = memory_store.search(memory_query, limit=8)
    rendered_memory = render_memory_context(relevant)
    loop.update_runtime_state(
        restored_memory_context=(rendered_memory,) if rendered_memory else (),
        restored_relevant_memory_ids=tuple(entry.id for entry in relevant),
        restored_context_checkpoint_id=checkpoint.id, context_integrity=integrity,
        context_workspace_fingerprint=None, restored_workspace_fingerprint=checkpoint.workspace,
    )

    plan = checkpoint.context_plan or ContextPlanSnapshot()
    previous_ledger = plan.cache_ledger
    invalidations = ["workspace-refresh"]
    if integrity.stale:
        invalidations.append("file-integrity-changed")
    restored_memory_ids = tuple(entry.id for entry in relevant)
    if (
        memory_query
        and previous_ledger.memory_evidence_ids != restored_memory_ids
    ):
        invalidations.append("memory-selection-refreshed")
    if (
        checkpoint.working_context.get("entries")
        and snapshot is not None
        and previous_ledger.working_context_sequence != snapshot.sequence
    ):
        invalidations.append("working-context-restored")
    plan = plan.restored_for(
        restored,
        checkpoint.id,
        working_context_sequence=(
            snapshot.sequence if snapshot is not None else 0),
        invalidations=invalidations,
    )
    loop.update_runtime_state(
        last_context_plan=plan,
        last_context_cache_ledger=(
            plan.cache_ledger if plan.cache_ledger.has_request_identity else None),
        previous_context_cache_ledger=None, pending_context_refresh=True,
    )

    result = ContextRestoreResult(
        checkpoint_id=checkpoint.id,
        session_id=checkpoint.session_id,
        turn_id=checkpoint.turn_id,
        history_messages=len(restored),
        compact_checkpoint_id=checkpoint.compact_checkpoint_id,
        working_context_entries=(snapshot.count if snapshot is not None else 0),
        integrity=integrity,
        memory_ids=restored_memory_ids,
        workspace_refresh_pending=True,
        context_plan=plan,
    )
    loop.update_runtime_state(last_context_restore=result)
    _refresh_now_if_possible(loop)
    loop.update_runtime_state(last_context_restore=replace(
        result,
        workspace_refresh_pending=bool(loop.pending_context_refresh),
    ))
    return cast(ContextRestoreResult, loop.last_context_restore)



def _refresh_now_if_possible(loop: CheckpointLoopPort) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(loop.refresh_workspace_context())
        except Exception:
            # The next AgentLoop turn retries; stale context is never injected.
            loop.update_runtime_state(pending_context_refresh=True)
        return
    loop.update_runtime_state(pending_context_refresh=True)




__all__ = ["restore_context_checkpoint"]
