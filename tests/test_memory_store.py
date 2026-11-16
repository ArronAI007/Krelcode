"""Project memory bounds, redaction and prompt-fragment integration."""

from __future__ import annotations

from krelcode.agent.fragments import PromptFragmentRegistry, base_fragment
from krelcode.agent.loop import AgentLoop
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.memory import (
    FRAGMENT_ID,
    INJECT_ENTRIES,
    MemoryStore,
    memory_lines,
    sync_memory_fragment,
)
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def test_memory_store_persists_redacted_entries(tmp_path):
    redactor = SecretRedactor(["configured-secret-value"])
    store = MemoryStore(tmp_path, redactor=redactor)

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


def test_memory_store_rejects_empty_and_oversized_values(tmp_path):
    store = MemoryStore(tmp_path)
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
    store = MemoryStore(tmp_path)
    for index in range(INJECT_ENTRIES + 4):
        store.add(f"entry-{index}")

    context = store.render_context(max_entries=3, max_chars=500)

    assert "entry-15" in context and "entry-14" in context
    assert "entry-13" in context and "entry-12" not in context
    assert len(context) <= 500
    assert store.token_count() > 0


def test_sync_memory_fragment_updates_system_prompt(tmp_path):
    store = MemoryStore(tmp_path)
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
    assert not next(item for item in snapshot.fragments if item.id == FRAGMENT_ID).cacheable


def test_memory_lines_show_most_recent_first(tmp_path):
    store = MemoryStore(tmp_path)
    store.add("first")
    store.add("second")
    rendered = memory_lines(store.list())
    assert rendered[1].endswith("second")
    assert rendered[2].endswith("first")
