"""Step 92: resume, fork, rewind and subagent context integration."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from krelcode.agent.compact_summary import CompactSummary
from krelcode.agent.context import ContextOptions
from krelcode.agent.context_plan import ContextPlanSnapshot, history_digest
from krelcode.agent.fragments import PromptFragmentRegistry, base_fragment
from krelcode.agent.loop import AgentLoop
from krelcode.agent.rules import RuleLoader
from krelcode.agent.runtime import AgentRuntime
from krelcode.agent.working_context import WorkingContextStore
from krelcode.core.events import EventBus, RunFinished, RunStarted
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.compact_checkpoint import CompactCheckpointStore
from krelcode.session.context_checkpoint import (
    ContextCheckpointCoordinator,
    ContextCheckpointStore,
    hash_tracked_files,
    restore_context_checkpoint,
    workspace_fingerprint,
)
from krelcode.session.controller import SessionController
from krelcode.session.lifecycle import SessionLifecycle
from krelcode.session.memory import MemoryKind, MemoryScope, MemoryStore
from krelcode.session.recorder import SessionRecorder
from krelcode.session.restore import restore_session
from krelcode.subagent.instructions import build_subagent_instructions
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(fallback="mock"),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        system_prompt="CURRENT-SYSTEM",
    )


def git(tmp_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(tmp_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def summary_json() -> str:
    return json.dumps({
        "version": 2,
        "goal": "Resume the context safely",
        "decisions": ["reload live instructions"],
        "files": [{"path": "app.py", "status": "modified", "note": "live"}],
        "commands": ["pytest -q"],
        "tests": [{"name": "step92", "status": "passed", "detail": "ok"}],
        "errors": [],
        "open_issues": [],
        "next_steps": ["continue"],
        "handoff_notes": ["checkpoint restored"],
    })


def working_record(store: WorkingContextStore) -> dict:
    snapshot = store.snapshot()
    return {
        **snapshot.as_record(),
        "entries": [
            {
                "id": entry.id,
                "kind": entry.kind,
                "content": entry.content,
                "source": entry.source,
                "status": entry.status,
                "metadata": dict(entry.metadata),
                "sequence": entry.sequence,
                "timestamp": entry.timestamp,
            }
            for entry in snapshot.entries
        ],
    }


def test_resume_reloads_live_git_tree_and_rules(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Krelcode Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(tmp_path, "add", "seed.txt")
    git(tmp_path, "commit", "-q", "-m", "seed")
    git(tmp_path, "branch", "-m", "old-branch")
    (tmp_path / "AGENTS.md").write_text("PROJECT RULE OLD\n", encoding="utf-8")
    old_workspace = workspace_fingerprint(
        tmp_path, rules_content="PROJECT RULE OLD")
    assert old_workspace.branch == "old-branch"

    old_history = [
        Message.system("OLD SYSTEM old-branch PROJECT RULE OLD"),
        Message.user("inspect app.py"),
        Message.assistant("old answer"),
    ]
    sessions = tmp_path / ".krelcode" / "sessions"
    session_path = sessions / "source-session.jsonl"
    SessionRecorder(session_path, lambda: old_history).flush_history()

    store = ContextCheckpointStore(tmp_path)
    checkpoint = store.record(
        history=old_history,
        session_id="source-session",
        turn_id="turn-old",
        workspace=old_workspace,
    )
    assert checkpoint is not None

    git(tmp_path, "branch", "-m", "fresh-branch")
    (tmp_path / "AGENTS.md").write_text("PROJECT RULE FRESH\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('fresh')\n", encoding="utf-8")

    loop = make_loop(tmp_path)
    loop.context_checkpoint_store = store
    loop.context_options = ContextOptions()
    loop.rule_loader = RuleLoader(tmp_path, home=tmp_path / "home")
    loop.prompt_registry = PromptFragmentRegistry([base_fragment()])

    assert restore_session(loop, session_path, log=lambda _: None)
    system = loop.history[0].content or ""
    assert "fresh-branch" in system
    assert "old-branch" not in system
    assert "PROJECT RULE FRESH" in system
    assert "PROJECT RULE OLD" not in system
    assert "app.py" in system
    assert loop.pending_context_refresh is False
    assert loop.restored_workspace_fingerprint == old_workspace
    assert loop.context_workspace_fingerprint is None


def test_resume_restores_working_context_and_invalidates_changed_file(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old = True\n", encoding="utf-8")
    tracked = hash_tracked_files((target,), tmp_path)
    history = [
        Message.system("CURRENT-SYSTEM"),
        Message.user("finish app.py"),
        Message.assistant("working"),
    ]

    working = WorkingContextStore(tmp_path)
    working.upsert("goal", "finish app.py", id="goal:current", source="user")
    working.upsert(
        "file", "app.py", id="file:app.py", source="tool:read_file",
        metadata={"path": "app.py"})
    working.upsert(
        "decision", "keep the public API", id="decision:api", source="agent")
    record = working_record(working)
    store = ContextCheckpointStore(tmp_path)
    checkpoint = store.record(
        history=history,
        session_id="s",
        turn_id="t1",
        working_context=record,
        file_hashes=tracked,
        context_plan=ContextPlanSnapshot(
            turn_id="t1",
            history_digest=history_digest(history),
            working_context_sequence=int(record["sequence"]),
        ),
    )
    assert checkpoint is not None
    target.write_text("old = False\n", encoding="utf-8")

    loop = make_loop(tmp_path)
    loop.context_checkpoint_store = store
    result = restore_context_checkpoint(loop, checkpoint)

    assert result.integrity.changed == ("app.py",)
    snapshot = loop.working_context_store.snapshot()
    ids = {entry.id for entry in snapshot.entries}
    assert "goal:current" in ids
    assert "decision:api" in ids
    assert "file:app.py" not in ids
    assert "open_issue:resume-file-integrity" in ids
    assert result.context_plan.matches_history(loop.history)
    assert result.context_plan.working_context_sequence == snapshot.sequence


def test_restore_loads_compact_summary_and_plan(tmp_path):
    pre_compact = [
        Message.system("SYS"),
        Message.user("old goal"),
        Message.assistant("old result"),
    ]
    summary = CompactSummary.from_model_json_text(summary_json())
    compact_store = CompactCheckpointStore(tmp_path)
    compact = compact_store.record(
        pre_compact_history=pre_compact,
        summary=summary,
        trigger="manual",
        before_tokens=100,
        after_tokens=20,
        session_id="s",
        turn_id="t1",
    )
    assert compact is not None

    compacted_history = [
        Message.system("SYS"),
        Message.user(summary.render()),
    ]
    store = ContextCheckpointStore(tmp_path)
    checkpoint = store.record(
        history=compacted_history,
        session_id="s",
        turn_id="t1",
        compact_checkpoint_id=compact.id,
        compact_summary=summary.as_dict(),
    )
    assert checkpoint is not None

    loop = make_loop(tmp_path)
    loop.context_checkpoint_store = store
    result = restore_context_checkpoint(
        loop, checkpoint, compact_store=compact_store)

    assert result.compact_checkpoint_id == compact.id
    assert loop.last_compact_checkpoint is not None
    assert loop.last_compact_checkpoint.id == compact.id
    assert result.context_plan.matches_history(loop.history)


def test_restore_retrieves_memory_with_attributed_context_source(tmp_path):
    memory = MemoryStore(
        tmp_path,
        path=tmp_path / ".krelcode" / "memory" / "entries.jsonl",
        user_path=tmp_path / "user-memory.jsonl",
        session_id="s",
    )
    entry = memory.add(
        "login tests use pytest and the auth fixture",
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.KNOWLEDGE,
    )
    history = [
        Message.system("SYS"),
        Message.user("fix login"),
        Message.assistant("need tests"),
    ]
    store = ContextCheckpointStore(tmp_path)
    checkpoint = store.record(
        history=history,
        session_id="s",
        turn_id="t1",
        memory_query="login pytest auth fixture",
    )
    assert checkpoint is not None
    assert "login tests use pytest" not in json.dumps(
        checkpoint.metadata_record(), ensure_ascii=False)

    provider = MockProvider(fallback="done")
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        system_prompt="CURRENT-SYSTEM",
    )
    loop.context_checkpoint_store = store
    loop.memory_store = memory
    result = restore_context_checkpoint(
        loop, checkpoint, memory_store=memory)

    assert entry.id in result.memory_ids
    assert entry.id in loop.restored_relevant_memory_ids
    assert "login tests use pytest" in "\n".join(loop.restored_memory_context)
    asyncio.run(loop.run("continue login task"))
    rendered_system = provider.calls[0][0].content or ""
    assert "<restored_memory" in rendered_system
    assert "login tests use pytest" in rendered_system
    assert "memory_retrieval" in loop.last_context_plan.sources


def test_fork_copies_context_checkpoint_and_session_memory(tmp_path):
    sessions = tmp_path / ".krelcode" / "sessions"
    source_path = sessions / "source.jsonl"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("", encoding="utf-8")
    memory = MemoryStore(
        tmp_path,
        path=tmp_path / ".krelcode" / "memory" / "entries.jsonl",
        user_path=tmp_path / "user-memory.jsonl",
        session_id="source",
    )
    memory.add("source-only fact", scope=MemoryScope.SESSION)
    history = [
        Message.system("SYS"),
        Message.user("source task"),
        Message.assistant("source answer"),
    ]
    store = ContextCheckpointStore(tmp_path)
    checkpoint = store.record(history=history, session_id="source", turn_id="t1")
    assert checkpoint is not None

    loop = make_loop(tmp_path)
    loop.context_checkpoint_store = store
    loop.memory_store = memory
    lifecycle = SessionLifecycle(loop, sessions, source_path)
    fork_path = lifecycle.fork(
        "branch", history=history, context_checkpoint_id=checkpoint.id)
    target_id = fork_path.stem

    copied = store.latest_for_session(target_id)
    assert copied is not None
    assert copied.copied_from == checkpoint.id
    assert copied.session_id == target_id
    assert store.latest_for_session("source").id == checkpoint.id
    assert memory.session_id == target_id

    memory.add("branch-only fact", scope=MemoryScope.SESSION)
    source_memory = MemoryStore(
        tmp_path,
        path=tmp_path / ".krelcode" / "memory" / "entries.jsonl",
        user_path=tmp_path / "user-memory.jsonl",
        session_id="source",
    )
    source_text = "\n".join(
        entry.text for entry in source_memory.list(limit=0, scopes=(MemoryScope.SESSION,)))
    fork_text = "\n".join(
        entry.text for entry in memory.list(limit=0, scopes=(MemoryScope.SESSION,)))
    assert "source-only fact" in source_text
    assert "branch-only fact" not in source_text
    assert "source-only fact" in fork_text
    assert "branch-only fact" in fork_text


def test_rewind_restores_context_plan_matching_history(tmp_path):
    async def scenario():
        loop = make_loop(tmp_path)
        store = ContextCheckpointStore(tmp_path)
        loop.context_checkpoint_store = store
        coordinator = ContextCheckpointCoordinator(loop=loop, store=store)
        coordinator.attach(loop.bus)
        controller = SessionController(AgentRuntime(loop), checkpoints=None)
        loop.history = [Message.system("SYS")]

        loop.history.append(Message.user("first"))
        await loop.bus.emit(RunStarted("first", turn_id="t1", session_id="s"))
        loop.history.append(Message.assistant("first done"))
        await loop.bus.emit(RunFinished("first done", turn_id="t1"))

        before_second = list(loop.history)
        loop.history.append(Message.user("second"))
        await loop.bus.emit(RunStarted("second", turn_id="t2", session_id="s"))
        loop.history.append(Message.assistant("second done"))
        await loop.bus.emit(RunFinished("second done", turn_id="t2"))

        result = controller.rewind.rewind(2)
        assert [message.content for message in loop.history] == [
            message.content for message in before_second]
        assert result.context_checkpoint_id
        assert loop.last_context_plan is not None
        assert loop.last_context_plan.restored_from == result.context_checkpoint_id
        assert loop.last_context_plan.matches_history(loop.history)
        assert (
            loop.last_context_plan.working_context_sequence
            == loop.working_context_store.snapshot().sequence
        )
        coordinator.detach()

    asyncio.run(scenario())


def test_subagent_instruction_layers_and_parent_history_are_separate(tmp_path):
    home = tmp_path / "home"
    (home / ".krelcode").mkdir(parents=True)
    (home / ".krelcode" / "AGENTS.md").write_text(
        "USER RULE\n", encoding="utf-8")
    (tmp_path / ".krelcode" / "managed").mkdir(parents=True)
    (tmp_path / ".krelcode" / "managed" / "AGENTS.md").write_text(
        "MANAGED RULE\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("PROJECT RULE\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AGENTS.md").write_text(
        "DIRECTORY RULE\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    bundle = build_subagent_instructions(
        tmp_path,
        "explain src/app.py",
        base_prompt="BASE SUBAGENT",
        home=home,
    )
    kinds = {layer.kind for layer in bundle.layers}
    rendered = bundle.text
    assert {"base", "managed-project", "user", "project", "directory"} <= kinds
    assert "BASE SUBAGENT" in rendered
    assert "MANAGED RULE" in rendered
    assert "USER RULE" in rendered
    assert "PROJECT RULE" in rendered
    assert "DIRECTORY RULE" in rendered
