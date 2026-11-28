"""Scoped, bounded and redacted memory storage.

Memory is intentionally separate from ``AGENTS.md``: notes are contextual
facts, not permanent behavioural rules.  The model has three scopes:

* ``user``    - private to the current OS user, shared across workspaces
* ``project`` - persisted under the current workspace's ``.krelcode``
* ``session`` - tied to one logical session; in-memory before session binding

The public path remains ``krelcode.session.memory``.  Storage, Markdown views,
evidence projection and migration live in focused sibling modules while this
facade preserves the original API and the single ``MemoryStore`` entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Optional, Protocol, cast

from ..agent.context import CONTEXT_ENGINE_V3
from ..agent.prompt.fragments import PromptFragment, refresh_registry_snapshot
from ..core.redact import MASK, SecretRedactor, redact_obvious_secrets
from .memory_context import MemoryStoreContextMixin
from .memory_lifecycle import MemoryStoreLifecycleMixin
from .memory_migration import MemoryStoreMigrationMixin
from .memory_models import (
    FRAGMENT_ID,
    INDEX_FRAGMENT_ID,
    INJECT_CHARS,
    INJECT_ENTRIES,
    LEGACY_FRAGMENT_ID,
    MAX_ENTRY_CHARS,
    MAX_ENTRIES,
    MAX_INDEX_CHARS,
    MAX_INDEX_ENTRIES,
    MAX_INDEX_LINE_CHARS,
    MAX_TAG_CHARS,
    MAX_TOPIC_CHARS,
    MAX_WHY_CHARS,
    MEMORY_SCOPE_ORDER,
    MEMORY_TYPE_ORDER,
    SCOPE_LABELS,
    STRUCTURED_MEMORY_TYPES,
    _SAFE_SESSION_ID_RE,
    _SCOPE_PREFIX_RE,
    _SENSITIVE_METADATA_KEY_RE,
    MemoryEntry,
    MemoryIndexSnapshot,
    MemoryKind,
    MemoryMigrationReport,
    MemoryRevisionConflict,
    MemoryScope,
    MemoryStoreSnapshot,
    MemoryTopic,
    MemoryType,
    coerce_memory_kind,
    coerce_memory_scope,
    coerce_memory_type,
    default_user_memory_path,
    parse_memory_note,
    validate_memory_structure,
)
from .memory_storage import MemoryStoreStorageMixin
from .memory_utils import (
    _ASSERTION_MARKERS,
    _NEGATION_MARKERS,
    _TEST_COMMAND_RE,
    _comparison_terms,
    _conflict_core,
    _entry_topic_slug,
    _one_line,
    _redact_obvious_secrets,
    extract_test_command,
    memory_digest,
    memory_texts_conflict,
    memory_topic_slug,
    normalize_memory_text,
    sanitize_memory_text,
)
from .memory_views import MemoryStoreViewMixin

if TYPE_CHECKING:
    from ..agent.turn.runtime_services import AgentServices
    from ..agent.turn.runtime_state import AgentRuntimeState


class _MemoryPromptHostPort(Protocol):
    services: "AgentServices"
    runtime_state: "AgentRuntimeState"


class MemoryStore(
    MemoryStoreStorageMixin,
    MemoryStoreViewMixin,
    MemoryStoreContextMixin,
    MemoryStoreLifecycleMixin,
    MemoryStoreMigrationMixin,
):
    """Manage user/project/session memory with common bounds and redaction."""


def sync_memory_fragment(
    loop: object, store: MemoryStore
) -> Optional[PromptFragment]:
    """Register/replace or remove the scoped-memory prompt fragment."""
    host = cast(_MemoryPromptHostPort, loop)
    registry = host.services.prompt.registry
    if registry is None:
        return None
    options = host.runtime_state.context.context_options
    engine = str(getattr(options, "engine", "") or "").strip().lower()
    if engine == CONTEXT_ENGINE_V3:
        # v3 injects memory through MemoryContextSource with an explicit
        # untrusted knowledge envelope. Keeping it in Prompt Registry would
        # accidentally promote it to a system instruction again.
        registry.remove(LEGACY_FRAGMENT_ID)
        registry.remove(FRAGMENT_ID)
        refresh_registry_snapshot(cast(Any, loop))
        return None
    fragment = store.fragment()
    registry.remove(LEGACY_FRAGMENT_ID)
    if fragment is None:
        registry.remove(FRAGMENT_ID)
    else:
        try:
            registry.upsert(fragment)
        except ValueError:
            registry.register(fragment)
    refresh_registry_snapshot(cast(Any, loop))
    return fragment


def memory_lines(entries: Iterable[MemoryEntry]) -> list[str]:
    items = tuple(entries)
    if not items:
        return ["当前没有记忆；输入 #内容 添加项目记忆"]
    scopes = {coerce_memory_scope(entry.scope) for entry in items}
    heading = (
        "项目记忆" if scopes == {MemoryScope.PROJECT} else "记忆")
    lines = [f"{heading}（最近 {len(items)} 条）:"]
    for index, entry in enumerate(reversed(items), 1):
        scope = coerce_memory_scope(entry.scope)
        lines.append(
            f"  #{index} [{scope.value}/{entry.kind_value}]"
            f" type={entry.memory_type_value} {entry.text}")
    return lines


__all__ = [
    "MemoryRevisionConflict",
    "MemoryStore",
    "memory_lines",
    "sync_memory_fragment",
]
