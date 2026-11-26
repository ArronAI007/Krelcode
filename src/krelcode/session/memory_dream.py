"""Idle-time memory governance proposals (AutoDream)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Sequence, cast

from ..core.events import (
    AgentEvent,
    EventBus,
    MemoryCandidateObserved,
    RunFinished,
    RunStarted,
)
from .memory import MemoryStore
from .memory_models import MemoryEntry, MemoryKind, MemoryScope
from .memory_utils import memory_digest, memory_texts_conflict
from ..core.file_lock import InterProcessFileLock


class DreamKind(str, Enum):
    MERGE_DUPLICATES = "merge_duplicates"
    RESOLVE_CONFLICT = "resolve_conflict"
    CONSOLIDATE_TOPIC = "consolidate_topic"
    REVALIDATE_PATH = "revalidate_path"


class DreamStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class MemoryDreamProposal:
    id: str
    kind: DreamKind | str
    title: str
    rationale: str
    memory_ids: tuple[str, ...]
    proposed_text: str = ""
    topic: str = ""
    confidence: float = 0.5
    status: DreamStatus | str = DreamStatus.PENDING
    created_at: str = field(default_factory=_now)
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", DreamKind(self.kind))
        object.__setattr__(self, "status", DreamStatus(self.status))
        object.__setattr__(self, "memory_ids", tuple(dict.fromkeys(
            str(item) for item in self.memory_ids if str(item).strip())))
        object.__setattr__(
            self, "confidence", round(
                max(0.0, min(float(self.confidence), 1.0)), 2))
        object.__setattr__(
            self, "updated_at", str(self.updated_at or self.created_at))

    @property
    def fingerprint(self) -> str:
        payload = json.dumps({
            "kind": DreamKind(self.kind).value,
            "memory_ids": self.memory_ids,
            "topic": self.topic,
            "proposed_text": self.proposed_text,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": DreamKind(self.kind).value,
            "title": self.title,
            "rationale": self.rationale,
            "memory_ids": list(self.memory_ids),
            "proposed_text": self.proposed_text,
            "topic": self.topic,
            "confidence": self.confidence,
            "status": DreamStatus(self.status).value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, raw: dict[str, object]) -> "MemoryDreamProposal":
        raw_memory_ids = raw.get("memory_ids")
        return cls(
            id=str(raw.get("id") or ""),
            kind=str(raw.get("kind") or DreamKind.RESOLVE_CONFLICT.value),
            title=str(raw.get("title") or ""),
            rationale=str(raw.get("rationale") or ""),
            memory_ids=tuple(
                str(item) for item in cast(list[object], raw_memory_ids)
                if str(item).strip()
            ) if isinstance(raw_memory_ids, list) else (),
            proposed_text=str(raw.get("proposed_text") or ""),
            topic=str(raw.get("topic") or ""),
            confidence=float(str(raw.get("confidence") or 0.5)),
            status=str(raw.get("status") or DreamStatus.PENDING.value),
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or ""),
        )


class MemoryDreamPlanner:
    """Generate deterministic governance proposals from reviewed memory."""

    def plan(self, entries: Sequence[MemoryEntry]) -> tuple[MemoryDreamProposal, ...]:
        proposals: list[MemoryDreamProposal] = []
        by_digest: dict[str, list[MemoryEntry]] = {}
        by_topic: dict[str, list[MemoryEntry]] = {}
        for entry in entries:
            by_digest.setdefault(memory_digest(entry.text), []).append(entry)
            if entry.topic:
                by_topic.setdefault(entry.topic, []).append(entry)

        for duplicates in by_digest.values():
            if len(duplicates) < 2:
                continue
            proposals.append(MemoryDreamProposal(
                id=f"dream-{uuid.uuid4().hex[:12]}",
                kind=DreamKind.MERGE_DUPLICATES,
                title=f"合并 {len(duplicates)} 条重复记忆",
                rationale="规范化文本摘要完全一致",
                memory_ids=tuple(item.id for item in duplicates),
                proposed_text=duplicates[0].text,
                topic=duplicates[0].topic,
                confidence=0.95,
            ))

        for index, first in enumerate(entries):
            for second in entries[index + 1:]:
                if not memory_texts_conflict(first.text, second.text):
                    continue
                proposals.append(MemoryDreamProposal(
                    id=f"dream-{uuid.uuid4().hex[:12]}",
                    kind=DreamKind.RESOLVE_CONFLICT,
                    title="检查潜在冲突记忆",
                    rationale="两条记忆包含相互竞争或否定的事实",
                    memory_ids=(first.id, second.id),
                    topic=first.topic or second.topic,
                    confidence=0.75,
                ))

        for topic, members in by_topic.items():
            if len(members) < 3:
                continue
            proposals.append(MemoryDreamProposal(
                id=f"dream-{uuid.uuid4().hex[:12]}",
                kind=DreamKind.CONSOLIDATE_TOPIC,
                title=f"整理主题 {topic}",
                rationale=f"该主题已有 {len(members)} 条记忆，可合并为更清晰的 Topic",
                memory_ids=tuple(item.id for item in members),
                topic=topic,
                confidence=0.65,
            ))

        for entry in entries:
            missing = [
                path for path in _absolute_paths(entry.text)
                if not Path(path).exists()
            ]
            if not missing:
                continue
            proposals.append(MemoryDreamProposal(
                id=f"dream-{uuid.uuid4().hex[:12]}",
                kind=DreamKind.REVALIDATE_PATH,
                title="记忆中的路径可能已失效",
                rationale="路径当前不存在: " + ", ".join(missing[:3]),
                memory_ids=(entry.id,),
                proposed_text=entry.text,
                topic=entry.topic,
                confidence=0.8,
            ))
        return tuple(proposals)


class MemoryDreamStore:
    """Crash-safe proposal queue with revision guards."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.path = self.workspace_root / ".krelcode" / "memory" / "dream.jsonl"
        self.revision_path = self.path.with_suffix(".jsonl.revision")
        self._lock = threading.RLock()

    def revision(self) -> int:
        try:
            return max(0, int(self.revision_path.read_text(
                encoding="utf-8").strip()))
        except (OSError, ValueError):
            return 0

    def _set_revision(self, revision: int) -> None:
        self.revision_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.revision_path.with_name(
            f".{self.revision_path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            temp.write_text(f"{max(0, revision)}\n", encoding="utf-8")
            temp.replace(self.revision_path)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

    def _load(self) -> list[MemoryDreamProposal]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        latest: dict[str, MemoryDreamProposal] = {}
        for line in lines:
            try:
                raw = json.loads(line)
                if isinstance(raw, dict):
                    proposal = MemoryDreamProposal.from_record(raw)
                else:
                    continue
            except (TypeError, ValueError):
                continue
            latest[proposal.id] = proposal
        return list(latest.values())

    def _write(self, proposals: Iterable[MemoryDreamProposal]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            temp.write_text(
                "".join(
                    json.dumps(item.as_record(), ensure_ascii=False, sort_keys=True)
                    + "\n"
                    for item in proposals
                ),
                encoding="utf-8",
            )
            temp.replace(self.path)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

    def list(
        self,
        *,
        status: Optional[DreamStatus | str] = None,
        limit: int = 100,
    ) -> tuple[MemoryDreamProposal, ...]:
        selected = DreamStatus(status) if status is not None else None
        proposals = self._load()
        if selected is not None:
            proposals = [item for item in proposals if item.status is selected]
        proposals.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(proposals[:limit] if limit > 0 else proposals)

    def merge(
        self,
        proposals: Iterable[MemoryDreamProposal],
    ) -> tuple[MemoryDreamProposal, ...]:
        with self._lock, InterProcessFileLock(self.path.with_suffix(".jsonl.lock")):
            current = self._load()
            fingerprints = {item.fingerprint for item in current}
            added: list[MemoryDreamProposal] = []
            for proposal in proposals:
                if proposal.fingerprint in fingerprints:
                    continue
                fingerprints.add(proposal.fingerprint)
                current.append(proposal)
                added.append(proposal)
            if added:
                self._write(current)
                self._set_revision(self.revision() + 1)
            return tuple(added)

    def set_status(
        self,
        proposal_id: str,
        status: DreamStatus | str,
    ) -> Optional[MemoryDreamProposal]:
        normalized = DreamStatus(status)
        with self._lock, InterProcessFileLock(self.path.with_suffix(".jsonl.lock")):
            current = self._load()
            target = next(
                (item for item in current if item.id == proposal_id), None)
            if target is None:
                return None
            updated = replace(
                target, status=normalized, updated_at=_now())
            self._write([
                updated if item.id == proposal_id else item for item in current])
            self._set_revision(self.revision() + 1)
            return updated


class MemoryDreamCoordinator:
    """Run AutoDream and apply only explicitly accepted safe proposals."""

    def __init__(self, store: MemoryStore, dream_store: MemoryDreamStore) -> None:
        self.store = store
        self.dream_store = dream_store
        self.planner = MemoryDreamPlanner()

    def run(self) -> tuple[MemoryDreamProposal, ...]:
        entries = self.store.list(
            limit=0, scopes=(MemoryScope.PROJECT,))
        return self.dream_store.merge(self.planner.plan(entries))

    def accept(self, proposal_id: str) -> Optional[MemoryDreamProposal]:
        proposal = next(
            (item for item in self.dream_store.list(limit=0)
             if item.id == proposal_id), None)
        if proposal is None or proposal.status is not DreamStatus.PENDING:
            return proposal
        if proposal.kind is DreamKind.MERGE_DUPLICATES:
            entries = [
                entry for entry in self.store.list(
                    limit=0, scopes=(MemoryScope.PROJECT,))
                if entry.id in proposal.memory_ids
            ]
            if entries:
                representative = entries[0]
                merged = self.store.add(
                    proposal.proposed_text or representative.text,
                    scope=MemoryScope.PROJECT,
                    kind=MemoryKind.KNOWLEDGE,
                    memory_type=representative.memory_type,
                    topic=representative.topic,
                    why=representative.why,
                    how_to_apply=representative.how_to_apply,
                    tags=("auto-dream", "merged"),
                    source="auto-dream",
                    pinned=any(item.pinned for item in entries),
                    deduplicate=True,
                )
                for entry in entries:
                    if entry.id != merged.id:
                        self.store.remove(entry.id, scope=entry.scope)
        return self.dream_store.set_status(proposal_id, DreamStatus.ACCEPTED)

    def reject(self, proposal_id: str) -> Optional[MemoryDreamProposal]:
        return self.dream_store.set_status(proposal_id, DreamStatus.REJECTED)


class MemoryDreamScheduler:
    """Run AutoDream only after an idle delay following a completed turn."""

    def __init__(
        self,
        coordinator: MemoryDreamCoordinator,
        *,
        bus: EventBus,
        idle_seconds: float = 30.0,
    ) -> None:
        self.coordinator = coordinator
        self.bus = bus
        self.idle_seconds = max(1.0, float(idle_seconds))
        self._task: Optional[asyncio.Task[object]] = None
        self._unsubscribe = bus.subscribe(self.on_event)
        self._closed = False

    async def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            task = self._task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._task = None
            return
        if not isinstance(event, RunFinished) or self._closed:
            return
        task = self._task
        if task is not None and not task.done():
            return
        self._task = asyncio.create_task(
            self._run_after_idle(),
            name="krelcode-auto-dream",
        )

    async def _run_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.idle_seconds)
            created = await asyncio.to_thread(self.coordinator.run)
            for proposal in created:
                await self.bus.emit(MemoryCandidateObserved(
                    candidate_id=proposal.id,
                    status=DreamStatus(proposal.status).value,
                    trigger=DreamKind(proposal.kind).value,
                    confidence=proposal.confidence,
                    conflict_count=0,
                    digest=proposal.fingerprint,
                ))
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        self._closed = True
        self._unsubscribe()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None


def _absolute_paths(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        re.findall(r"(?<!\w)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+", text)
    ))


__all__ = [
    "DreamKind",
    "DreamStatus",
    "MemoryDreamCoordinator",
    "MemoryDreamPlanner",
    "MemoryDreamProposal",
    "MemoryDreamScheduler",
    "MemoryDreamStore",
]
