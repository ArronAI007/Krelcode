"""Crash-safe persistence and conflict reconciliation for Memory candidates."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

from ..core.redact import SecretRedactor
from .memory_candidate_models import (
    MAX_CANDIDATES,
    MAX_CANDIDATE_CHARS,
    MAX_EVIDENCE_CHARS,
    EVIDENCE_CONFIDENCE_BOOST,
    MemoryCandidate,
    MemoryCandidateDraft,
    MemoryCandidateStatus,
    MemoryCandidateTrigger,
    MemoryEvidence,
    _meaningful_candidate_text,
    _now,
    _project_id,
)
from .memory_utils import (
    memory_digest,
    memory_texts_conflict,
    normalize_memory_text,
    sanitize_memory_text,
)


class MemoryCandidateQueue:
    """Crash-safe, bounded project-local candidate queue."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        path: Optional[Path] = None,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.path = Path(path) if path is not None else (
            self.workspace_root / ".krelcode" / "memory" / "candidates.jsonl")
        self.project_id = _project_id(self.workspace_root)
        self.redactor = redactor
        self._lock = threading.RLock()

    def _sanitize_text(self, value: str, limit: int) -> str:
        raw = " ".join(str(value).split())
        if not raw:
            return ""
        return sanitize_memory_text(
            raw, redactor=self.redactor, max_chars=limit)

    def _sanitize_evidence(self, evidence: MemoryEvidence) -> MemoryEvidence:
        return MemoryEvidence(
            kind=self._sanitize_text(evidence.kind, 80) or "observation",
            turn_id=self._sanitize_text(evidence.turn_id, 160),
            session_id=self._sanitize_text(evidence.session_id, 160),
            excerpt=self._sanitize_text(evidence.excerpt, MAX_EVIDENCE_CHARS),
            tool_name=self._sanitize_text(evidence.tool_name, 120),
            command=self._sanitize_text(evidence.command, MAX_EVIDENCE_CHARS),
            exit_code=evidence.exit_code,
            created_at=evidence.created_at,
        )

    def _candidate_from_record(
        self,
        payload: Mapping[str, object],
    ) -> Optional[MemoryCandidate]:
        if str(payload.get("project_id") or "") != self.project_id:
            return None
        try:
            text = self._sanitize_text(str(payload.get("text") or ""), MAX_CANDIDATE_CHARS)
            if not text or not _meaningful_candidate_text(text):
                return None
            raw_evidence = payload.get("evidence")
            evidence = []
            if isinstance(raw_evidence, list):
                for item in raw_evidence[:64]:
                    if isinstance(item, dict):
                        evidence.append(self._sanitize_evidence(
                            MemoryEvidence.from_record(item)))
            raw_conflicts = payload.get("conflict_ids")
            conflicts = (
                tuple(str(item) for item in raw_conflicts if str(item).strip())
                if isinstance(raw_conflicts, list) else ()
            )
            confidence = float(str(payload.get("confidence") or 0.0))
            return MemoryCandidate(
                id=str(payload.get("id") or f"memcand-{uuid.uuid4().hex[:12]}"),
                text=text,
                trigger=str(payload.get("trigger") or MemoryCandidateTrigger.EXPLICIT_REMEMBER.value),
                confidence=confidence,
                digest=memory_digest(text),
                project_id=self.project_id,
                created_at=str(payload.get("created_at") or _now()),
                updated_at=str(payload.get("updated_at") or ""),
                status=str(payload.get("status") or MemoryCandidateStatus.PENDING.value),
                topic=str(payload.get("topic") or "general"),
                memory_type=str(payload.get("memory_type") or "reference"),
                why=str(payload.get("why") or ""),
                how_to_apply=str(payload.get("how_to_apply") or ""),
                evidence=tuple(evidence),
                conflict_ids=conflicts,
                existing_memory_id=str(payload.get("existing_memory_id") or ""),
            )
        except (TypeError, ValueError):
            return None

    def _load(self) -> list[MemoryCandidate]:
        if not self.path.is_file():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        latest: dict[str, MemoryCandidate] = {}
        for line in lines:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            candidate = self._candidate_from_record(payload)
            if candidate is not None:
                latest[candidate.id] = candidate
        records = list(latest.values())
        records.sort(key=lambda item: (item.created_at, item.id))
        return records[-MAX_CANDIDATES:]

    def _write(self, candidates: Iterable[MemoryCandidate]) -> None:
        records = list(candidates)[-MAX_CANDIDATES:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            temp.write_text(
                "".join(
                    json.dumps(
                        item.as_record(), ensure_ascii=False, sort_keys=True)
                    + "\n"
                    for item in records
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
        status: Optional[MemoryCandidateStatus | str] = None,
        limit: int = 50,
    ) -> tuple[MemoryCandidate, ...]:
        selected = MemoryCandidateStatus(status) if status is not None else None
        with self._lock:
            records = self._load()
        if selected is not None:
            records = [item for item in records if item.status is selected]
        if limit > 0:
            records = records[-limit:]
        return tuple(records)

    def get(self, candidate_id: str) -> Optional[MemoryCandidate]:
        target = str(candidate_id).strip()
        return next((item for item in self.list(limit=0) if item.id == target), None)

    def pending_count(self) -> int:
        return len(self.list(status=MemoryCandidateStatus.PENDING, limit=0))

    def merge(self, draft: MemoryCandidateDraft) -> tuple[Optional[MemoryCandidate], bool]:
        text = self._sanitize_text(draft.text, MAX_CANDIDATE_CHARS)
        if not text or not _meaningful_candidate_text(text):
            return None, False
        why = self._sanitize_text(draft.why, 600)
        how_to_apply = self._sanitize_text(draft.how_to_apply, 600)
        evidence_items: list[MemoryEvidence] = []
        for item in draft.evidence:
            safe_item = self._sanitize_evidence(item)
            if safe_item.excerpt or safe_item.command:
                evidence_items.append(safe_item)
        evidence = tuple(evidence_items)
        digest = memory_digest(text)
        now = _now()
        with self._lock:
            records = self._load()
            existing = next(
                (item for item in reversed(records) if item.digest == digest),
                None,
            )
            if existing is None:
                candidate = MemoryCandidate(
                    id=f"memcand-{uuid.uuid4().hex[:12]}",
                    text=text,
                    trigger=draft.trigger,
                    confidence=draft.confidence,
                    digest=digest,
                    project_id=self.project_id,
                    created_at=now,
                    updated_at=now,
                    topic=draft.topic,
                    memory_type=draft.memory_type,
                    why=why,
                    how_to_apply=how_to_apply,
                    evidence=evidence,
                )
                records.append(candidate)
                records = self._reconcile_conflicts(records)
                candidate = next(item for item in records if item.id == candidate.id)
                self._write(records)
                return candidate, True

            identities = {item.identity for item in existing.evidence}
            new_evidence = [item for item in evidence if item.identity not in identities]
            rejected = existing.status is MemoryCandidateStatus.REJECTED
            reopen = rejected and draft.trigger is MemoryCandidateTrigger.EXPLICIT_REMEMBER
            if rejected and not reopen:
                return existing, False
            confidence = max(existing.confidence, draft.confidence)
            if new_evidence and not rejected:
                confidence = min(0.99, confidence + EVIDENCE_CONFIDENCE_BOOST)
            status = (
                MemoryCandidateStatus.PENDING
                if reopen else existing.status
            )
            updated = replace(
                existing,
                text=text,
                confidence=confidence,
                evidence=(*existing.evidence, *new_evidence),
                status=status,
                trigger=draft.trigger if reopen else existing.trigger,
                topic=draft.topic or existing.topic,
                memory_type=draft.memory_type,
                why=why or existing.why,
                how_to_apply=how_to_apply or existing.how_to_apply,
                updated_at=now,
            )
            records = [updated if item.id == existing.id else item for item in records]
            records = self._reconcile_conflicts(records)
            updated = next(item for item in records if item.id == existing.id)
            self._write(records)
            return updated, False

    def _reconcile_conflicts(
        self,
        records: Sequence[MemoryCandidate],
    ) -> List[MemoryCandidate]:
        conflicts: dict[str, set[str]] = {
            item.id: set(item.conflict_ids)
            for item in records
            if item.status is not MemoryCandidateStatus.REJECTED
        }
        active = [
            item for item in records
            if item.status is not MemoryCandidateStatus.REJECTED
        ]
        candidate_ids = {item.id for item in records}
        for index, first in enumerate(active):
            for second in active[index + 1:]:
                conflicting = memory_texts_conflict(first.text, second.text)
                if (
                    first.topic == second.topic == "test_command"
                    and first.digest != second.digest
                ):
                    conflicting = True
                if not conflicting:
                    continue
                conflicts[first.id].add(second.id)
                conflicts[second.id].add(first.id)
        result: list[MemoryCandidate] = []
        for item in records:
            active_ids = conflicts.get(item.id)
            if active_ids is None:
                result.append(item)
                continue
            related = {
                candidate_id for candidate_id in active_ids
                if (
                    candidate_id not in candidate_ids
                    or any(candidate.id == candidate_id for candidate in active)
                )
            }
            result.append(replace(
                item,
                conflict_ids=tuple(sorted(related)),
                updated_at=_now() if related != set(item.conflict_ids) else item.updated_at,
            ))
        return result

    def set_external_conflict_ids(
        self,
        candidate_id: str,
        conflict_ids: Iterable[str],
    ) -> Optional[MemoryCandidate]:
        """Replace conflicts with permanent memory while retaining candidate ids."""
        target = str(candidate_id).strip()
        if not target:
            return None
        with self._lock:
            records = self._load()
            candidate = next((item for item in records if item.id == target), None)
            if candidate is None:
                return None
            candidate_ids = {item.id for item in records}
            internal = {
                item for item in candidate.conflict_ids if item in candidate_ids}
            external = {
                str(item).strip() for item in conflict_ids if str(item).strip()}
            merged = tuple(sorted(internal | external))
            if merged == candidate.conflict_ids:
                return candidate
            updated = replace(
                candidate, conflict_ids=merged, updated_at=_now())
            self._write([
                updated if item.id == target else item for item in records])
            return updated

    def set_status(
        self,
        candidate_id: str,
        status: MemoryCandidateStatus | str,
        *,
        existing_memory_id: str = "",
    ) -> Optional[MemoryCandidate]:
        target = str(candidate_id).strip()
        normalized = MemoryCandidateStatus(status)
        with self._lock:
            records = self._load()
            candidate = next((item for item in records if item.id == target), None)
            if candidate is None:
                return None
            updated = replace(
                candidate,
                status=normalized,
                existing_memory_id=existing_memory_id or candidate.existing_memory_id,
                updated_at=_now(),
            )
            records = [updated if item.id == target else item for item in records]
            records = self._reconcile_conflicts(records)
            self._write(records)
            return next((item for item in records if item.id == target), None)


__all__ = ["MemoryCandidateQueue"]
