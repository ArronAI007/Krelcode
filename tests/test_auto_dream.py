"""AutoDream proposal generation and reviewed merge tests."""

from __future__ import annotations

from pathlib import Path

from krelcode.session.memory import MemoryStore
from krelcode.session.memory_dream import (
    DreamKind,
    DreamStatus,
    MemoryDreamCoordinator,
    MemoryDreamPlanner,
    MemoryDreamStore,
)


def _store(root: Path) -> MemoryStore:
    return MemoryStore(root, user_path=root / "user.jsonl")


def test_dream_planner_finds_duplicate_conflict_topic_and_missing_path(
        tmp_path: Path):
    store = _store(tmp_path)
    store.add("项目测试命令是 pytest -q")
    store.add("项目测试命令是 pytest -q")
    store.add("项目使用 asyncio")
    store.add("项目不使用 asyncio")
    for index in range(3):
        store.add(f"架构说明 {index}", topic="architecture")
    store.add(f"入口位于 {tmp_path}/missing/file.py")

    proposals = MemoryDreamPlanner().plan(store.list(limit=0))
    kinds = {proposal.kind for proposal in proposals}

    assert DreamKind.MERGE_DUPLICATES in kinds
    assert DreamKind.RESOLVE_CONFLICT in kinds
    assert DreamKind.CONSOLIDATE_TOPIC in kinds
    assert DreamKind.REVALIDATE_PATH in kinds


def test_accept_duplicate_merge_is_reviewed_and_project_scoped(tmp_path: Path):
    store = _store(tmp_path)
    first = store.add("项目使用 pytest", source="manual")
    second = store.add("项目使用 pytest", source="manual")
    coordinator = MemoryDreamCoordinator(
        store, MemoryDreamStore(tmp_path))
    proposals = coordinator.run()
    merge = next(
        item for item in proposals
        if item.kind is DreamKind.MERGE_DUPLICATES
    )

    accepted = coordinator.accept(merge.id)
    assert accepted is not None
    assert accepted.status is DreamStatus.ACCEPTED
    entries = store.list(limit=0, scopes=("project",))
    assert len(entries) == 1
    assert entries[0].text == "项目使用 pytest"
    assert entries[0].source in {"manual", "auto-dream"}
    assert second.id != entries[0].id


def test_dream_queue_deduplicates_same_proposal_across_runs(tmp_path: Path):
    store = _store(tmp_path)
    store.add("项目测试命令是 pytest")
    store.add("项目测试命令是 pytest")
    coordinator = MemoryDreamCoordinator(
        store, MemoryDreamStore(tmp_path))

    first = coordinator.run()
    second = coordinator.run()

    assert first
    assert second == ()
    assert len(coordinator.dream_store.list(status=DreamStatus.PENDING)) == len(first)
