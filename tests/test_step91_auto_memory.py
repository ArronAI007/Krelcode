"""Step 91: evidence-backed Auto Memory and review workflow."""

from __future__ import annotations

import asyncio
from argparse import Namespace
from pathlib import Path

import pytest

from krelcode.agent.fragments import PromptFragmentRegistry, base_fragment
from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus, RunFinished, RunStarted
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands import builtin_command_registry
from krelcode.session.controller import SessionController
from krelcode.session.memory import (
    FRAGMENT_ID,
    MAX_ENTRIES,
    MemoryKind,
    MemoryScope,
    MemoryStore,
    memory_texts_conflict,
    sync_memory_fragment,
)
from krelcode.session.memory_candidates import (
    AutoMemoryCoordinator,
    AutoMemoryMode,
    MemoryCandidate,
    MemoryCandidateDraft,
    MemoryCandidateExtractor,
    MemoryCandidateQueue,
    MemoryCandidateStatus,
    MemoryCandidateTrigger,
    MemoryEvidence,
    MemoryTurnSnapshot,
)
from krelcode.tools.base import ToolContext
from krelcode.session.memory_tool import MemorySearchTool
from krelcode.tools.registry import ToolRegistry


def make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(
        tmp_path, user_path=tmp_path / "home" / "entries.jsonl")


def make_coordinator(
    tmp_path: Path,
    *,
    mode: AutoMemoryMode | str = AutoMemoryMode.SUGGEST,
    store: MemoryStore | None = None,
) -> tuple[AutoMemoryCoordinator, MemoryStore, AgentLoop]:
    store = store or make_store(tmp_path)
    loop = AgentLoop(
        llm=MockProvider(fallback="ok"),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="BASE",
    )
    loop.prompt_registry = PromptFragmentRegistry([base_fragment()])
    loop.memory_store = store
    coordinator = AutoMemoryCoordinator(
        loop,
        store,
        mode=mode,
        queue=MemoryCandidateQueue(
            tmp_path, path=tmp_path / ".krelcode" / "memory" / "candidates.jsonl"),
        on_memory_changed=lambda: sync_memory_fragment(loop, store),
    )
    loop.auto_memory = coordinator
    return coordinator, store, loop


def tool_result(command: str, *, exit_code: int = 0) -> Message:
    return Message.tool_result(
        f"call-{command}",
        f"exit_code: {exit_code}",
        tool_name="run_command",
        is_error=exit_code != 0,
        metadata={"command": command, "exit_code": exit_code},
    )


def test_explicit_remember_is_reviewable_knowledge_and_suggest_never_writes(tmp_path):
    coordinator, store, _loop = make_coordinator(tmp_path, mode="suggest")

    report = coordinator.process_turn(
        "请记住：项目所有测试统一使用 pytest -q",
        (Message.assistant("已记录到候选。"),),
        turn_id="turn-1",
        session_id="session-1",
    )

    assert report.queued_count == 1
    assert report.accepted == ()
    assert store.list() == ()
    candidate = report.extracted[0]
    assert candidate.kind is MemoryKind.KNOWLEDGE
    assert candidate.scope is MemoryScope.PROJECT
    assert candidate.status is MemoryCandidateStatus.PENDING
    assert candidate.trigger is MemoryCandidateTrigger.EXPLICIT_REMEMBER
    assert candidate.evidence[0].turn_id == "turn-1"


def test_user_correction_extracts_a_candidate(tmp_path):
    coordinator, store, _loop = make_coordinator(tmp_path, mode="auto")

    report = coordinator.process_turn(
        "不对，应该是 CLI 入口位于 src/krelcode/entrypoints/cli.py",
        (Message.assistant("明白，已更正。"),),
        turn_id="correct-1",
    )

    assert report.accepted
    assert len(store.list()) == 1
    entry = store.list()[0]
    assert entry.kind is MemoryKind.KNOWLEDGE
    assert "entrypoints/cli.py" in entry.text


def test_successful_test_command_reuse_accumulates_evidence_then_auto_writes(tmp_path):
    coordinator, store, _loop = make_coordinator(tmp_path, mode="auto")

    first = coordinator.process_turn(
        "运行测试", (tool_result("pytest -q"),), turn_id="test-1")
    assert first.accepted == ()
    assert store.list() == ()
    candidate = first.extracted[0]
    assert candidate.status is MemoryCandidateStatus.PENDING
    assert candidate.confidence == pytest.approx(0.70)

    second = coordinator.process_turn(
        "再运行一次测试", (tool_result("pytest -q"),), turn_id="test-2")
    accepted = second.accepted[0]
    assert accepted.status is MemoryCandidateStatus.ACCEPTED
    assert accepted.confidence == pytest.approx(0.80)
    assert len(accepted.evidence) == 2
    assert len(store.list()) == 1
    assert store.list()[0].text == "验证命令：pytest -q"


