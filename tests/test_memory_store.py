"""Project memory bounds, redaction and prompt-fragment integration."""

from __future__ import annotations

import json
from krelcode.agent.fragments import PromptFragmentRegistry, base_fragment
from krelcode.agent.loop import AgentLoop
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.memory import (
    FRAGMENT_ID,
    INJECT_ENTRIES,
    MemoryKind,
    MemoryScope,
    MemoryStore,
    coerce_memory_scope,
    memory_lines,
    parse_memory_note,
    sync_memory_fragment,
)
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def make_store(tmp_path, **kwargs) -> MemoryStore:
    kwargs.setdefault(
        "user_path", tmp_path / "global-memory" / "entries.jsonl")
    return MemoryStore(tmp_path, **kwargs)


def test_memory_store_persists_redacted_entries(tmp_path):
    redactor = SecretRedactor(["configured-secret-value"])
    store = make_store(tmp_path, redactor=redactor)

    store.add("token=configured-secret-value keep this note")
    store.add("api_key=sk-abcdefgh12345678")

    entries = store.list()
    assert len(entries) == 2
    assert "configured-secret-value" not in entries[0].text
    assert "sk-abcdefgh" not in entries[1].text
    raw = store.path.read_text(encoding="utf-8")
    assert "configured-secret-value" not in raw
    assert "sk-abcdefgh12345678" not in raw
    assert entries[0].id and entries[0].created_at
    assert entries[0].scope is MemoryScope.PROJECT
    assert entries[0].kind is MemoryKind.NOTE
    payload = json.loads(raw.splitlines()[0])
    assert payload["scope"] == "project"
    assert payload["kind"] == "note"


def test_memory_store_rejects_empty_and_oversized_values(tmp_path):
    store = make_store(tmp_path)
    for value in ("", "   "):
        try:
            store.add(value)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("empty memory should fail")
    try:
        store.add("x" * 2_001)
    except ValueError as exc:
        assert "不能超过" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("oversized memory should fail")


def test_memory_context_is_bounded_and_recent_first(tmp_path):
    store = make_store(tmp_path)
    for index in range(INJECT_ENTRIES + 4):
        store.add(f"entry-{index}")

    context = store.render_context(max_entries=3, max_chars=2_000)

    assert "entry-15" in context and "entry-14" in context
    assert "entry-13" in context and "entry-12" not in context
    assert len(context) <= 2_000
    assert store.token_count() > 0


def test_sync_memory_fragment_updates_system_prompt(tmp_path):
    store = make_store(tmp_path)
    store.add("项目测试命令是 pytest -q")
    loop = AgentLoop(
        llm=MockProvider(fallback="ok"),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="BASE",
    )
    loop.prompt_registry = PromptFragmentRegistry([base_fragment()])
    loop.history = [Message.system("BASE")]

    fragment = sync_memory_fragment(loop, store)

    assert fragment is not None and fragment.id == FRAGMENT_ID
    assert "pytest -q" in loop.history[0].content
    snapshot = loop.prompt_registry.snapshot()
    assert any(item.id == FRAGMENT_ID for item in snapshot.fragments)
    fragment_info = next(
        item for item in snapshot.fragments if item.id == FRAGMENT_ID)
    assert not fragment_info.cacheable
    assert fragment_info.metadata["scopes"] == {
        "user": 0, "project": 1, "session": 0}


def test_memory_lines_show_most_recent_first(tmp_path):
    store = make_store(tmp_path)
    store.add("first")
    store.add("second")
    rendered = memory_lines(store.list())
    assert rendered[1].endswith("second")
    assert rendered[2].endswith("first")


