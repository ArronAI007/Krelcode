"""Immutable memory records and their stable semantic taxonomy."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional

MAX_ENTRY_CHARS = 2_000
MAX_ENTRIES = 200
MAX_TAGS = 12
MAX_TAG_CHARS = 64
MAX_WHY_CHARS = 600
MAX_TOPIC_CHARS = 120
MAX_INDEX_ENTRIES = 200
MAX_INDEX_LINE_CHARS = 150
MAX_INDEX_CHARS = 24_000
INJECT_ENTRIES = 12
INJECT_CHARS = 4_000
FRAGMENT_ID = "memory:scoped"
LEGACY_FRAGMENT_ID = "memory:project"
INDEX_FRAGMENT_ID = "memory:index"

_SCOPE_PREFIX_RE = re.compile(
    r"^\s*(user|project|session)\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)
_SAFE_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SENSITIVE_METADATA_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|secret|password|passwd|cookie|"
    r"authorization|private[_-]?key)(?:$|[_-])"
)


class MemoryScope(str, Enum):
    """Persistence boundary for one memory record."""

    USER = "user"
    PROJECT = "project"
    SESSION = "session"


class MemoryKind(str, Enum):
    """Stable semantic class used by later retrieval/ranking stages."""

    NOTE = "note"
    PREFERENCE = "preference"
    FACT = "fact"
    WORKFLOW = "workflow"
    # Model-generated or reviewed long-term project knowledge. This is
    # contextual data only and must never be promoted to a system rule.
    KNOWLEDGE = "knowledge"


class MemoryType(str, Enum):
    """Long-term memory semantics, independent from persistence scope.

    ``MemoryKind`` remains the legacy JSONL taxonomy used by older records.
    ``MemoryType`` is the user-facing and prompt-facing classification.  The
    two are intentionally not collapsed so existing stores remain readable.
    """

    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


MEMORY_SCOPE_ORDER = (
    MemoryScope.USER,
    MemoryScope.PROJECT,
    MemoryScope.SESSION,
)
SCOPE_LABELS = {
    MemoryScope.USER: "用户",
    MemoryScope.PROJECT: "项目",
    MemoryScope.SESSION: "会话",
}

MEMORY_TYPE_ORDER = (
    MemoryType.USER,
    MemoryType.FEEDBACK,
    MemoryType.PROJECT,
    MemoryType.REFERENCE,
)
STRUCTURED_MEMORY_TYPES = frozenset((
    MemoryType.FEEDBACK,
    MemoryType.PROJECT,
))


def coerce_memory_scope(value: MemoryScope | str) -> MemoryScope:
    """Parse canonical scope names with a few user-facing aliases."""
    if isinstance(value, MemoryScope):
        return value
    normalized = str(value).strip().lower()
    aliases = {
        "global": MemoryScope.USER,
        "workspace": MemoryScope.PROJECT,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return MemoryScope(normalized)
    except ValueError as exc:
        allowed = ", ".join(scope.value for scope in MEMORY_SCOPE_ORDER)
        raise ValueError(f"未知记忆作用域 {value!r}；可用: {allowed}") from exc


def coerce_memory_kind(value: MemoryKind | str | None) -> MemoryKind:
    if isinstance(value, MemoryKind):
        return value
    normalized = str(value or MemoryKind.NOTE.value).strip().lower()
    try:
        return MemoryKind(normalized)
    except ValueError as exc:
        allowed = ", ".join(kind.value for kind in MemoryKind)
        raise ValueError(f"未知记忆类型 {value!r}；可用: {allowed}") from exc


def coerce_memory_type(value: MemoryType | str | None) -> MemoryType:
    """Parse the memory semantics used by MEMORY.md and prompt contracts."""
    if isinstance(value, MemoryType):
        return value
    normalized = str(value or MemoryType.REFERENCE.value).strip().lower()
    aliases = {
        "preference": MemoryType.USER,
        "correction": MemoryType.FEEDBACK,
        "knowledge": MemoryType.PROJECT,
        "note": MemoryType.REFERENCE,
        "fact": MemoryType.REFERENCE,
        "workflow": MemoryType.FEEDBACK,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return MemoryType(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in MEMORY_TYPE_ORDER)
        raise ValueError(f"未知记忆语义类型 {value!r}；可用: {allowed}") from exc


def validate_memory_structure(
    memory_type: MemoryType | str,
    why: str,
    how_to_apply: str,
) -> None:
    """Require actionable context for feedback and project memories."""
    normalized = coerce_memory_type(memory_type)
    if normalized not in STRUCTURED_MEMORY_TYPES:
        return
    missing = []
    if not str(why).strip():
        missing.append("Why")
    if not str(how_to_apply).strip():
        missing.append("How to apply")
    if missing:
        raise ValueError(
            f"{normalized.value} 类型记忆必须包含 {' / '.join(missing)}")


def default_user_memory_path(home: Optional[Path] = None) -> Path:
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / ".krelcode" / "memory" / "entries.jsonl"


def parse_memory_note(text: str) -> tuple[MemoryScope, str]:
    """Parse ``user: ...`` / ``project: ...`` / ``session: ...`` prefixes.

    A note without a known prefix defaults to project scope, preserving the
    original single-scope ``#`` shortcut behaviour.
    """
    raw = str(text).strip()
    match = _SCOPE_PREFIX_RE.match(raw)
    if match is None:
        return MemoryScope.PROJECT, raw
    return coerce_memory_scope(match.group(1)), match.group(2).strip()


@dataclass(frozen=True)
class MemoryEntry:
    """One immutable memory record independent of its storage backend."""

    id: str
    text: str
    created_at: str
    scope: MemoryScope | str = MemoryScope.PROJECT
    kind: MemoryKind | str = MemoryKind.NOTE
    source: str = "manual"
    tags: tuple[str, ...] = ()
    updated_at: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    pinned: bool = False
    memory_type: MemoryType | str = MemoryType.REFERENCE
    topic: str = ""
    why: str = ""
    how_to_apply: str = ""

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValueError("memory entry id 不能为空")
        if not str(self.text).strip():
            raise ValueError("memory entry text 不能为空")
        scope = coerce_memory_scope(self.scope)
        kind = coerce_memory_kind(self.kind)
        memory_type = coerce_memory_type(self.memory_type)
        tags: list[str] = []
        for raw in self.tags:
            tag = " ".join(str(raw).split())
            if tag and tag not in tags:
                tags.append(tag[:MAX_TAG_CHARS])
        metadata = dict(self.metadata) if isinstance(self.metadata, Mapping) else {}
        why = " ".join(str(self.why or "").split())[:MAX_WHY_CHARS]
        how_to_apply = " ".join(
            str(self.how_to_apply or "").split())[:MAX_WHY_CHARS]
        if memory_type in STRUCTURED_MEMORY_TYPES and (
            not why or not how_to_apply
        ):
            metadata.setdefault("original_memory_type", memory_type.value)
            metadata["memory_type_downgraded"] = True
            memory_type = MemoryType.REFERENCE
        object.__setattr__(self, "id", str(self.id))
        object.__setattr__(self, "text", str(self.text))
        object.__setattr__(self, "created_at", str(self.created_at))
        object.__setattr__(self, "updated_at", str(self.updated_at or self.created_at))
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", str(self.source or "manual"))
        object.__setattr__(self, "tags", tuple(tags[:MAX_TAGS]))
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "pinned", bool(self.pinned))
        object.__setattr__(self, "memory_type", memory_type)
        object.__setattr__(
            self, "topic", " ".join(str(self.topic or "").split())[:MAX_TOPIC_CHARS])
        object.__setattr__(self, "why", why)
        object.__setattr__(self, "how_to_apply", how_to_apply)

    @property
    def scope_value(self) -> str:
        return coerce_memory_scope(self.scope).value

    @property
    def kind_value(self) -> str:
        return coerce_memory_kind(self.kind).value

    @property
    def memory_type_value(self) -> str:
        return coerce_memory_type(self.memory_type).value

    @property
    def is_structured(self) -> bool:
        memory_type = coerce_memory_type(self.memory_type)
        return (
            memory_type not in STRUCTURED_MEMORY_TYPES
            or bool(self.why and self.how_to_apply)
        )

    def as_record(self) -> dict[str, object]:
        """JSON-compatible representation used by the scoped JSONL files."""
        return {
            "id": self.id,
            "text": self.text,
            "scope": self.scope_value,
            "kind": self.kind_value,
            "source": self.source,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
            "pinned": self.pinned,
            "memory_type": self.memory_type_value,
            "topic": self.topic,
            "why": self.why,
            "how_to_apply": self.how_to_apply,
        }


@dataclass(frozen=True)
class MemoryTopic:
    """One generated Markdown topic view over authoritative JSONL entries."""

    filename: str
    title: str
    hook: str
    memory_type: MemoryType
    scope: MemoryScope
    entries: tuple[MemoryEntry, ...]
    pinned: bool = False
    updated_at: str = ""

    @property
    def relative_path(self) -> str:
        return f"topics/{self.filename}"

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.entries)


@dataclass(frozen=True)
class MemoryIndexSnapshot:
    """Bounded MEMORY.md content plus the exact entries represented by it."""

    text: str
    topics: tuple[MemoryTopic, ...]
    entries: tuple[MemoryEntry, ...]
    truncated: int = 0


@dataclass(frozen=True)
class MemoryStoreSnapshot:
    """One disk read worth of dashboard and prompt-accounting data."""

    entries: tuple[MemoryEntry, ...] = ()
    total_entries: int = 0
    tokens: int = 0


@dataclass(frozen=True)
class MemoryMigrationReport:
    """Machine-checkable result of normalizing one memory scope file."""

    scope: str
    path: str
    scanned: int = 0
    migrated: int = 0
    kept: int = 0
    duplicates: int = 0
    skipped: int = 0
    changed: bool = False
    backup_path: str = ""

