"""Bounded DTOs and validation rules for Auto Memory candidates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, cast

from ..core.redact import MASK
from .memory_models import (
    MemoryKind,
    MemoryScope,
    MemoryType,
    coerce_memory_kind,
    coerce_memory_type,
)
from .memory_utils import normalize_memory_text


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


__all__ = [
    "DEFAULT_AUTO_THRESHOLD",
    "DEFAULT_SUGGEST_THRESHOLD",
    "EVIDENCE_CONFIDENCE_BOOST",
    "MAX_CANDIDATES",
    "MAX_CANDIDATE_CHARS",
    "MAX_EVIDENCE_CHARS",
    "TRIGGER_LABELS",
    "AutoMemoryMode",
    "MemoryCandidate",
    "MemoryCandidateDraft",
    "MemoryCandidateStatus",
    "MemoryCandidateTrigger",
    "MemoryEvidence",
    "coerce_auto_memory_mode",
]