def test_architecture_pattern_reaches_auto_threshold_only_after_reconfirmation(tmp_path):
    coordinator, store, _loop = make_coordinator(tmp_path, mode="auto")
    statement = "项目架构采用分层架构，模块之间通过协议解耦"

    reports = [
        coordinator.process_turn(
            statement, (Message.assistant("确认。"),), turn_id=f"arch-{index}")
        for index in range(1, 4)
    ]

    assert reports[0].accepted == reports[1].accepted == ()
    assert reports[2].accepted
    assert reports[2].accepted[0].confidence >= 0.80
    assert len(store.list()) == 1


def test_failed_or_temporary_commands_never_become_long_term_memory(tmp_path):
    coordinator, store, _loop = make_coordinator(tmp_path, mode="auto")

    report = coordinator.process_turn(
        "刚才这个临时测试失败了",
        (tool_result("pytest -q", exit_code=1), Message.assistant("测试失败，待排查。")),
        turn_id="failed-1",
    )

    assert report.extracted == ()
    assert store.list() == ()
    assert coordinator.queue.list(limit=0) == ()


def test_model_generated_candidate_kind_is_forced_to_knowledge():
    candidate = MemoryCandidate(
        id="c1",
        text="项目使用 asyncio",
        trigger=MemoryCandidateTrigger.TASK_COMPLETION,
        confidence=0.65,
        digest="digest",
        project_id="project",
        created_at="now",
    )
    assert candidate.kind is MemoryKind.KNOWLEDGE
    with pytest.raises(ValueError, match="knowledge"):
        MemoryCandidate(
            id="c2",
            text="项目使用 asyncio",
            trigger=MemoryCandidateTrigger.TASK_COMPLETION,
            confidence=0.65,
            digest="digest",
            project_id="project",
            created_at="now",
            kind=MemoryKind.FACT,
        )


@pytest.mark.parametrize(
    ("mode", "should_queue", "should_write"),
    [
        ("off", False, False),
        ("suggest", True, False),
        ("auto", True, True),
    ],
)
def test_auto_memory_modes(mode, should_queue, should_write, tmp_path):
    coordinator, store, _loop = make_coordinator(tmp_path, mode=mode)

    report = coordinator.process_turn(
        "请记住：项目版本文件是 src/krelcode/__init__.py",
        (Message.assistant("好"),),
        turn_id=f"mode-{mode}",
    )

    assert bool(report.extracted) is should_queue
    assert bool(store.list()) is should_write


def test_candidate_and_evidence_are_redacted_before_disk_write(tmp_path):
    coordinator, store, _loop = make_coordinator(tmp_path, mode="suggest")
    secret = "sk-abcdefgh12345678"
    cookie = "session=abcdef123456; other=should-not-leak"

    report = coordinator.process_turn(
        f"请记住：测试命令是 pytest -q，api_key={secret} "
        f"Cookie: {cookie} Authorization: Bearer abcdefghijkl",
        (Message.assistant("好"),),
        turn_id="secret-1",
    )

    assert report.extracted
    raw_queue = coordinator.queue.path.read_text(encoding="utf-8")
    assert secret not in raw_queue
    assert cookie not in raw_queue
    assert "should-not-leak" not in raw_queue
    assert "abcdefghijkl" not in raw_queue
    assert "***" in raw_queue
    assert "pytest -q" in raw_queue
    assert all(secret not in item.text for item in store.list())


def test_memory_tags_and_nested_metadata_are_redacted(tmp_path):
    store = make_store(tmp_path)
    secret = "sk-abcdefgh12345678"
    entry = store.add(
        "项目部署说明",
        tags=(secret,),
        metadata={
            "authorization": "Bearer abcdefghijkl",
            "nested": {"cookie": "session=should-not-leak"},
        },
    )

    raw = store.path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "abcdefghijkl" not in raw
    assert "should-not-leak" not in raw
    assert entry.tags == ("***",)
    assert entry.metadata["authorization"] == "***"
    assert entry.metadata["nested"]["cookie"] == "***"


