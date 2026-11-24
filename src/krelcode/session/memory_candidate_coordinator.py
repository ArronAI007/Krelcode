"""Event-bus integration for reviewable Auto Memory candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..agent.loop import AgentLoop
from ..core.events import (
    AgentEvent,
    EventBus,
    MemoryCandidateObserved,
    MemoryWritten,
    RunFinished,
    RunStarted,
)
from ..core.messages import Message
from ..core.redact import SecretRedactor
from .memory import MemoryStore
from .memory_candidate_extractor import MemoryCandidateExtractor, MemoryTurnSnapshot
from .memory_candidate_models import (
    DEFAULT_AUTO_THRESHOLD,
    DEFAULT_SUGGEST_THRESHOLD,
    AutoMemoryMode,
    MemoryCandidate,
    MemoryCandidateStatus,
    coerce_auto_memory_mode,
)
from .memory_candidate_queue import MemoryCandidateQueue
from .memory_models import (
    MemoryEntry,
    MemoryKind,
    MemoryScope,
    validate_memory_structure,
)
from .memory_utils import memory_texts_conflict


@dataclass(frozen=True)
class AutoMemoryReport:
    extracted: tuple[MemoryCandidate, ...] = ()
    accepted: tuple[MemoryCandidate, ...] = ()
    skipped: int = 0

    @property
    def queued_count(self) -> int:
        return len(self.extracted)


class AutoMemoryCoordinator:
    """Event-bus sidecar that turns completed turns into reviewable knowledge."""

    def __init__(
        self,
        loop: AgentLoop,
        store: MemoryStore,
        *,
        mode: AutoMemoryMode | str = AutoMemoryMode.SUGGEST,
        suggest_threshold: float = DEFAULT_SUGGEST_THRESHOLD,
        auto_threshold: float = DEFAULT_AUTO_THRESHOLD,
        queue: Optional[MemoryCandidateQueue] = None,
        extractor: Optional[MemoryCandidateExtractor] = None,
        on_memory_changed: Optional[Callable[[], object]] = None,
        log: Optional[Callable[[str], object]] = None,
    ) -> None:
        self.loop = loop
        self.store = store
        self.mode = coerce_auto_memory_mode(mode)
        self.suggest_threshold = max(0.0, min(float(suggest_threshold), 1.0))
        self.auto_threshold = max(
            self.suggest_threshold, min(float(auto_threshold), 1.0))
        self.queue = queue or MemoryCandidateQueue(store.workspace_root)
        self.extractor = extractor or MemoryCandidateExtractor()
        self.on_memory_changed = on_memory_changed
        self.log = log or (lambda _message: None)
        self._turn_input = ""
        self._turn_start = 0
        self._turn_id = ""
        self._session_id = ""
        self._unsubscribe: Optional[Callable[[], None]] = None

    def attach(self, bus: EventBus) -> Callable[[], None]:
        if self._unsubscribe is not None:
            return self._unsubscribe
        self._unsubscribe = bus.subscribe(self.on_event)
        return self._unsubscribe

    def set_redactor(self, redactor: Optional[SecretRedactor]) -> None:
        self.queue.redactor = redactor
        self.extractor.redactor = redactor

    def set_mode(self, mode: AutoMemoryMode | str) -> AutoMemoryMode:
        self.mode = coerce_auto_memory_mode(mode)
        return self.mode

    async def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            self._turn_input = event.user_input
            self._turn_id = event.turn_id
            self._session_id = event.session_id
            history_now = self.loop.history
            self._turn_start = max(0, len(history_now) - 1)
            return
        if not isinstance(event, RunFinished):
            return
        turn_history = tuple(self.loop.history[self._turn_start:])
        try:
            report = self.process_turn(
                self._turn_input,
                turn_history,
                turn_id=event.turn_id or self._turn_id,
                session_id=self._session_id,
            )
            for candidate in report.extracted:
                await self.loop.services.core.bus.emit(MemoryCandidateObserved(
                    candidate_id=candidate.id,
                    status=candidate.status_value,
                    trigger=candidate.trigger_value,
                    confidence=candidate.confidence,
                    conflict_count=len(candidate.conflict_ids),
                    digest=candidate.digest,
                ))
            for candidate in report.accepted:
                await self.loop.services.core.bus.emit(MemoryWritten(
                    memory_id=candidate.existing_memory_id or "",
                    action="accepted",
                    scope=MemoryScope.PROJECT.value,
                    kind=MemoryKind.KNOWLEDGE.value,
                    tokens=0,
                    source="auto-memory:review",
                ))
        except Exception as exc:  # Auto memory must never break the agent loop.
            self.log(f"[memory] 自动记忆提取失败: {type(exc).__name__}: {exc}")

    def process_turn(
        self,
        user_input: str,
        messages: Iterable[Message] = (),
        *,
        turn_id: str = "",
        session_id: str = "",
    ) -> AutoMemoryReport:
        if self.mode is AutoMemoryMode.OFF:
            return AutoMemoryReport()
        turn = MemoryTurnSnapshot(
            user_input=user_input,
            messages=tuple(messages),
            turn_id=turn_id,
            session_id=session_id,
        )
        all_drafts = self.extractor.extract(turn)
        drafted = [
            item for item in all_drafts
            if item.confidence >= self.suggest_threshold
        ]
        queued: list[MemoryCandidate] = []
        accepted: list[MemoryCandidate] = []
        for draft in drafted:
            candidate, _created = self.queue.merge(draft)
            if candidate is None:
                continue
            conflicts = self._memory_conflicts(candidate)
            candidate = self.queue.set_external_conflict_ids(
                candidate.id, conflicts) or candidate
            if (
                self.mode is AutoMemoryMode.AUTO
                and candidate.status is MemoryCandidateStatus.PENDING
                and candidate.confidence >= self.auto_threshold
                and not candidate.conflict_ids
                and candidate.structurally_complete
            ):
                candidate = self.accept(candidate.id) or candidate
                if candidate.status is MemoryCandidateStatus.ACCEPTED:
                    accepted.append(candidate)
            queued.append(candidate)
        return AutoMemoryReport(
            extracted=tuple(queued),
            accepted=tuple(accepted),
            skipped=max(0, len(all_drafts) - len(drafted)),
        )

    def _memory_conflicts(self, candidate: MemoryCandidate) -> tuple[str, ...]:
        conflicts: list[str] = []
        for entry in self.store.list(limit=0):
            if memory_texts_conflict(candidate.text, entry.text):
                conflicts.append(entry.id)
        return tuple(dict.fromkeys(conflicts))

    def accept(self, candidate_id: str) -> Optional[MemoryCandidate]:
        candidate = self.queue.get(candidate_id)
        if candidate is None:
            return None
        if candidate.status is MemoryCandidateStatus.ACCEPTED:
            return candidate
        conflicts = self._memory_conflicts(candidate)
        candidate = self.queue.set_external_conflict_ids(
            candidate.id, conflicts) or candidate
        validate_memory_structure(
            candidate.memory_type, candidate.why, candidate.how_to_apply)
        entry = self.store.add(
            candidate.text,
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.KNOWLEDGE,
            memory_type=candidate.memory_type,
            topic=candidate.topic,
            why=candidate.why,
            how_to_apply=candidate.how_to_apply,
            tags=("auto-memory", candidate.trigger_value),
            source="auto-memory:review",
            metadata={
                "candidate_id": candidate.id,
                "trigger": candidate.trigger_value,
                "confidence": candidate.confidence,
                "digest": candidate.digest,
                "project_id": candidate.project_id,
                "evidence_count": len(candidate.evidence),
            },
            deduplicate=True,
        )
        accepted = self.queue.set_status(
            candidate.id,
            MemoryCandidateStatus.ACCEPTED,
            existing_memory_id=entry.id,
        )
        if self.on_memory_changed is not None:
            self.on_memory_changed()
        return accepted

    def reject(self, candidate_id: str) -> Optional[MemoryCandidate]:
        return self.queue.set_status(
            candidate_id, MemoryCandidateStatus.REJECTED)

    def pin(self, entry_id: str) -> Optional[MemoryEntry]:
        entry = self.store.pin(entry_id)
        if entry is not None and self.on_memory_changed is not None:
            self.on_memory_changed()
        return entry

    def unpin(self, entry_id: str) -> Optional[MemoryEntry]:
        entry = self.store.unpin(entry_id)
        if entry is not None and self.on_memory_changed is not None:
            self.on_memory_changed()
        return entry


__all__ = ["AutoMemoryCoordinator", "AutoMemoryReport"]
