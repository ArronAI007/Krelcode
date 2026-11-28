"""Evidence projection, topic lookup and dashboard snapshots."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ..agent.prompt.fragments import (
    GROUP_RUNTIME,
    LAYER_L7_EVIDENCE,
    STABILITY_TURN,
    PromptFragment,
    TRUST_UNTRUSTED,
)
from ..core.tokens import estimate_text_tokens
from ..core.evidence import (
    EvidenceRecord,
    EvidenceSource,
    evidence_metadata,
    evidence_record_from_memory,
    render_evidence_set,
)
from .memory_base import MemoryStoreBase
from .memory_models import (
    FRAGMENT_ID,
    INDEX_FRAGMENT_ID,
    INJECT_CHARS,
    INJECT_ENTRIES,
    MAX_INDEX_CHARS,
    MAX_INDEX_ENTRIES,
    MEMORY_SCOPE_ORDER,
    MemoryEntry,
    MemoryIndexSnapshot,
    MemoryScope,
    MemoryStoreSnapshot,
    MemoryTopic,
    coerce_memory_scope,
    coerce_memory_type,
    memory_asset_allowed,
    memory_lifecycle_score,
)
from .memory_utils import _entry_topic_slug


class MemoryStoreContextMixin(MemoryStoreBase):
    def refresh_index_views(self) -> None:
        """Refresh only MEMORY.md files; topic bodies are updated incrementally."""
        project_snapshot, user_snapshot = self._view_snapshots()
        if self.memory_dir.resolve(strict=False) == self.user_memory_dir.resolve(
            strict=False
        ):
            combined = self.index_snapshot(max_entries=MAX_INDEX_ENTRIES)
            self._atomic_write_text(
                self.index_path, combined.text, root=self.workspace_root)
            return
        self._atomic_write_text(
            self.index_path,
            project_snapshot.text,
            root=self.workspace_root,
        )
        self._atomic_write_text(
            self.user_index_path,
            user_snapshot.text,
            root=self.user_memory_dir,
        )

    def refresh_topic_for_entry(self, entry: MemoryEntry) -> Optional[MemoryTopic]:
        """Atomically refresh the one topic group affected by an add/update."""
        scope = coerce_memory_scope(entry.scope)
        slug = _entry_topic_slug(entry)
        memory_type = coerce_memory_type(entry.memory_type)
        expected_filename = (
            f"{scope.value}-{memory_type.value}-{slug}.md")
        match = next((
            topic for topic in self.memory_topics(scopes=(scope,))
            if topic.filename == expected_filename
        ), None)
        if match is None:
            return None
        if scope is MemoryScope.USER:
            root = self.user_memory_dir
            topics_dir = self.user_topics_dir
        else:
            root = self.workspace_root
            topics_dir = self.topics_dir
        self._atomic_write_text(
            topics_dir / match.filename,
            self._render_topic_markdown(match),
            root=root,
        )
        return match

    def refresh_markdown_views(self) -> MemoryIndexSnapshot:
        """Regenerate project/session and user views from authoritative JSONL.

        User-scoped bodies are written only below the user memory directory so a
        project-local Markdown view can never copy cross-workspace private data
        into the repository.
        """
        project_snapshot, user_snapshot = self._view_snapshots()
        if self.memory_dir.resolve(strict=False) == self.user_memory_dir.resolve(
            strict=False
        ):
            combined = self.index_snapshot(max_entries=MAX_INDEX_ENTRIES)
            self._refresh_view_root(
                combined,
                root=self.workspace_root,
                index_path=self.index_path,
                topics_dir=self.topics_dir,
            )
            return combined
        self._refresh_view_root(
            project_snapshot,
            root=self.workspace_root,
            index_path=self.index_path,
            topics_dir=self.topics_dir,
        )
        self._refresh_view_root(
            user_snapshot,
            root=self.user_memory_dir,
            index_path=self.user_index_path,
            topics_dir=self.user_topics_dir,
        )
        merged_topics = (*project_snapshot.topics, *user_snapshot.topics)
        merged_entries = (*project_snapshot.entries, *user_snapshot.entries)
        return MemoryIndexSnapshot(
            text=project_snapshot.text,
            topics=merged_topics,
            entries=merged_entries,
            truncated=project_snapshot.truncated + user_snapshot.truncated,
        )

    def topic(self, selector: str) -> Optional[MemoryTopic]:
        """Resolve a topic filename, slug, title, or contained entry id."""
        target = str(selector or "").strip()
        if not target:
            return None
        normalized = target.lower()
        for item in self.memory_topics():
            aliases = {
                item.filename.lower(),
                item.filename[:-3].lower() if item.filename.endswith(".md") else "",
                item.relative_path.lower(),
                item.title.lower(),
                *[entry.id.lower() for entry in item.entries],
            }
            if normalized in aliases:
                return item
        return None

    def render_context(
        self,
        *,
        max_entries: int = INJECT_ENTRIES,
        max_chars: int = INJECT_CHARS,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
        principal_id: str = "",
        tenant_id: str = "",
        organization_id: str = "",
        project_id: str = "",
        session_id: str = "",
        task_id: str = "",
    ) -> str:
        content, _entries = self.context_view(
            max_entries=max_entries,
            max_chars=max_chars,
            scopes=scopes,
            principal_id=principal_id,
            tenant_id=tenant_id,
            organization_id=organization_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
        )
        return content

    def context_view(
        self,
        *,
        max_entries: int = INJECT_ENTRIES,
        max_chars: int = INJECT_CHARS,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
        principal_id: str = "",
        tenant_id: str = "",
        organization_id: str = "",
        project_id: str = "",
        session_id: str = "",
        task_id: str = "",
    ) -> tuple[str, tuple[MemoryEntry, ...]]:
        """Render the injected memory block and its exact entry selection."""
        included = self._select_context_entries(
            max_entries=max_entries,
            max_chars=max_chars,
            scopes=scopes,
            principal_id=principal_id,
            tenant_id=tenant_id,
            organization_id=organization_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
        )
        content = self._render_context_entries(included, max_chars=max_chars)
        selected = tuple(
            entry for scope in MEMORY_SCOPE_ORDER
            for entry in included.get(scope, ())
        )
        return content, selected

    def context_selection(
        self,
        *,
        max_entries: int = INJECT_ENTRIES,
        max_chars: int = INJECT_CHARS,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
        principal_id: str = "",
        tenant_id: str = "",
        organization_id: str = "",
        project_id: str = "",
        session_id: str = "",
        task_id: str = "",
    ) -> tuple[MemoryEntry, ...]:
        """Return the exact entries selected by ``render_context``.

        Context sources use this content-free selection only for IDs, scopes and
        token attribution; the rendered bodies remain in the provider request.
        """
        _content, selected = self.context_view(
            max_entries=max_entries,
            max_chars=max_chars,
            scopes=scopes,
            principal_id=principal_id,
            tenant_id=tenant_id,
            organization_id=organization_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
        )
        return selected

    def index_view(
        self,
        *,
        max_entries: int = MAX_INDEX_ENTRIES,
        max_chars: int = MAX_INDEX_CHARS,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
        principal_id: str = "",
        tenant_id: str = "",
        organization_id: str = "",
        project_id: str = "",
        session_id: str = "",
        task_id: str = "",
    ) -> tuple[str, tuple[MemoryEntry, ...], tuple[MemoryTopic, ...]]:
        """Render the always-on index as one bounded Memory Evidence block."""
        snapshot = self.index_snapshot(
            scopes=scopes,
            max_entries=max_entries,
            max_chars=max_chars,
            principal_id=principal_id,
            tenant_id=tenant_id,
            organization_id=organization_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
        )
        if not snapshot.entries:
            return "", (), ()
        record = EvidenceRecord(
            id=INDEX_FRAGMENT_ID,
            source=EvidenceSource.MEMORY,
            content=snapshot.text,
            scope="mixed",
            kind="index",
            trust=TRUST_UNTRUSTED,
            confidence=0.5,
            requires_validation=True,
            origin="scoped_memory_index",
            metadata={
                "topic_count": len(snapshot.topics),
                "entry_count": len(snapshot.entries),
                "truncated": snapshot.truncated,
            },
        )
        content = render_evidence_set((record,), origin="scoped_memory_index")
        return content, snapshot.entries, snapshot.topics

    def _select_context_entries(
        self,
        *,
        max_entries: int,
        max_chars: int,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
        principal_id: str = "",
        tenant_id: str = "",
        organization_id: str = "",
        project_id: str = "",
        session_id: str = "",
        task_id: str = "",
    ) -> dict[MemoryScope, list[MemoryEntry]]:
        if max_entries <= 0 or max_chars <= 0:
            return {}
        selected_scopes = self._coerce_scopes(scopes)
        by_scope = {
            scope: [
                item for item in self.list(limit=0, scopes=(scope,))
                if memory_asset_allowed(
                    item,
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    organization_id=organization_id,
                    project_id=project_id,
                    session_id=session_id,
                    task_id=task_id,
                )
            ]
            for scope in selected_scopes
        }
        included: dict[MemoryScope, list[MemoryEntry]] = defaultdict(list)
        slots = max_entries
        # Allocate scarce prompt budget from most-specific to least-specific.
        for scope in reversed(MEMORY_SCOPE_ORDER):
            if scope not in selected_scopes or slots <= 0:
                continue
            ordered = [
                item for _index, item in sorted(
                    enumerate(by_scope[scope]),
                    key=lambda pair: (
                        pair[1].pinned,
                        memory_lifecycle_score(pair[1]),
                        pair[1].updated_at,
                        pair[0],
                    ),
                    reverse=True,
                )
            ]
            for entry in ordered:
                if slots <= 0:
                    break
                included[scope].append(entry)
                rendered = self._render_context_entries(
                    included, max_chars=max_chars)
                if not rendered:
                    included[scope].pop()
                    continue
                slots -= 1
        return included

    @staticmethod
    def _render_context_entries(
        included: Mapping[MemoryScope, list[MemoryEntry]],
        *,
        max_chars: int,
    ) -> str:
        if not included:
            return ""
        records = MemoryStoreContextMixin._evidence_records(included)
        rendered = render_evidence_set(records, origin="scoped_memory")
        return rendered if len(rendered) <= max_chars else ""

    @staticmethod
    def _evidence_records(
        included: Mapping[MemoryScope, list[MemoryEntry]],
    ) -> tuple[EvidenceRecord, ...]:
        return tuple(
            evidence_record_from_memory(entry)
            for scope in MEMORY_SCOPE_ORDER
            for entry in included.get(scope, ())
        )

    def fragment(self) -> Optional[PromptFragment]:
        content, selected, topics = self.index_view()
        if not content:
            return None
        scope_counts = {
            scope.value: sum(1 for entry in selected if entry.scope == scope)
            for scope in MEMORY_SCOPE_ORDER
        }
        records = tuple(evidence_record_from_memory(entry) for entry in selected)
        metadata = evidence_metadata(records, origin="scoped_memory_index", extra={
            "entries": len(selected),
            "scopes": scope_counts,
            "entry_ids": [entry.id for entry in selected],
            "topics": [topic.relative_path for topic in topics],
            "index_only": True,
            "tokens": estimate_text_tokens(content),
        })
        return PromptFragment(
            id=FRAGMENT_ID,
            source="memory",
            title="MEMORY.md index",
            content=content,
            group=GROUP_RUNTIME,
            priority=80,
            cacheable=False,
            compactable=True,
            toggleable=True,
            metadata=metadata,
            scope="mixed",
            layer=LAYER_L7_EVIDENCE,
            stability=STABILITY_TURN,
            trust=TRUST_UNTRUSTED,
        )

    def token_count(self) -> int:
        content, _entries, _topics = self.index_view()
        return estimate_text_tokens(content) if content else 0

    def dashboard_snapshot(self, *, limit: int = 8) -> MemoryStoreSnapshot:
        """Read all scopes once for TUI/diagnostic snapshots.

        ``refresh_panels`` and other high-frequency callers must use this
        snapshot instead of repeatedly invoking ``list`` / ``token_count``.
        """
        entries = self.list(limit=0)
        content, _selected, _topics = self.index_view()
        visible = entries[-limit:] if limit > 0 else entries
        return MemoryStoreSnapshot(
            entries=tuple(visible),
            total_entries=len(entries),
            tokens=estimate_text_tokens(content) if content else 0,
        )