def test_auto_candidates_do_not_cross_workspace_boundaries(tmp_path):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    coordinator_a, store_a, _ = make_coordinator(project_a, mode="auto")
    coordinator_b, store_b, _ = make_coordinator(project_b, mode="auto")

    coordinator_a.process_turn(
        "请记住：项目 A 使用 asyncio",
        (Message.assistant("好"),),
        turn_id="a-1",
    )

    assert len(store_a.list()) == 1
    assert store_b.list() == ()
    assert coordinator_b.queue.list(limit=0) == ()
    assert coordinator_a.queue.project_id != coordinator_b.queue.project_id


def test_memory_review_accept_reject_pin_and_search_commands(tmp_path):
    coordinator, store, loop = make_coordinator(tmp_path, mode="suggest")
    first = coordinator.process_turn(
        "请记住：项目测试命令是 pytest -q", turn_id="review-1")
    second = coordinator.process_turn(
        "请记住：项目使用 asyncio 事件循环", turn_id="review-2")
    controller = SessionController(
        loop, command_registry=builtin_command_registry(),
        workspace_root=tmp_path)

    review = asyncio.run(controller.execute_command("/memory review"))
    rendered = "\n".join(review.output)
    assert first.extracted[0].id in rendered
    assert second.extracted[0].id in rendered

    accepted = asyncio.run(controller.execute_command(
        f"/memory review accept {first.extracted[0].id}"))
    assert "已接受" in accepted.output[0]
    rejected = asyncio.run(controller.execute_command(
        f"/memory review reject {second.extracted[0].id}"))
    assert "已拒绝" in rejected.output[0]
    assert coordinator.queue.get(second.extracted[0].id).status is MemoryCandidateStatus.REJECTED

    entry = store.list()[0]
    pinned = asyncio.run(controller.execute_command(f"/memory pin {entry.id}"))
    assert "固定" in pinned.output[0]
    assert store.list()[0].pinned is True
    searched = asyncio.run(controller.execute_command("/memory search pytest"))
    assert entry.id in "\n".join(searched.output)
    unpinned = asyncio.run(controller.execute_command(f"/memory unpin {entry.id}"))
    assert "取消固定" in unpinned.output[0]
    assert store.list()[0].pinned is False


def test_memory_mode_command_controls_future_extraction(tmp_path):
    coordinator, _store, loop = make_coordinator(tmp_path, mode="suggest")
    controller = SessionController(loop, workspace_root=tmp_path)

    switched = asyncio.run(controller.execute_command("/memory mode off"))
    assert "off" in switched.output[0]
    report = coordinator.process_turn("请记住：项目使用 asyncio", turn_id="off-1")
    assert report.extracted == ()

    asyncio.run(controller.execute_command("/memory mode suggest"))
    report = coordinator.process_turn("请记住：项目使用 asyncio", turn_id="suggest-1")
    assert report.extracted


def test_candidate_queue_persists_evidence_and_reloads(tmp_path):
    queue = MemoryCandidateQueue(
        tmp_path, path=tmp_path / ".krelcode" / "memory" / "candidates.jsonl")
    draft = MemoryCandidateDraft(
        text="项目使用 asyncio",
        trigger=MemoryCandidateTrigger.TASK_COMPLETION,
        confidence=0.65,
        topic="architecture",
        evidence=(MemoryEvidence(
            kind="task_completion", turn_id="t1", excerpt="项目使用 asyncio"),),
    )

    created, _ = queue.merge(draft)
    assert created is not None
    reloaded = MemoryCandidateQueue(
        tmp_path, path=queue.path)
    loaded = reloaded.get(created.id)

    assert loaded is not None
    assert loaded.text == "项目使用 asyncio"
    assert loaded.evidence[0].turn_id == "t1"


def test_conflicting_test_commands_block_auto_write(tmp_path):
    coordinator, store, _loop = make_coordinator(tmp_path, mode="auto")
    coordinator.process_turn(
        "请记住：项目测试命令是 pytest -q", turn_id="conflict-1")
    assert len(store.list()) == 1

    report = coordinator.process_turn(
        "请记住：项目测试命令是 cargo test", turn_id="conflict-2")

    candidate = report.extracted[0]
    assert candidate.status is MemoryCandidateStatus.PENDING
    assert candidate.conflict_ids
    assert len(store.list()) == 1
    assert memory_texts_conflict(candidate.text, store.list()[0].text)
    assert memory_texts_conflict("项目使用 pytest", "项目不使用 pytest")


