"""Incremental, source-attributed context attachments.

Attachments model volatile request context separately from the stable prompt
registry.  Each attachment owns a fixed id and revision.  Updating an
environment snapshot therefore cannot mutate the base prompt, project rules,
skills, or unrelated reminders.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass, field, replace
from datetime import date as local_date
from enum import Enum
from types import MappingProxyType
from typing import Dict, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from .context import WorkspaceContext
from .evidence import (
    EvidenceRecord,
    EvidenceSource,
    evidence_metadata,
    render_evidence_set,
)
from .fragments import (
    GROUP_RUNTIME,
    GROUP_SYSTEM,
    LAYER_L5_MODE_CONTRACT,
    LAYER_L6_CATALOG,
    LAYER_L7_EVIDENCE,
    LAYER_L8_ENVIRONMENT,
    LAYER_L12_REMINDER,
    LAYER_ORDER,
    PROMPT_STABILITY_LEVELS,
    PROMPT_TRUST_LEVELS,
    STABILITY_SESSION,
    STABILITY_TURN,
    TURN_ONLY_LAYERS,
    TRUST_DATA,
    TRUST_MANAGED,
    TRUST_PROJECT,
    TRUST_UNTRUSTED,
    PromptFragment,
)
from .tokens import estimate_text_tokens
from ..safety.contract import ModePermissionContract


class _CatalogItem(Protocol):
    name: str


class _SkillCatalogManager(Protocol):
    def list(self, *, model_invocable_only: bool = False) -> Sequence[_CatalogItem]: ...
    def prompt_index(self) -> str: ...


class _AgentCatalogItem(Protocol):
    name: str
    source: str
    description: str


class _AgentCatalogManager(Protocol):
    def list(self) -> Sequence[_AgentCatalogItem]: ...


class ContextAttachmentKind(str, Enum):
    ENVIRONMENT_SNAPSHOT = "environment_snapshot"
    ENVIRONMENT_DELTA = "environment_delta"
    DATE = "date"
    MODE_PERMISSION_CONTRACT = "mode_permission_contract"
    SKILL_CATALOG_DELTA = "skill_catalog_delta"
    AGENT_CATALOG_DELTA = "agent_catalog_delta"
    MCP_INSTRUCTIONS_DELTA = "mcp_instructions_delta"
    TOKEN_BUDGET_REMINDER = "token_budget_reminder"
    MODE_REMINDER = "mode_reminder"


ATTACHMENT_KIND_ORDER = tuple(ContextAttachmentKind)
ATTACHMENT_ID_PREFIX = "attachment:"

_DEFAULT_LAYER = {
    ContextAttachmentKind.ENVIRONMENT_SNAPSHOT: LAYER_L8_ENVIRONMENT,
    ContextAttachmentKind.ENVIRONMENT_DELTA: LAYER_L8_ENVIRONMENT,
    ContextAttachmentKind.DATE: LAYER_L8_ENVIRONMENT,
    ContextAttachmentKind.MODE_PERMISSION_CONTRACT: LAYER_L5_MODE_CONTRACT,
    ContextAttachmentKind.SKILL_CATALOG_DELTA: LAYER_L6_CATALOG,
    ContextAttachmentKind.AGENT_CATALOG_DELTA: LAYER_L6_CATALOG,
    ContextAttachmentKind.MCP_INSTRUCTIONS_DELTA: LAYER_L7_EVIDENCE,
    ContextAttachmentKind.TOKEN_BUDGET_REMINDER: LAYER_L12_REMINDER,
    ContextAttachmentKind.MODE_REMINDER: LAYER_L12_REMINDER,
}
_DEFAULT_STABILITY = {
    ContextAttachmentKind.ENVIRONMENT_SNAPSHOT: STABILITY_TURN,
    ContextAttachmentKind.ENVIRONMENT_DELTA: STABILITY_TURN,
    ContextAttachmentKind.DATE: STABILITY_SESSION,
    ContextAttachmentKind.MODE_PERMISSION_CONTRACT: STABILITY_TURN,
    ContextAttachmentKind.SKILL_CATALOG_DELTA: STABILITY_SESSION,
    ContextAttachmentKind.AGENT_CATALOG_DELTA: STABILITY_SESSION,
    ContextAttachmentKind.MCP_INSTRUCTIONS_DELTA: STABILITY_SESSION,
    ContextAttachmentKind.TOKEN_BUDGET_REMINDER: STABILITY_TURN,
    ContextAttachmentKind.MODE_REMINDER: STABILITY_TURN,
}
_DEFAULT_TRUST = {
    ContextAttachmentKind.ENVIRONMENT_SNAPSHOT: TRUST_DATA,
    ContextAttachmentKind.ENVIRONMENT_DELTA: TRUST_DATA,
    ContextAttachmentKind.DATE: TRUST_MANAGED,
    ContextAttachmentKind.MODE_PERMISSION_CONTRACT: TRUST_MANAGED,
    ContextAttachmentKind.SKILL_CATALOG_DELTA: TRUST_PROJECT,
    ContextAttachmentKind.AGENT_CATALOG_DELTA: TRUST_PROJECT,
    ContextAttachmentKind.MCP_INSTRUCTIONS_DELTA: TRUST_UNTRUSTED,
    ContextAttachmentKind.TOKEN_BUDGET_REMINDER: TRUST_MANAGED,
    ContextAttachmentKind.MODE_REMINDER: TRUST_MANAGED,
}
_DEFAULT_PRIORITY = {
    ContextAttachmentKind.ENVIRONMENT_SNAPSHOT: 20,
    ContextAttachmentKind.ENVIRONMENT_DELTA: 21,
    ContextAttachmentKind.DATE: 0,
    ContextAttachmentKind.MODE_PERMISSION_CONTRACT: 0,
    ContextAttachmentKind.SKILL_CATALOG_DELTA: 20,
    ContextAttachmentKind.AGENT_CATALOG_DELTA: 30,
    ContextAttachmentKind.MCP_INSTRUCTIONS_DELTA: 40,
    ContextAttachmentKind.TOKEN_BUDGET_REMINDER: 10,
    ContextAttachmentKind.MODE_REMINDER: 20,
}


def attachment_id(kind: ContextAttachmentKind | str) -> str:
    normalized = ContextAttachmentKind(kind)
    return f"{ATTACHMENT_ID_PREFIX}{normalized.value}"


@dataclass(frozen=True)
class ContextAttachment:
    """One independently replaceable dynamic context block."""

    id: str
    kind: ContextAttachmentKind
    content: str
    layer: str = ""
    stability: str = ""
    trust: str = ""
    scope: str = "session"
    priority: int = -1
    cacheable: bool = False
    compactable: bool = True
    revision: int = 1
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = ContextAttachmentKind(self.kind)
        object.__setattr__(self, "kind", kind)
        if not self.id:
            raise ValueError("context attachment 必须包含非空 id")
        object.__setattr__(self, "layer", self.layer or _DEFAULT_LAYER[kind])
        object.__setattr__(
            self, "stability", self.stability or _DEFAULT_STABILITY[kind])
        object.__setattr__(
            self, "trust", self.trust or _DEFAULT_TRUST[kind])
        if self.priority < 0:
            object.__setattr__(self, "priority", _DEFAULT_PRIORITY[kind])
        if self.layer not in LAYER_ORDER:
            raise ValueError(f"未知 attachment layer: {self.layer}")
        if self.stability not in PROMPT_STABILITY_LEVELS:
            raise ValueError(f"未知 attachment stability: {self.stability}")
        if self.trust not in PROMPT_TRUST_LEVELS:
            raise ValueError(f"未知 attachment trust: {self.trust}")
        if self.layer in TURN_ONLY_LAYERS and self.stability != STABILITY_TURN:
            raise ValueError(
                f"{self.layer} attachment 必须是 turn stability: {self.id}")
        if self.cacheable:
            raise ValueError(
                f"动态 context attachment 不允许进入稳定缓存前缀: {self.id}")
        if self.stability == STABILITY_TURN and self.cacheable:
            raise ValueError(f"turn attachment 不允许 cacheable: {self.id}")
        object.__setattr__(
            self, "metadata",
            MappingProxyType(dict(self.metadata or {})),
        )

    @property
    def tokens(self) -> int:
        return estimate_text_tokens(self.content)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def semantic_key(self) -> str:
        payload = {
            "id": self.id,
            "kind": self.kind.value,
            "content": self.content,
            "layer": self.layer,
            "stability": self.stability,
            "trust": self.trust,
            "scope": self.scope,
            "priority": self.priority,
            "cacheable": self.cacheable,
            "compactable": self.compactable,
            "metadata": dict(self.metadata),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_fragment(self) -> PromptFragment:
        group = (
            GROUP_SYSTEM
            if self.layer == LAYER_L6_CATALOG else GROUP_RUNTIME
        )
        metadata = dict(self.metadata)
        metadata.update({
            "attachment_id": self.id,
            "attachment_kind": self.kind.value,
            "attachment_revision": self.revision,
        })
        return PromptFragment(
            id=self.id,
            source=f"attachment:{self.kind.value}",
            title=f"Context attachment: {self.kind.value}",
            content=self.content.strip(),
            group=group,
            priority=self.priority,
            cacheable=False,
            compactable=self.compactable,
            toggleable=False,
            metadata=metadata,
            scope=self.scope,
            layer=self.layer,
            stability=self.stability,
            trust=self.trust,
        )


@dataclass(frozen=True)
class ContextAttachmentSnapshot:
    items: Tuple[ContextAttachment, ...]
    generation: int

    def get(self, item_id: str) -> Optional[ContextAttachment]:
        return next((item for item in self.items if item.id == item_id), None)

    @property
    def total_tokens(self) -> int:
        return sum(item.tokens for item in self.items)


@dataclass(frozen=True)
class ContextAttachmentDiff:
    """Content-free revision diff suitable for logs and diagnostics."""

    previous_generation: int
    generation: int
    added: Tuple[str, ...] = ()
    updated: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    revisions: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "revisions", MappingProxyType(
            dict(self.revisions)))

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    @property
    def changed_ids(self) -> Tuple[str, ...]:
        return tuple(sorted((*self.added, *self.updated, *self.removed)))

    def summary_lines(self) -> Tuple[str, ...]:
        if not self.changed:
            return ("attachments unchanged",)
        parts = []
        if self.added:
            parts.append(f"added {', '.join(self.added)}")
        if self.updated:
            parts.append(f"updated {', '.join(self.updated)}")
        if self.removed:
            parts.append(f"removed {', '.join(self.removed)}")
        return (f"attachments generation {self.generation}: " + " · ".join(parts),)


class ContextAttachmentStore:
    """Mutable runtime store with stable IDs and per-item revisions."""

    def __init__(self, items: Iterable[ContextAttachment] = ()) -> None:
        self._items: Dict[str, ContextAttachment] = {}
        self._generation = 0
        for item in items:
            self.upsert(item)

    @property
    def generation(self) -> int:
        return self._generation

    def get(self, item_id: str) -> Optional[ContextAttachment]:
        return self._items.get(item_id)

    def upsert(self, item: ContextAttachment) -> ContextAttachment:
        existing = self._items.get(item.id)
        if existing is not None and existing.semantic_key == item.semantic_key:
            return existing
        revision = (
            existing.revision + 1
            if existing is not None else max(1, int(item.revision))
        )
        stored = replace(item, revision=revision)
        self._items[item.id] = stored
        self._generation += 1
        return stored

    def remove(self, item_id: str) -> bool:
        existed = self._items.pop(item_id, None) is not None
        if existed:
            self._generation += 1
        return existed

    def clear(self) -> None:
        if self._items:
            self._items.clear()
            self._generation += 1

    def snapshot(self) -> ContextAttachmentSnapshot:
        ordered = sorted(self._items.values(), key=_attachment_sort_key)
        return ContextAttachmentSnapshot(
            items=tuple(ordered), generation=self._generation)

    def fragments(self) -> Tuple[PromptFragment, ...]:
        return tuple(item.to_fragment() for item in self.snapshot().items)

    def diff(
        self, previous: Optional[ContextAttachmentSnapshot],
    ) -> ContextAttachmentDiff:
        previous = previous or ContextAttachmentSnapshot((), 0)
        old = {item.id: item for item in previous.items}
        new = self._items
        added = tuple(sorted(new.keys() - old.keys()))
        removed = tuple(sorted(old.keys() - new.keys()))
        updated = tuple(sorted(
            item_id for item_id in new.keys() & old.keys()
            if new[item_id].semantic_key != old[item_id].semantic_key
        ))
        revisions = {
            item_id: item.revision
            for item_id, item in new.items()
        }
        return ContextAttachmentDiff(
            previous_generation=previous.generation,
            generation=self._generation,
            added=added,
            updated=updated,
            removed=removed,
            revisions=revisions,
        )


def context_attachment_fragments(
    items: Sequence[ContextAttachment],
) -> Tuple[PromptFragment, ...]:
    return tuple(item.to_fragment() for item in items if item.content.strip())


def date_attachment(value: Optional[str] = None) -> ContextAttachment:
    current = value or local_date.today().isoformat()
    return ContextAttachment(
        id=attachment_id(ContextAttachmentKind.DATE),
        kind=ContextAttachmentKind.DATE,
        content=f"<date>{html.escape(current, quote=False)}</date>",
        metadata={"date": current},
    )


def mode_permission_contract_attachment(
    contract: ModePermissionContract,
) -> ContextAttachment:
    return ContextAttachment(
        id=attachment_id(ContextAttachmentKind.MODE_PERMISSION_CONTRACT),
        kind=ContextAttachmentKind.MODE_PERMISSION_CONTRACT,
        content=contract.render(),
        scope="managed",
        priority=0,
        compactable=False,
        metadata=contract.metadata(),
    )


def mode_reminder_attachment(run_mode: object, permission_mode: object) -> ContextAttachment:
    run = _enum_value(run_mode)
    permission = _enum_value(permission_mode)
    return ContextAttachment(
        id=attachment_id(ContextAttachmentKind.MODE_REMINDER),
        kind=ContextAttachmentKind.MODE_REMINDER,
        content=(
            "<mode_reminder>\n"
            f"Run mode: {html.escape(run, quote=False)}\n"
            f"Permission mode: {html.escape(permission, quote=False)}\n"
            "</mode_reminder>"
        ),
        metadata={"run_mode": run, "permission_mode": permission},
    )


def token_budget_reminder_attachment(
    limit: int,
    used: int,
) -> ContextAttachment:
    limit = max(0, int(limit))
    used = max(0, int(used))
    remaining = max(0, limit - used)
    return ContextAttachment(
        id=attachment_id(ContextAttachmentKind.TOKEN_BUDGET_REMINDER),
        kind=ContextAttachmentKind.TOKEN_BUDGET_REMINDER,
        content=(
            "<token_budget_reminder>\n"
            f"Token budget: {limit}\n"
            f"Used: {used}\n"
            f"Remaining: {remaining}\n"
            "This is a hard budget for the current turn; avoid unnecessary context.\n"
            "</token_budget_reminder>"
        ),
        metadata={"limit": limit, "used": used, "remaining": remaining},
    )


def environment_attachments(
    bundle: WorkspaceContext,
    previous_snapshot: Optional[ContextAttachment],
) -> Tuple[ContextAttachment, ...]:
    """Build snapshot/delta attachments, returning empty on no environment change."""
    sections = tuple(
        section for section in bundle.sections if section.name != "rules")
    hashes = {
        section.name: hashlib.sha256(
            section.content.encode("utf-8")).hexdigest()
        for section in sections
    }
    section_order = tuple(section.name for section in sections)
    previous_hashes = _metadata_string_map(
        previous_snapshot, "section_hashes")
    previous_order = _metadata_string_sequence(
        previous_snapshot, "section_order")
    if (
        previous_snapshot is not None
        and previous_hashes == hashes
        and previous_order == section_order
    ):
        return ()

    current_names = set(hashes)
    previous_names = set(previous_hashes)
    added = tuple(sorted(current_names - previous_names))
    removed = tuple(sorted(previous_names - current_names))
    changed = tuple(sorted(
        name for name in current_names & previous_names
        if hashes[name] != previous_hashes[name]
    ))
    initial = previous_snapshot is None

    body: list[str] = []
    for section in sections:
        body.extend((
            f'<section name="{html.escape(section.name, quote=True)}" '
            f'source="{html.escape(section.source, quote=True)}">',
            html.escape(section.content, quote=False),
            "</section>",
        ))
    snapshot_content = (
        '<environment_snapshot trust="data">\n'
        + "\n".join(body)
        + "\n</environment_snapshot>"
    )
    metadata: dict[str, object] = {
        "section_hashes": hashes,
        "section_sources": {
            section.name: section.source for section in sections},
        "section_order": section_order,
        "workspace_root": str(bundle.root),
        "initial": initial,
    }
    snapshot = ContextAttachment(
        id=attachment_id(ContextAttachmentKind.ENVIRONMENT_SNAPSHOT),
        kind=ContextAttachmentKind.ENVIRONMENT_SNAPSHOT,
        content=snapshot_content,
        scope="project",
        metadata=metadata,
    )

    if initial:
        delta_content = (
            "<environment_delta initial=\"true\">\n"
            "Environment baseline established. Subsequent changes are incremental.\n"
            "</environment_delta>"
        )
    else:
        lines = []
        if added:
            lines.append("Added sections: " + ", ".join(added))
        if removed:
            lines.append("Removed sections: " + ", ".join(removed))
        if changed:
            lines.append("Changed sections: " + ", ".join(changed))
        if previous_order and previous_order != section_order:
            lines.append("Section order changed.")
        delta_content = "<environment_delta>\n" + "\n".join(lines) + "\n</environment_delta>"
    delta = ContextAttachment(
        id=attachment_id(ContextAttachmentKind.ENVIRONMENT_DELTA),
        kind=ContextAttachmentKind.ENVIRONMENT_DELTA,
        content=delta_content,
        scope="project",
        metadata={
            "initial": initial,
            "added_sections": added,
            "removed_sections": removed,
            "changed_sections": changed,
            "current_hashes": hashes,
            "previous_hashes": previous_hashes,
        },
    )
    return snapshot, delta


def skill_catalog_attachment(
    manager: _SkillCatalogManager,
    previous: Optional[ContextAttachment] = None,
) -> Optional[ContextAttachment]:
    items = tuple(manager.list(model_invocable_only=True))
    names = tuple(sorted(str(item.name) for item in items))
    if not items:
        return None
    content = str(manager.prompt_index()).strip()
    if not content:
        return None
    return _catalog_attachment(
        ContextAttachmentKind.SKILL_CATALOG_DELTA,
        content,
        names,
        previous,
    )


def agent_catalog_attachment(
    manager: Optional[_AgentCatalogManager],
    previous: Optional[ContextAttachment] = None,
) -> Optional[ContextAttachment]:
    if manager is None:
        return None
    definitions = tuple(manager.list())
    if not definitions:
        return None
    lines = [
        "<agent_catalog>",
        "Available built-in and custom subagent definitions. Use spawn_agent(definition=...) when a task matches; omission selects general-purpose.",
    ]
    names = []
    for definition in definitions:
        name = str(definition.name)
        names.append(name)
        source = html.escape(str(definition.source or "custom"), quote=True)
        description = html.escape(str(definition.description), quote=False)
        line = (
            f'- <agent name="{html.escape(name, quote=True)}" source="{source}">'
            f"{description[:500]}</agent>"
        )
        if sum(len(item) + 1 for item in lines) + len(line) > 12_000:
            lines.append("- …[agent catalog truncated]")
            break
        lines.append(line)
    lines.append("</agent_catalog>")
    return _catalog_attachment(
        ContextAttachmentKind.AGENT_CATALOG_DELTA,
        "\n".join(lines),
        tuple(sorted(names)),
        previous,
    )


def mcp_instructions_attachment(
    supervisor: Optional[object],
    previous: Optional[ContextAttachment] = None,
) -> Optional[ContextAttachment]:
    if supervisor is None:
        return None
    provider = getattr(supervisor, "server_instructions", None)
    if not callable(provider):
        return None
    entries = tuple(provider())
    entries = tuple((str(name), str(text).strip()) for name, text in entries
                    if str(name).strip() and str(text).strip())
    if not entries:
        return None
    names = tuple(sorted(name for name, _text in entries))
    records: list[EvidenceRecord] = []
    remaining = 12_000
    for name, text in sorted(entries, key=lambda item: item[0]):
        if remaining <= 0:
            break
        bounded = _truncate_escaped_text(text, remaining)
        if not bounded:
            continue
        records.append(EvidenceRecord(
            id=f"mcp:{name}",
            source=EvidenceSource.MCP,
            content=bounded,
            scope="server",
            kind="instructions",
            trust=TRUST_UNTRUSTED,
            requires_validation=True,
            origin=name,
            metadata={"server": name},
        ))
        remaining -= len(html.escape(bounded, quote=False))
    if not records:
        return None
    attachment = _catalog_attachment(
        ContextAttachmentKind.MCP_INSTRUCTIONS_DELTA,
        render_evidence_set(records, origin="mcp_instructions"),
        names,
        previous,
    )
    metadata = dict(attachment.metadata)
    metadata.update(evidence_metadata(
        records,
        origin="mcp_instructions",
        extra={"mcp_servers": names},
    ))
    return replace(attachment, metadata=metadata)


def _truncate_escaped_text(text: str, max_escaped_chars: int) -> str:
    """Bound raw text by its escaped provider representation."""
    if max_escaped_chars <= 0:
        return ""
    raw = str(text)
    if len(html.escape(raw, quote=False)) <= max_escaped_chars:
        return raw
    low, high = 0, len(raw)
    while low < high:
        middle = (low + high + 1) // 2
        if len(html.escape(raw[:middle], quote=False)) <= max_escaped_chars:
            low = middle
        else:
            high = middle - 1
    if low <= 0:
        return ""
    return raw[:low].rstrip() + "…" if low < len(raw) else raw[:low]


def _catalog_attachment(
    kind: ContextAttachmentKind,
    content: str,
    names: Sequence[str],
    previous: Optional[ContextAttachment],
) -> ContextAttachment:
    previous_names = set(_metadata_string_sequence(previous, "catalog_names"))
    current_names = set(names)
    added = tuple(sorted(current_names - previous_names))
    removed = tuple(sorted(previous_names - current_names))
    body_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    previous_body_hash = (
        str(previous.metadata.get("catalog_body_hash", ""))
        if previous is not None else ""
    )
    if (
        previous is not None
        and not added
        and not removed
        and previous_body_hash == body_hash
    ):
        return previous
    initial = previous is None
    header = (
        f'<catalog_delta kind="{kind.value}" initial="true">\n'
        if initial else
        f'<catalog_delta kind="{kind.value}">\n'
    )
    delta_lines = []
    if added:
        delta_lines.append("Added: " + ", ".join(added))
    if removed:
        delta_lines.append("Removed: " + ", ".join(removed))
    if not initial and not delta_lines and previous_body_hash != body_hash:
        delta_lines.append("Catalog content changed.")
    body = "\n".join(delta_lines)
    return ContextAttachment(
        id=attachment_id(kind),
        kind=kind,
        content=f"{header}{body}\n{content}\n</catalog_delta>",
        scope=(
            "session"
            if kind is ContextAttachmentKind.MCP_INSTRUCTIONS_DELTA
            else "project"
        ),
        metadata={
            "catalog_names": tuple(sorted(current_names)),
            "added": added,
            "removed": removed,
            "initial": initial,
            "catalog_body_hash": body_hash,
        },
    )


def _metadata_string_map(
    attachment: Optional[ContextAttachment], key: str,
) -> Dict[str, str]:
    if attachment is None:
        return {}
    raw = attachment.metadata.get(key, {})
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _metadata_string_sequence(
    attachment: Optional[ContextAttachment], key: str,
) -> Tuple[str, ...]:
    if attachment is None:
        return ()
    raw = attachment.metadata.get(key, ())
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item) for item in raw)


def _attachment_sort_key(item: ContextAttachment) -> Tuple[int, str]:
    try:
        kind_index = ATTACHMENT_KIND_ORDER.index(item.kind)
    except ValueError:
        kind_index = len(ATTACHMENT_KIND_ORDER)
    return kind_index, item.id


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "unknown")


__all__ = [
    "ATTACHMENT_ID_PREFIX",
    "ATTACHMENT_KIND_ORDER",
    "ContextAttachment",
    "ContextAttachmentDiff",
    "ContextAttachmentKind",
    "ContextAttachmentSnapshot",
    "ContextAttachmentStore",
    "agent_catalog_attachment",
    "attachment_id",
    "context_attachment_fragments",
    "date_attachment",
    "environment_attachments",
    "mcp_instructions_attachment",
    "mode_permission_contract_attachment",
    "mode_reminder_attachment",
    "skill_catalog_attachment",
    "token_budget_reminder_attachment",
]
