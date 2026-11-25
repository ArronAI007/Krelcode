"""Event-bus integration that records start/end context checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, cast

from krelcode.agent.context.context_checkpoint_event import checkpoint_created_event
from krelcode.agent.context.context_checkpoint_models import (
    ContextCheckpoint,
    MemoryReference,
    WorkspaceFingerprint,
    collect_tracked_files,
    hash_tracked_files,
)
from krelcode.agent.context.context_checkpoint_ports import (
    CheckpointLoopPort,
    CompactStorePort,
    MemoryStorePort,
    WorkingContextPort,
)
from krelcode.agent.context.context_checkpoint_store import ContextCheckpointStore
from krelcode.agent.context.context_checkpoint_workspace import workspace_fingerprint
from krelcode.agent.context.context_plan import ContextPlanSnapshot
from krelcode.agent.context.working_context import WorkingContextStore
from krelcode.core.events import (
    AgentEvent,
    ContextAssembled,
    EventBus,
    PlanUpdated,
    RunCancelled,
    RunFinished,
    RunStarted,
)
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor


class ContextCheckpointCoordinator:
    """Event-bus sidecar that records start/end context checkpoints."""

    def __init__(
        self,
        *,
        loop: CheckpointLoopPort,
        store: ContextCheckpointStore,
        compact_store: Optional[CompactStorePort] = None,
        memory_store: Optional[MemoryStorePort] = None,
        redactor: Optional[SecretRedactor] = None,
        log: Optional[Callable[[str], None]] = None,
        capture_start: bool = False,
        capture_workspace: bool = True,
    ) -> None:
        self.loop = loop
        self.store = store
        self.compact_store = compact_store
        self.memory_store = memory_store
        self.redactor = redactor
        self.log = log
        self.capture_start = bool(capture_start)
        self.capture_workspace = bool(capture_workspace)
        self._unsub: Optional[Callable[[], None]] = None
        self._last_plan: dict[str, object] = {}
        self._active_session_id = ""

    def attach(self, bus: EventBus) -> None:
        if self._unsub is None:
            self._unsub = bus.subscribe(self.on_event)

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, PlanUpdated):
            self._last_plan = {
                "items": list(event.items),
                "statuses": list(event.statuses),
                "active_index": event.active_index,
                "explanation": event.explanation,
                "turn_id": event.turn_id,
            }
        elif isinstance(event, ContextAssembled):
            plan = self.loop.last_context_plan
            if plan is not None:
                self._last_plan["context_plan"] = plan.as_dict()
        elif isinstance(event, RunStarted):
            session_id = event.session_id or self._session_id()
            self._active_session_id = session_id
            if (
                self.capture_start
                and self.store.latest_for_session(session_id) is None
            ):
                history = list(self.loop.history)
                if (
                    history and history[-1].role == "user"
                    and (history[-1].content or "") == event.user_input
                ):
                    history.pop()
                await asyncio.to_thread(
                    self._capture,
                    history, session_id=session_id,
                    turn_id=event.turn_id, phase="start")
        elif isinstance(event, (RunFinished, RunCancelled)):
            history = list(self.loop.history)
            checkpoint = await asyncio.to_thread(
                self._capture,
                history,
                session_id=(
                    getattr(event, "session_id", "")
                    or self._active_session_id
                    or self._session_id()
                ),
                turn_id=event.turn_id,
                phase="cancelled" if isinstance(event, RunCancelled) else "end",
            )
            if checkpoint is not None:
                await self.loop.bus.emit(checkpoint_created_event(checkpoint))
            self._active_session_id = ""

    def _capture(self, history: Sequence[Message],
        *,
        session_id: str,
        turn_id: str,
        phase: str,
    ) -> Optional[ContextCheckpoint]:
        root = Path(self.loop.tool_context.workspace_root)
        plan = (
            self.loop.last_context_plan
            if self.loop.last_context_plan is not None
            else None
        )
        working: WorkingContextPort
        if phase == "start":
            redactor = self.loop.services.core.redactor
            scratch = WorkingContextStore(
                root,
                redactor=redactor.redact if redactor is not None else None,
            )
            working = scratch.rebuild_from_history(history)
        else:
            working = self.loop.working_context_store.snapshot()
        tracked = collect_tracked_files(root, history, working)
        file_hashes = hash_tracked_files(tracked, root)
        query = _memory_query(history)
        memory_refs = []
        if self.memory_store is not None and query:
            for entry in self.memory_store.search(query, limit=8):
                text = str(getattr(entry, "text", "") or "")
                memory_refs.append(MemoryReference(
                    id=str(getattr(entry, "id", "") or ""),
                    digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    scope=str(getattr(
                        getattr(entry, "scope", ""), "value",
                        getattr(entry, "scope", "")) or ""),
                    kind=str(getattr(
                        getattr(entry, "kind", ""), "value",
                        getattr(entry, "kind", "")) or ""),
                ))
        if self.capture_workspace:
            rule_loader = self.loop.services.context.rule_loader
            rules_content = ""
            rules_sources: list[str] = []
            if rule_loader is not None:
                try:
                    bundle = rule_loader.load_base()
                    rules_content = bundle.content
                    rules_sources = [
                        source.display_path for source in bundle.sources
                        if not source.imported
                    ]
                except Exception:
                    pass
            workspace = workspace_fingerprint(
                root, rules_content=rules_content, rules_sources=rules_sources)
        else:
            # Subagent checkpoints are scoped to their private sidechain. Their
            # parent workspace is already fingerprinted by the main session;
            # rescanning it from every child would contend on the event loop
            # and turn parallel children into serialized Git/tree scans.
            workspace = WorkspaceFingerprint(captured_at=time.time())
        compact = self.loop.last_compact_checkpoint
        compact_summary: Mapping[str, object] = {}
        if compact is not None:
            try:
                compact_summary = compact.summary.as_dict()
            except AttributeError:
                compact_summary = {}
        manager = self.loop.context_manager
        if not compact_summary and manager is not None:
            summary = getattr(manager, "last_summary", None)
            if summary is not None:
                compact_summary = summary.as_dict()
        if plan is None:
            plan = ContextPlanSnapshot()
        plan = plan.restored_for(history, str(getattr(compact, "id", "") or ""))
        checkpoint = self.store.record(
            history=history,
            session_id=session_id,
            turn_id=turn_id,
            phase=phase,
            working_context=working.as_checkpoint_record(),
            file_hashes=file_hashes,
            workspace=workspace,
            compact_checkpoint_id=str(getattr(compact, "id", "") or ""),
            compact_summary=compact_summary,
            memory_query=query,
            memory_refs=memory_refs,
            context_plan=plan,
            plan=self._last_plan,
        )
        if checkpoint is None and self.log is not None:
            self.log("[context] checkpoint 写入失败（fail-open）")
        return checkpoint

    def _session_id(self) -> str:
        runtime = self.loop.tool_context.runtime
        return str(runtime.session_id or "")


def _memory_query(history: Sequence[Message]) -> str:
    for message in reversed(history):
        if message.role != "user":
            continue
        text = " ".join(str(message.content or "").split())
        if not text or text.startswith("[上下文摘要]") or text.startswith("[用户插话]"):
            continue
        return text[:2_000]
    return ""



__all__ = ["ContextCheckpointCoordinator"]