def test_pinned_entries_rank_first_in_search_and_context(tmp_path):
    store = make_store(tmp_path)
    older = store.add("deployment command is make deploy")
    store.add("deployment notes are recent")
    pinned = store.pin(older.id)
    assert pinned is not None and pinned.pinned
    for index in range(MAX_ENTRIES + 2):
        store.add(f"unrelated note {index}")

    results = store.search("deployment", limit=2)
    assert results[0].id == older.id
    assert any(item.id == older.id for item in store.list(limit=0))
    context = store.render_context(max_entries=1, max_chars=500)
    assert "make deploy" in context
    assert "pinned: true" in context


def test_memory_search_tool_is_read_only_bounded_and_marks_context(tmp_path):
    store = make_store(tmp_path)
    entry = store.add(
        "项目测试命令是 pytest -q",
        kind=MemoryKind.KNOWLEDGE,
        source="auto-memory:review",
    )
    store.pin(entry.id)
    tool = MemorySearchTool(store)

    result = asyncio.run(tool.execute(
        {"query": "pytest", "scope": "project", "limit": 999},
        ToolContext(workspace_root=tmp_path),
    ))

    assert result.is_error is False
    assert tool.read_only is True
    assert "[Evidence: Memory]" in result.content
    assert "[Evidence: Memory]" in result.content
    assert "kind: knowledge" in result.content
    assert "pinned: true" in result.content
    assert result.data["count"] == 1
    assert result.data["ids"] == [entry.id]


def test_memory_fragment_explicitly_marks_knowledge_as_not_instructions(tmp_path):
    store = make_store(tmp_path)
    store.add(
        "项目测试命令是 pytest -q",
        kind=MemoryKind.KNOWLEDGE,
        source="auto-memory:review",
    )

    fragment = store.fragment()

    assert fragment is not None and fragment.id == FRAGMENT_ID
    assert fragment.content.startswith("[Evidence: Memory]\n")
    assert "requires_validation: true" in fragment.content
    assert fragment.metadata["source_role"] == "evidence"
    assert fragment.metadata["instruction"] is False


def test_task_completion_extracts_only_high_value_stable_fact(tmp_path):
    extractor = MemoryCandidateExtractor()
    turn = MemoryTurnSnapshot(
        user_input="请完成项目初始化",
        messages=(Message.assistant(
            "初始化完成。项目使用 asyncio 实现执行推进层。接下来可以运行测试。"),),
        turn_id="done-1",
    )

    drafts = extractor.extract(turn)

    assert any(
        item.trigger is MemoryCandidateTrigger.TASK_COMPLETION
        and item.text == "项目使用 asyncio 实现执行推进层。"
        for item in drafts
    )


def test_run_finished_event_extracts_from_current_turn_only(tmp_path):
    coordinator, store, loop = make_coordinator(tmp_path, mode="suggest")
    bus = EventBus()
    coordinator.attach(bus)
    loop.history = [
        Message.user("旧任务"),
        Message.assistant("旧回复"),
    ]

    async def scenario():
        loop.history.append(Message.user("请记住：项目使用 asyncio"))
        await bus.emit(RunStarted(
            "请记住：项目使用 asyncio", turn_id="event-1", session_id="s1"))
        loop.history.append(Message.assistant("已记录"))
        await bus.emit(RunFinished("已记录", turn_id="event-1"))

    asyncio.run(scenario())

    pending = coordinator.queue.list(limit=0)
    assert len(pending) == 1
    assert pending[0].evidence[0].turn_id == "event-1"
    assert store.list() == ()


def test_memory_mode_resolution_defaults_to_suggest_and_config_can_override():
    from krelcode.app import resolve_auto_memory_mode

    assert resolve_auto_memory_mode(Namespace(), {}) is AutoMemoryMode.SUGGEST
    assert resolve_auto_memory_mode(
        Namespace(memory_mode="off"), {"memory": {"mode": "auto"}}
    ) is AutoMemoryMode.OFF
    assert resolve_auto_memory_mode(
        Namespace(memory_mode=None), {"memory": {"mode": "auto"}}
    ) is AutoMemoryMode.AUTO


def test_default_toolset_registers_memory_search_as_read_only(tmp_path):
    from krelcode.app import default_tools

    store = make_store(tmp_path)
    registry = ToolRegistry(default_tools(store))

    assert "memory_search" in registry.names()
    descriptor = registry.descriptor("memory_search")
    assert descriptor is not None and descriptor.read_only is True
