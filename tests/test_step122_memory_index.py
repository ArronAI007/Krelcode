"""Step 122: bounded MEMORY.md index and typed on-demand memory topics."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.agent.context_source import ContextRequest, MemoryContextSource
from krelcode.agent.fragments import (
    LAYER_L5_MODE_CONTRACT,
    STABILITY_STATIC,
    TRUST_MANAGED,
)
from krelcode.agent.memory_contract import (
    MEMORY_CONTRACT_ID,
    MEMORY_CONTRACT_PROMPT,
    memory_contract_fragment,
)
from krelcode.session.memory import (
    MAX_INDEX_ENTRIES,
    MAX_INDEX_LINE_CHARS,
    MemoryScope,
    MemoryStore,
    MemoryType,
    memory_topic_slug,
    validate_memory_structure,
)
from krelcode.session.memory_candidates import (
    AutoMemoryCoordinator,
    MemoryCandidateDraft,
    MemoryCandidateExtractor,
    MemoryCandidateQueue,
    MemoryCandidateTrigger,
    MemoryTurnSnapshot,
)
from krelcode.tools.base import ToolContext
from krelcode.session.memory_tool import MemorySearchTool


def make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(
        tmp_path,
        path=tmp_path / ".krelcode" / "memory" / "entries.jsonl",
        user_path=tmp_path / "home" / ".krelcode" / "memory" / "entries.jsonl",
    )


def request(tmp_path: Path) -> ContextRequest:
    return ContextRequest(
        turn_id="turn-122",
        step=1,
        history=(),
        workspace_root=tmp_path,
    )


def test_memory_md_is_an_index_and_user_bodies_stay_outside_workspace(tmp_path):
    store = make_store(tmp_path)
    project = store.add(
        "PROJECT_TOPIC_BODY_SENTINEL run make deploy before release",
        memory_type=MemoryType.PROJECT,
        topic="deployment",
        kind="knowledge",
        why="Deployment mistakes affect production.",
        how_to_apply="Use this as a checklist item before a release.",
    )
    user = store.add(
        "USER_PRIVATE_BODY_SENTINEL prefers Chinese explanations",
        scope=MemoryScope.USER,
        memory_type=MemoryType.USER,
        topic="communication",
    )

    project_index = store.index_path.read_text(encoding="utf-8")
    user_index = store.user_index_path.read_text(encoding="utf-8")
    project_topics = "\n".join(
        path.read_text(encoding="utf-8")
        for path in store.topics_dir.glob("*.md"))
    user_topics = "\n".join(
        path.read_text(encoding="utf-8")
        for path in store.user_topics_dir.glob("*.md"))

    assert "TOPIC_BODY_SENTINEL" not in project_index
    assert "USER_PRIVATE_BODY_SENTINEL" not in project_index
    assert "USER_PRIVATE_BODY_SENTINEL" not in project_topics
    assert project.id not in user_index
    assert "communication" in user_index
    assert "USER_PRIVATE_BODY_SENTINEL" in user_topics
    assert "**Why:**" in project_topics
    assert "**How to apply:**" in project_topics


def test_memory_index_is_strictly_bounded_by_lines_and_entries(tmp_path):
    store = make_store(tmp_path)
    for index in range(3):
        store.add(
            f"reference body {index} " + "x" * 500,
            memory_type=MemoryType.REFERENCE,
            topic=f"long-topic-{index}",
        )

    snapshot = store.index_snapshot(max_entries=2)
    lines = store.index_path.read_text(encoding="utf-8").splitlines()
    entry_lines = [line for line in lines if line.startswith("- [")]

    assert len(entry_lines) <= MAX_INDEX_ENTRIES
    assert all(len(line) <= MAX_INDEX_LINE_CHARS for line in entry_lines)
    assert len(snapshot.topics) == 2
    assert snapshot.truncated == 1


def test_feedback_and_project_require_why_and_how(tmp_path):
    store = make_store(tmp_path)
    for memory_type in (MemoryType.FEEDBACK, MemoryType.PROJECT):
        with pytest.raises(ValueError, match="Why.*How to apply"):
            store.add("structured memory without reasons", memory_type=memory_type)

    feedback = store.add(
        "Use uv for tool installation",
        memory_type=MemoryType.FEEDBACK,
        why="The user corrected the previous pip-based workflow.",
        how_to_apply="Prefer uv in related installation changes.",
    )
    reference = store.add(
        "Status page: https://status.example.com",
        memory_type=MemoryType.REFERENCE,
    )

    assert feedback.memory_type is MemoryType.FEEDBACK
    assert feedback.is_structured is True
    assert reference.memory_type is MemoryType.REFERENCE
    validate_memory_structure(MemoryType.REFERENCE, "", "")


def test_prompt_gets_only_index_and_topic_body_is_loaded_on_demand(tmp_path):
    store = make_store(tmp_path)
    entry = store.add(
        "TOPIC_BODY_ONLY_SENTINEL run the database migration rollback",
        memory_type=MemoryType.PROJECT,
        topic="database-migration",
        why="Rollback behavior prevents irreversible production changes.",
        how_to_apply="Verify down/rollback before approving a migration.",
    )
    source = MemoryContextSource(store)

    (layer,) = source.collect(request(tmp_path))
    tool = MemorySearchTool(store)
    loaded = asyncio.run(tool.execute(
        {"id": entry.id},
        ToolContext(workspace_root=tmp_path),
    ))

    assert "TOPIC_BODY_ONLY_SENTINEL" not in layer.content
    assert "MEMORY.md" in layer.content
    assert layer.metadata["memory_index_only"] is True
    assert layer.metadata["evidence_ids"] == (entry.id,)
    assert layer.metadata["memory_topics"] == (
        "topics/project-project-database-migration.md",)
    assert "TOPIC_BODY_ONLY_SENTINEL" in loaded.content
    assert "memory_type: project" in loaded.content
    assert "why: Rollback behavior" in loaded.content
    assert "how_to_apply: Verify down/rollback" in loaded.content


def test_dedicated_memory_contract_covers_types_not_save_and_state_boundaries():
    fragment = memory_contract_fragment()

    assert fragment.id == MEMORY_CONTRACT_ID
    assert fragment.layer == LAYER_L5_MODE_CONTRACT
    assert fragment.stability == STABILITY_STATIC
    assert fragment.trust == TRUST_MANAGED
    for phrase in (
        "user:", "feedback:", "project:", "reference:",
        "**Why:**", "**How to apply:**", "What NOT to save:",
        "Task owns", "Plan owns", "Memory owns", "Compact owns",
        "transient task state", "secrets, credentials",
    ):
        assert phrase in MEMORY_CONTRACT_PROMPT


def test_candidate_extractor_assigns_types_and_structures_required_memory():
    extractor = MemoryCandidateExtractor()
    explicit = extractor.extract(MemoryTurnSnapshot(
        user_input="请记住：项目使用 uv 安装工具"))
    correction = extractor.extract(MemoryTurnSnapshot(
        user_input="不对，应该是 pytest -q"))
    reference = extractor.extract(MemoryTurnSnapshot(
        user_input="请记住 reference: https://status.example.com"))

    assert explicit[0].memory_type is MemoryType.PROJECT
    assert explicit[0].why and explicit[0].how_to_apply
    assert correction[0].memory_type is MemoryType.FEEDBACK
    assert correction[0].why and correction[0].how_to_apply
    assert reference[0].memory_type is MemoryType.REFERENCE
    assert reference[0].why == ""
    assert reference[0].how_to_apply == ""


def test_incomplete_structured_candidate_is_pending_and_cannot_be_accepted(tmp_path):
    store = make_store(tmp_path)
    queue = MemoryCandidateQueue(
        tmp_path,
        path=tmp_path / ".krelcode" / "memory" / "candidates.jsonl",
    )
    candidate, _created = queue.merge(MemoryCandidateDraft(
        text="project fact without rationale",
        trigger=MemoryCandidateTrigger.EXPLICIT_REMEMBER,
        confidence=0.95,
        memory_type=MemoryType.PROJECT,
    ))
    coordinator = AutoMemoryCoordinator(
        SimpleNamespace(), store, queue=queue, mode="suggest")

    assert candidate is not None
    assert candidate.structurally_complete is False
    with pytest.raises(ValueError, match="Why.*How to apply"):
        coordinator.accept(candidate.id)
    assert store.list() == ()


def test_topic_slug_cannot_escape_generated_directory(tmp_path):
    store = make_store(tmp_path)
    entry = store.add(
        "unsafe path should be reduced to a filename",
        memory_type=MemoryType.REFERENCE,
        topic="../../.ssh/authorized_keys",
    )

    topic = store.topic(entry.id)
    assert topic is not None
    assert "/" not in topic.filename
    assert ".." not in topic.filename
    assert (store.topics_dir / topic.filename).is_file()
    assert memory_topic_slug("../../.ssh/authorized_keys") == "ssh-authorized-keys"


def test_remove_rebuilds_index_and_deletes_stale_topic_view(tmp_path):
    store = make_store(tmp_path)
    keep = store.add("keep this reference", memory_type="reference", topic="keep")
    remove = store.add(
        "remove this reference", memory_type="reference", topic="remove")
    stale = store.topics_dir / store.topic(remove.id).filename
    assert stale.is_file()

    store.remove(remove.id)

    index = store.index_path.read_text(encoding="utf-8")
    assert keep.id in {
        entry.id for topic in store.memory_topics() for entry in topic.entries}
    assert "remove" not in index
    assert stale.exists() is False
