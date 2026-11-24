"""Project-local Auto Memory candidate extraction and review workflow.

Auto Memory deliberately works on *candidates*, not directly on the long-term
memory store.  The default ``suggest`` mode records reviewable evidence only;
``auto`` promotes candidates that are both confident and conflict-free.  All
candidate queues live under ``<workspace>/.krelcode/memory`` so project
knowledge cannot leak into another workspace.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, cast

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
from ..core.redact import MASK, SecretRedactor
from .memory import MemoryStore
from .memory_models import (
    MemoryEntry,
    MemoryKind,
    MemoryScope,
    MemoryType,
    coerce_memory_kind,
    coerce_memory_type,
    validate_memory_structure,
)
from .memory_utils import (
    extract_test_command,
    memory_digest,
    memory_texts_conflict,
    normalize_memory_text,
    sanitize_memory_text,
)

MAX_CANDIDATES, MAX_CANDIDATE_CHARS, MAX_EVIDENCE_CHARS = 200, 1_000, 600
DEFAULT_SUGGEST_THRESHOLD, DEFAULT_AUTO_THRESHOLD, EVIDENCE_CONFIDENCE_BOOST = 0.55, 0.80, 0.10


class AutoMemoryMode(str, Enum):
    OFF = "off"
    SUGGEST = "suggest"
    AUTO = "auto"


class MemoryCandidateStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class MemoryCandidateTrigger(str, Enum):
    EXPLICIT_REMEMBER = "explicit_remember"
    USER_CORRECTION = "user_correction"
    SUCCESSFUL_TEST_REUSE = "successful_test_reuse"
    ARCHITECTURE_PATTERN = "architecture_pattern"
    TASK_COMPLETION = "task_completion"


TRIGGER_LABELS = {
    MemoryCandidateTrigger.EXPLICIT_REMEMBER: "用户明确要求记住",
    MemoryCandidateTrigger.USER_CORRECTION: "用户纠正",
    MemoryCandidateTrigger.SUCCESSFUL_TEST_REUSE: "成功测试命令复用",
    MemoryCandidateTrigger.ARCHITECTURE_PATTERN: "架构模式重复确认",
    MemoryCandidateTrigger.TASK_COMPLETION: "任务完成后的高价值事实",
}


def coerce_auto_memory_mode(value: AutoMemoryMode | str | None) -> AutoMemoryMode:
    if isinstance(value, AutoMemoryMode):
        return value
    normalized = str(value or AutoMemoryMode.SUGGEST.value).strip().lower()
    try:
        return AutoMemoryMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in AutoMemoryMode)
        raise ValueError(f"未知 Auto Memory 模式 {value!r}；可用: {allowed}") from exc


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _project_id(workspace_root: Path) -> str:
    resolved = str(Path(workspace_root).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def _meaningful_candidate_text(text: str) -> bool:
    """Reject empty, generic or secret-only candidates after redaction."""
    value = normalize_memory_text(text)
    for marker in (
        "记住这个", "记住一下", "remember this", "remember that",
        "this", "这个", "一下",
    ):
        value = value.replace(marker, " ")
    value = value.replace(MASK.casefold(), " ")
    value = re.sub(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|"
        r"password|cookie|authorization|bearer)\b",
        " ",
        value,
    )
    substantive = re.findall(r"[a-z0-9\u4e00-\u9fff]", value)
    return len(substantive) >= 6


@dataclass(frozen=True)
class MemoryEvidence:
    kind: str
    turn_id: str = ""
    session_id: str = ""
    excerpt: str = ""
    tool_name: str = ""
    command: str = ""
    exit_code: Optional[int] = None
    created_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind or "observation")[:80])
        object.__setattr__(self, "turn_id", str(self.turn_id or "")[:160])
        object.__setattr__(self, "session_id", str(self.session_id or "")[:160])
        object.__setattr__(self, "excerpt", str(self.excerpt or "")[:MAX_EVIDENCE_CHARS])
        object.__setattr__(self, "tool_name", str(self.tool_name or "")[:120])
        object.__setattr__(self, "command", str(self.command or "")[:MAX_EVIDENCE_CHARS])
        object.__setattr__(self, "created_at", str(self.created_at or _now()))

    @property
    def identity(self) -> str:
        payload = json.dumps({
            "kind": self.kind,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "excerpt": normalize_memory_text(self.excerpt),
            "tool_name": self.tool_name,
            "command": normalize_memory_text(self.command),
            "exit_code": self.exit_code,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_record(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "excerpt": self.excerpt,
            "tool_name": self.tool_name,
            "command": self.command,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, payload: Mapping[str, object]) -> "MemoryEvidence":
        raw_exit = payload.get("exit_code")
        exit_code = raw_exit if isinstance(raw_exit, int) else None
        return cls(
            kind=str(payload.get("kind") or "observation"),
            turn_id=str(payload.get("turn_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            excerpt=str(payload.get("excerpt") or ""),
            tool_name=str(payload.get("tool_name") or ""),
            command=str(payload.get("command") or ""),
            exit_code=exit_code,
            created_at=str(payload.get("created_at") or _now()),
        )


@dataclass(frozen=True)
class MemoryCandidateDraft:
    text: str
    trigger: MemoryCandidateTrigger | str
    confidence: float
    evidence: tuple[MemoryEvidence, ...] = ()
    topic: str = "general"
    memory_type: MemoryType | str = MemoryType.REFERENCE
    why: str = ""
    how_to_apply: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger", MemoryCandidateTrigger(self.trigger))
        object.__setattr__(
            self, "confidence", round(
                max(0.0, min(float(self.confidence), 1.0)), 2))
        object.__setattr__(
            self, "evidence", tuple(self.evidence or ()))
        object.__setattr__(self, "topic", str(self.topic or "general")[:120])
        object.__setattr__(
            self, "memory_type", coerce_memory_type(self.memory_type))
        object.__setattr__(
            self, "why", " ".join(str(self.why or "").split())[:600])
        object.__setattr__(
            self,
            "how_to_apply",
            " ".join(str(self.how_to_apply or "").split())[:600],
        )


@dataclass(frozen=True)
class MemoryCandidate:
    id: str
    text: str
    trigger: MemoryCandidateTrigger | str
    confidence: float
    digest: str
    project_id: str
    created_at: str
    updated_at: str = ""
    scope: MemoryScope | str = MemoryScope.PROJECT
    kind: MemoryKind | str = MemoryKind.KNOWLEDGE
    status: MemoryCandidateStatus | str = MemoryCandidateStatus.PENDING
    topic: str = "general"
    evidence: tuple[MemoryEvidence, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    existing_memory_id: str = ""
    memory_type: MemoryType | str = MemoryType.REFERENCE
    why: str = ""
    how_to_apply: str = ""

    def __post_init__(self) -> None:
        trigger = MemoryCandidateTrigger(self.trigger)
        status = MemoryCandidateStatus(self.status)
        scope = MemoryScope(self.scope)
        kind = coerce_memory_kind(self.kind)
        memory_type = coerce_memory_type(self.memory_type)
        if scope is not MemoryScope.PROJECT:
            raise ValueError("Auto Memory 候选只能使用 project scope")
        if kind is not MemoryKind.KNOWLEDGE:
            raise ValueError("模型生成的 Auto Memory 候选必须是 knowledge 类型")
        object.__setattr__(self, "id", str(self.id).strip())
        object.__setattr__(self, "text", str(self.text).strip())
        object.__setattr__(self, "trigger", trigger)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "memory_type", memory_type)
        object.__setattr__(
            self, "confidence", round(
                max(0.0, min(float(self.confidence), 1.0)), 2))
        object.__setattr__(self, "topic", str(self.topic or "general")[:120])
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))
        object.__setattr__(
            self, "conflict_ids", tuple(dict.fromkeys(self.conflict_ids or ())))
        object.__setattr__(self, "updated_at", str(self.updated_at or self.created_at))
        object.__setattr__(
            self, "why", " ".join(str(self.why or "").split())[:600])
        object.__setattr__(
            self,
            "how_to_apply",
            " ".join(str(self.how_to_apply or "").split())[:600],
        )

    @property
    def trigger_value(self) -> str:
        return cast(MemoryCandidateTrigger, self.trigger).value

    @property
    def status_value(self) -> str:
        return cast(MemoryCandidateStatus, self.status).value

    @property
    def memory_type_value(self) -> str:
        return coerce_memory_type(self.memory_type).value

    @property
    def structurally_complete(self) -> bool:
        if coerce_memory_type(self.memory_type) not in {
            MemoryType.FEEDBACK,
            MemoryType.PROJECT,
        }:
            return True
        return bool(self.why and self.how_to_apply)

    def as_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "trigger": self.trigger_value,
            "confidence": self.confidence,
            "digest": self.digest,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "scope": MemoryScope(self.scope).value,
            "kind": coerce_memory_kind(self.kind).value,
            "memory_type": self.memory_type_value,
            "why": self.why,
            "how_to_apply": self.how_to_apply,
            "status": self.status_value,
            "topic": self.topic,
            "evidence": [item.as_record() for item in self.evidence],
            "conflict_ids": list(self.conflict_ids),
            "existing_memory_id": self.existing_memory_id,
        }


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


@dataclass(frozen=True)
class MemoryTurnSnapshot:
    user_input: str
    messages: tuple[Message, ...] = ()
    turn_id: str = ""
    session_id: str = ""


_EXPLICIT_REMEMBER_RE = re.compile(
    r"(?is)(?:请|帮我)?记住(?:一下|这一点|这点)?\s*[：:，,\-]?\s*(.+)"
    r"|remember(?:\s+that)?\s*[：:,\-]?\s*(.+)"
)
_CORRECTION_SENTENCE_RE = re.compile(
    r"(?is)(?:不是|并非).{1,120}?(?:而是|而是说)\s*[：:，,\-]?\s*(.+)"
)
_CORRECTION_PREFIX_RE = re.compile(
    r"(?is)^(?:(?:不对|错了|纠正|更正|应该是|应当是|实际上|其实是|"
    r"you\s+are\s+wrong|that'?s\s+wrong|correction|actually)"
    r"\s*[：:，,\-]?\s*)(.+)$"
)
_ARCHITECTURE_TERMS = (
    "架构", "分层", "模块", "解耦", "流水线", "入口层", "执行层",
    "architecture", "layered", "module", "decoupled", "pipeline",
    "entrypoint", "runtime layer",
)
_ARCHITECTURE_ASSERTIONS = (
    "采用", "使用", "分为", "分成", "由", "负责", "位于", "是",
    "uses", "uses a", "consists", "has", "is", "contains",
)
_TEMP_OR_UNVERIFIED = (
    "临时", "暂时", "今天", "这次", "本次", "刚刚", "可能", "也许",
    "待定", "以后再", "todo", "temporary", "for now", "this time",
    "one-off", "one time", "maybe", "might", "not sure",
)
_TASK_FACT_RE = re.compile(
    r"(?i)(测试命令|运行测试|入口文件|入口位于|项目使用|项目采用|"
    r"架构采用|模块负责|配置文件位于|依赖包含|test command|"
    r"entry ?point|project uses|architecture|configured with)"
)
_FORMAL_TEST_COMMAND_RE = re.compile(
    r"(?i)\b(?:pytest|py\.test|tox|nox|npm(?:\s+run)?\s+test|"
    r"pnpm\s+test|yarn\s+test|bun\s+test|cargo\s+test|go\s+test|"
    r"dotnet\s+test|mvn\s+test|gradlew?(?:\s+test)?|make\s+test|"
    r"ruff\s+check)\b"
)
_MEMORY_TYPE_PREFIX_RE = re.compile(
    r"^(user|feedback|project|reference)\s*:\s*", re.IGNORECASE)
_WHY_SECTION_RE = re.compile(
    r"(?is)(?:\*\*)?why(?:\*\*)?\s*:\s*(.*?)"
    r"(?=(?:\*\*)?how\s+to\s+apply(?:\*\*)?\s*:|$)"
)
_HOW_SECTION_RE = re.compile(
    r"(?is)(?:\*\*)?how\s+to\s+apply(?:\*\*)?\s*:\s*(.*)$"
)


class MemoryCandidateExtractor:
    """Extract only bounded, stable and evidence-backed memory candidates."""

    def __init__(
        self,
        *,
        redactor: Optional[SecretRedactor] = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.redactor = redactor
        self.clock = clock

    def extract(self, turn: MemoryTurnSnapshot) -> tuple[MemoryCandidateDraft, ...]:
        user_input = str(turn.user_input or "").strip()
        if not user_input:
            return ()
        drafts: list[MemoryCandidateDraft] = []
        explicit_text = self._explicit_remember(user_input)
        if explicit_text:
            drafts.append(self._draft(
                explicit_text,
                MemoryCandidateTrigger.EXPLICIT_REMEMBER,
                0.95,
                turn,
                evidence_kind="explicit_user_request",
                excerpt=user_input,
            ))
        else:
            correction = self._user_correction(user_input)
            if correction:
                drafts.append(self._draft(
                    correction,
                    MemoryCandidateTrigger.USER_CORRECTION,
                    0.85,
                    turn,
                    evidence_kind="user_correction",
                    excerpt=user_input,
                    memory_type=MemoryType.FEEDBACK,
                ))

        architecture = self._architecture_pattern(user_input)
        if architecture:
            drafts.append(self._draft(
                architecture,
                MemoryCandidateTrigger.ARCHITECTURE_PATTERN,
                0.65,
                turn,
                evidence_kind="architecture_confirmation",
                excerpt=user_input,
                topic="architecture",
            ))

        drafts.extend(self._successful_test_commands(turn))
        completion = self._task_completion_fact(turn)
        if completion:
            drafts.append(self._draft(
                completion,
                MemoryCandidateTrigger.TASK_COMPLETION,
                0.65,
                turn,
                evidence_kind="task_completion",
                excerpt=completion,
            ))
        return tuple(item for item in drafts if _meaningful_candidate_text(item.text))

    def _draft(
        self,
        text: str,
        trigger: MemoryCandidateTrigger,
        confidence: float,
        turn: MemoryTurnSnapshot,
        *,
        evidence_kind: str,
        excerpt: str = "",
        tool_name: str = "",
        command: str = "",
        exit_code: Optional[int] = None,
        topic: str = "general",
        memory_type: MemoryType | str = MemoryType.PROJECT,
        why: str = "",
        how_to_apply: str = "",
    ) -> MemoryCandidateDraft:
        clean_text, prefix_type = self._type_prefix(text)
        normalized_type = prefix_type or coerce_memory_type(memory_type)
        without_structure, parsed_why, parsed_how = self._parse_structure(clean_text)
        safe_why = parsed_why or why or self._default_why(trigger)
        safe_how = parsed_how or how_to_apply or self._default_how(trigger)
        if normalized_type not in {MemoryType.FEEDBACK, MemoryType.PROJECT}:
            safe_why = parsed_why or why
            safe_how = parsed_how or how_to_apply
        evidence = MemoryEvidence(
            kind=evidence_kind,
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            excerpt=excerpt,
            tool_name=tool_name,
            command=command,
            exit_code=exit_code,
            created_at=self.clock(),
        )
        return MemoryCandidateDraft(
            text=without_structure,
            trigger=trigger,
            confidence=confidence,
            evidence=(evidence,),
            topic=topic,
            memory_type=normalized_type,
            why=safe_why,
            how_to_apply=safe_how,
        )

    @staticmethod
    def _type_prefix(text: str) -> tuple[str, Optional[MemoryType]]:
        raw = str(text or "").strip()
        match = _MEMORY_TYPE_PREFIX_RE.match(raw)
        if match is None:
            return raw, None
        return raw[match.end():].strip(), MemoryType(match.group(1).lower())

    @staticmethod
    def _parse_structure(text: str) -> tuple[str, str, str]:
        raw = str(text or "").strip()
        why_match = _WHY_SECTION_RE.search(raw)
        how_match = _HOW_SECTION_RE.search(raw)
        if why_match is None and how_match is None:
            return raw, "", ""
        first = min(
            match.start() for match in (why_match, how_match)
            if match is not None)
        body = raw[:first].strip() or raw
        why = " ".join(why_match.group(1).split()) if why_match else ""
        how = " ".join(how_match.group(1).split()) if how_match else ""
        return body, why, how

    @staticmethod
    def _default_why(trigger: MemoryCandidateTrigger) -> str:
        return {
            MemoryCandidateTrigger.EXPLICIT_REMEMBER:
                "用户明确要求将这一稳定事实用于后续工作。",
            MemoryCandidateTrigger.USER_CORRECTION:
                "用户纠正了此前的错误理解，避免后续重复犯错。",
            MemoryCandidateTrigger.SUCCESSFUL_TEST_REUSE:
                "该验证命令已在当前项目中成功执行并可重复使用。",
            MemoryCandidateTrigger.ARCHITECTURE_PATTERN:
                "用户多次确认了这一项目级约束或设计意图。",
            MemoryCandidateTrigger.TASK_COMPLETION:
                "任务完成时已确认该事实具有跨任务复用价值。",
        }[trigger]

    @staticmethod
    def _default_how(trigger: MemoryCandidateTrigger) -> str:
        return {
            MemoryCandidateTrigger.EXPLICIT_REMEMBER:
                "仅在相关任务中作为 Evidence 使用，并结合当前代码与用户请求验证。",
            MemoryCandidateTrigger.USER_CORRECTION:
                "遇到同类决策时先应用该纠正；若工作区事实已变化，以当前事实为准。",
            MemoryCandidateTrigger.SUCCESSFUL_TEST_REUSE:
                "需要同类验证时优先复用该命令，并确认工作目录、依赖和参数仍然匹配。",
            MemoryCandidateTrigger.ARCHITECTURE_PATTERN:
                "设计或修改相关模块时先检查该约束是否仍成立，不得用它替代代码检查。",
            MemoryCandidateTrigger.TASK_COMPLETION:
                "仅在后续任务与该事实直接相关时引用，并重新验证路径、符号和行为。",
        }[trigger]

    @staticmethod
    def _explicit_remember(text: str) -> str:
        match = _EXPLICIT_REMEMBER_RE.search(text)
        if match is None:
            return ""
        candidate = next((group for group in match.groups() if group), "")
        return MemoryCandidateExtractor._stable(candidate)

    @staticmethod
    def _user_correction(text: str) -> str:
        match = _CORRECTION_SENTENCE_RE.search(text)
        if match is not None:
            return MemoryCandidateExtractor._stable(text.strip())
        match = _CORRECTION_PREFIX_RE.match(text.strip())
        if match is None:
            return ""
        return MemoryCandidateExtractor._stable(match.group(1))

    @staticmethod
    def _architecture_pattern(text: str) -> str:
        candidate = MemoryCandidateExtractor._stable(text)
        if not candidate:
            return ""
        lowered = candidate.casefold()
        if not any(term in lowered for term in _ARCHITECTURE_TERMS):
            return ""
        if not any(term in lowered for term in _ARCHITECTURE_ASSERTIONS):
            return ""
        return candidate

    @staticmethod
    def _stable(text: str) -> str:
        value = " ".join(str(text).strip().split())
        if len(value) < 6 or len(value) > MAX_CANDIDATE_CHARS:
            return ""
        if value.rstrip().endswith(("?", "？")):
            return ""
        lowered = value.casefold()
        if any(marker in lowered for marker in _TEMP_OR_UNVERIFIED):
            return ""
        if lowered.startswith(("如果 ", "假如 ", "if ", "what if ")):
            return ""
        return value

    def _successful_test_commands(
        self,
        turn: MemoryTurnSnapshot,
    ) -> list[MemoryCandidateDraft]:
        drafts: list[MemoryCandidateDraft] = []
        seen: set[str] = set()
        for message in turn.messages:
            if message.role != "tool" or message.tool_is_error:
                continue
            metadata = message.tool_meta or {}
            if metadata.get("exit_code") != 0:
                continue
            command = str(metadata.get("command") or "")
            if not command or not _FORMAL_TEST_COMMAND_RE.search(command):
                continue
            canonical = extract_test_command(command) or normalize_memory_text(command)
            if canonical in seen:
                continue
            seen.add(canonical)
            drafts.append(self._draft(
                f"验证命令：{command}",
                MemoryCandidateTrigger.SUCCESSFUL_TEST_REUSE,
                0.70,
                turn,
                evidence_kind="successful_test_command",
                excerpt=f"exit_code=0; command={command}",
                tool_name=message.tool_name or "run_command",
                command=command,
                exit_code=0,
                topic="test_command",
            ))
        return drafts

    @staticmethod
    def _task_completion_fact(turn: MemoryTurnSnapshot) -> str:
        assistant_texts = [
            message.content for message in turn.messages
            if message.role == "assistant" and message.content
        ]
        if not assistant_texts:
            return ""
        final = " ".join(str(assistant_texts[-1]).split())
        if "[krelcode]" in final.casefold():
            return ""
        pieces = re.split(
            r"(?<=[。！？.!?])(?:\s+|(?=[^\s]))|\n+", final)
        for piece in pieces:
            candidate = MemoryCandidateExtractor._stable(piece)
            if not candidate or not _TASK_FACT_RE.search(candidate):
                continue
            if len(candidate) > 240:
                continue
            return candidate
        return ""


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
