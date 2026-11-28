"""Immutable memory records and their stable semantic taxonomy."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional

from ..contracts.asset import (
    AssetEnvelope,
    AssetRetention,
    AssetScope,
    AssetSensitivity,
)

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


class MemoryClass(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryRevisionConflict(RuntimeError):
    """Raised when an optimistic memory write used a stale revision."""

    def __init__(self, scope: MemoryScope | str, expected: int, actual: int) -> None:
        normalized = coerce_memory_scope(scope)
        self.scope = normalized.value
        self.expected = int(expected)
        self.actual = int(actual)
        super().__init__(
            f"memory revision conflict ({self.scope}): "
            f"expected {self.expected}, actual {self.actual}"
        )


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
    revision: int = 1
    writer_id: str = ""
    memory_class: MemoryClass | str = MemoryClass.SEMANTIC
    last_verified_at: str = ""
    use_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_outcome: str = ""
    asset: Optional[AssetEnvelope] = None

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
        object.__setattr__(self, "revision", max(1, int(self.revision)))
        object.__setattr__(self, "writer_id", str(self.writer_id or ""))
        memory_class = str(
            self.memory_class or MemoryClass.SEMANTIC.value).strip().lower()
        aliases = {
            "note": MemoryClass.SEMANTIC,
            "fact": MemoryClass.SEMANTIC,
            "knowledge": MemoryClass.SEMANTIC,
            "preference": MemoryClass.SEMANTIC,
            "workflow": MemoryClass.PROCEDURAL,
        }
        try:
            memory_class_value = MemoryClass(
                aliases.get(memory_class, memory_class))
        except ValueError:
            memory_class_value = MemoryClass.SEMANTIC
        object.__setattr__(self, "memory_class", memory_class_value)
        object.__setattr__(
            self, "last_verified_at", str(self.last_verified_at or ""))
        for name in ("use_count", "success_count", "failure_count"):
            object.__setattr__(
                self, name, max(0, int(getattr(self, name) or 0)))
        object.__setattr__(
            self, "last_outcome", str(self.last_outcome or ""))
        if self.asset is None:
            object.__setattr__(self, "asset", AssetEnvelope(
                asset_id=self.id,
                asset_type="memory",
                scope={
                    MemoryScope.USER: AssetScope.USER,
                    MemoryScope.PROJECT: AssetScope.PROJECT,
                    MemoryScope.SESSION: AssetScope.SESSION,
                }[scope],
                owner=str(metadata.get("owner") or ""),
                tenant_id=str(metadata.get("tenant_id") or ""),
                organization_id=str(
                    metadata.get("organization_id") or ""),
                project_id=str(metadata.get("project_id") or ""),
                user_id=(
                    str(metadata.get("user_id") or "")
                    if scope is MemoryScope.USER else ""),
                session_id=(
                    str(metadata.get("session_id") or "")
                    if scope is MemoryScope.SESSION else ""),
                version=str(self.revision),
                sensitivity=str(
                    metadata.get("sensitivity")
                    or AssetSensitivity.INTERNAL.value),
                retention=str(
                    metadata.get("retention")
                    or (
                        AssetRetention.SESSION.value
                        if scope is MemoryScope.SESSION
                        else AssetRetention.PROJECT.value
                    )),
                expires_at=_optional_float(metadata.get("expires_at")),
                derived_from=_string_tuple(metadata.get("derived_from")),
                metadata={"memory_scope": scope.value},
            ))

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
            "revision": self.revision,
            "writer_id": self.writer_id,
            "memory_class": MemoryClass(self.memory_class).value,
            "last_verified_at": self.last_verified_at,
            "use_count": self.use_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_outcome": self.last_outcome,
            "asset": self.asset.to_dict() if self.asset is not None else {},
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


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _string_tuple(value: object) -> tuple[str, ...]:
    values: tuple[object, ...]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = tuple(value)
    else:
        values = ()
    return tuple(
        text for item in values
        if (text := str(item).strip())
    )


def memory_lifecycle_score(
    entry: MemoryEntry,
    *,
    now: float | None = None,
) -> float:
    """Rank verified and repeatedly successful memory above stale notes."""
    confidence = _optional_float(entry.metadata.get("confidence")) or 0.5
    attempts = entry.success_count + entry.failure_count
    success_rate = (
        entry.success_count / attempts if attempts else 0.5)
    verification = 0.0
    if entry.last_verified_at:
        try:
            verified = datetime.fromisoformat(entry.last_verified_at)
            current = (
                datetime.now(timezone.utc)
                if now is None
                else datetime.fromtimestamp(now, timezone.utc)
            )
            age_days = max(
                0.0, (current - verified.astimezone(timezone.utc)).days)
            verification = max(0.0, 1.0 - min(age_days / 90.0, 1.0))
        except (TypeError, ValueError):
            verification = 0.0
    return (
        confidence
        + success_rate
        + verification
        - min(entry.failure_count, 5) * 0.1
    )


def memory_asset_active(entry: MemoryEntry, *, now: float | None = None) -> bool:
    asset = entry.asset
    status = (
        getattr(asset.status, "value", asset.status) if asset is not None else ""
    )
    if asset is None or status != "active":
        return False
    current = now if now is not None else datetime.now(
        timezone.utc).timestamp()
    return asset.expires_at is None or asset.expires_at > current


def memory_asset_allowed(
    entry: MemoryEntry,
    *,
    principal_id: str = "",
    tenant_id: str = "",
    organization_id: str = "",
    project_id: str = "",
    session_id: str = "",
    task_id: str = "",
) -> bool:
    if not memory_asset_active(entry):
        return False
    asset = entry.asset
    if asset is None:
        return True
    allowed, _reason = asset.allows_identity(
        principal_id=principal_id,
        tenant_id=tenant_id,
        organization_id=organization_id,
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
    )
    return allowed