def test_memory_scopes_use_separate_files_and_structured_records(tmp_path):
    store = make_store(tmp_path, session_id="session-86")

    user = store.add(
        "默认使用中文回答", scope=MemoryScope.USER,
        kind=MemoryKind.PREFERENCE, tags=("style", "language"))
    project = store.add("测试命令是 pytest -q", scope="project")
    session = store.add("本轮正在重构 memory", scope="session")

    assert user.scope is MemoryScope.USER
    assert user.kind is MemoryKind.PREFERENCE
    assert user.tags == ("style", "language")
    assert len({store.scope_path(user.scope), store.scope_path(project.scope),
                store.scope_path(session.scope)}) == 3
    assert store.user_path.is_file()
    assert store.project_path.is_file()
    assert store.session_path.is_file()
    assert {entry.scope for entry in store.list()} == {
        MemoryScope.USER, MemoryScope.PROJECT, MemoryScope.SESSION}
    record = json.loads(store.user_path.read_text(encoding="utf-8").strip())
    assert record["id"] == user.id
    assert record["updated_at"] == user.created_at
    assert record["source"] == "manual"
    assert record["tags"] == ["style", "language"]


def test_legacy_project_entries_migrate_on_read(tmp_path):
    store = make_store(tmp_path)
    store.project_path.parent.mkdir(parents=True)
    store.project_path.write_text(json.dumps({
        "id": "legacy-1",
        "text": "旧格式记忆",
        "created_at": "2026-01-01T00:00:00+08:00",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    entry = store.list()[0]

    assert entry.scope is MemoryScope.PROJECT
    assert entry.kind is MemoryKind.NOTE
    assert entry.source == "manual"
    assert entry.updated_at == entry.created_at
    assert entry.tags == ()


def test_scoped_context_prioritizes_specific_scopes_and_keeps_order(tmp_path):
    store = make_store(tmp_path)
    store.add("user-rule", scope="user")
    store.add("project-rule", scope="project")
    store.add("session-rule", scope="session")

    context = store.render_context(max_entries=3, max_chars=1_000)

    user_at = context.index("user-rule")
    project_at = context.index("project-rule")
    session_at = context.index("session-rule")
    assert user_at < project_at < session_at
    assert context.count("[Evidence: Memory]") == 3
    assert "scope: user" in context
    assert "scope: project" in context
    assert "scope: session" in context
    assert "requires_validation: true" in context
    assert "instruction: false" in context


def test_session_scope_flushes_to_bound_session_file(tmp_path):
    store = make_store(tmp_path)
    entry = store.add("session-only", scope="session")

    assert store.session_path is None
    assert store.list(scopes=("session",)) == (entry,)

    store.bind_session("../session safe/id")

    assert store.session_id == ".._session_safe_id"
    assert store.session_path.is_file()
    reloaded = make_store(tmp_path, session_id=store.session_id)
    assert reloaded.list(scopes=("session",))[0].text == "session-only"


def test_remove_only_rewrites_matching_scope(tmp_path):
    store = make_store(tmp_path)
    user = store.add("global", scope="user")
    project = store.add("local", scope="project")

    removed = store.remove(user.id)

    assert removed is not None and removed.id == user.id
    assert store.list(scopes=("user",)) == ()
    assert [entry.id for entry in store.list(scopes=("project",))] == [project.id]


def test_parse_memory_note_supports_explicit_scope_prefixes():
    assert parse_memory_note("普通项目记忆") == (
        MemoryScope.PROJECT, "普通项目记忆")
    assert parse_memory_note("user: 偏好中文") == (
        MemoryScope.USER, "偏好中文")
    assert parse_memory_note("session: 当前任务") == (
        MemoryScope.SESSION, "当前任务")
    assert coerce_memory_scope("global") is MemoryScope.USER
    assert coerce_memory_scope("workspace") is MemoryScope.PROJECT


def test_user_scope_is_cross_workspace_while_project_scope_is_isolated(tmp_path):
    shared_user_path = tmp_path / "home" / ".krelcode" / "memory" / "entries.jsonl"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    store_a = MemoryStore(project_a, user_path=shared_user_path)
    store_b = MemoryStore(project_b, user_path=shared_user_path)

    store_a.add("user-wide", scope="user")
    store_a.add("project-a-only", scope="project")

    assert [entry.text for entry in store_b.list(scopes=("user",))] == [
        "user-wide"]
    assert store_b.list(scopes=("project",)) == ()
    assert [entry.text for entry in store_a.list(scopes=("project",))] == [
        "project-a-only"]
