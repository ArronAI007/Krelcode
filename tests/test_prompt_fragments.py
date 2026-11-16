"""Prompt Fragment Registry、prompt cache 边界与 compact preview 回归测试。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from krelcode.agent.compression import ContextManager, SUMMARY_PREFIX
from krelcode.agent.context import ContextSection, WorkspaceContext
from krelcode.agent.fragments import (
    GROUP_RUNTIME,
    GROUP_SYSTEM,
    GROUP_WORKSPACE,
    PromptFragment,
    PromptFragmentRegistry,
    apply_prompt_snapshot,
    base_fragment,
    workspace_fragments,
)
from krelcode.agent.loop import AgentLoop
from krelcode.agent.tokens import estimate_history_tokens
from krelcode.app import build_loop, build_parser
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import (
    handle_compact,
    handle_context,
)
from krelcode.session.commands.types import CommandRequest
from krelcode.skills import SkillManager
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def make_workspace_bundle(
    tmp_path: Path,
    *,
    rules: str = "RULES: keep changes focused",
    git: str = "git branch: main",
) -> WorkspaceContext:
    sections = (
        ContextSection("rules", rules, priority=10, source="AGENTS.md"),
        ContextSection("git", git, priority=20, source="git"),
        ContextSection(
            "tree", "工作区目录树:\nproj.py", priority=50,
            source="filesystem"),
    )
    body = "\n\n".join(section.content for section in sections)
    return WorkspaceContext(
        root=tmp_path,
        sections=sections,
        text=f"<workspace_context>\n{body}\n</workspace_context>",
        max_tokens=1_000,
        total_tokens=40,
    )


def make_loop(tmp_path: Path, system_prompt: str = "") -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt=system_prompt,
    )


def command_context(text: str, loop: AgentLoop, tmp_path: Path, skills=None) -> CommandContext:
    token, _, arguments = text.partition(" ")
    return CommandContext(
        request=CommandRequest(
            name=token.lstrip("/"), arguments=arguments, raw_text=text),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=tmp_path,
        skills=skills,
    )


def run_context(text: str, loop: AgentLoop, tmp_path: Path, skills=None):
    ctx = command_context(text, loop, tmp_path, skills)
    return asyncio.run(handle_context(ctx))


def run_compact(text: str, loop: AgentLoop, tmp_path: Path):
    ctx = command_context(text, loop, tmp_path)
    return asyncio.run(handle_compact(ctx))


def test_registry_renders_global_cacheable_prefix_before_dynamic_fragments(tmp_path):
    bundle = make_workspace_bundle(tmp_path)
    registry = PromptFragmentRegistry([
        PromptFragment(
            id="runtime:hint", source="runtime", title="Runtime hint",
            content="RUNTIME", group=GROUP_RUNTIME),
        *workspace_fragments(bundle),
        PromptFragment(
            id="dynamic:system", source="dynamic", title="Dynamic system",
            content="DYNAMIC-SYSTEM", group=GROUP_SYSTEM, priority=-100),
        base_fragment(),
    ])

    snapshot = registry.snapshot()

    assert [fragment.id for fragment in registry.list()] == [
        "core:base",
        "workspace:rules",
        "dynamic:system",
        "workspace:git",
        "workspace:tree",
        "runtime:hint",
    ]
    assert snapshot.cache.stable_fragments == ("core:base", "workspace:rules")
    assert "RULES: keep changes focused" in snapshot.stable_prefix
    assert "DYNAMIC-SYSTEM" not in snapshot.stable_prefix
    assert "git branch: main" not in snapshot.stable_prefix
    assert snapshot.cache.offset == len(snapshot.stable_prefix)
    assert snapshot.provider_meta() == {
        "prompt_cache_boundaries": [snapshot.cache.offset]}


def test_fragment_source_and_id_overrides_reset_and_locked_fragment(tmp_path):
    bundle = make_workspace_bundle(tmp_path)
    registry = PromptFragmentRegistry([base_fragment(), *workspace_fragments(bundle)])

    kind, count = registry.set_enabled("workspace:git", False)
    assert (kind, count) == ("fragment", 1)
    assert not registry.is_enabled("workspace:git")
    registry.set_enabled("workspace:git", True)
    assert registry.is_enabled("workspace:git")

    with pytest.raises(ValueError, match="不可手动启停"):
        registry.set_enabled("core:base", False)
    with pytest.raises(ValueError, match="不可手动启停"):
        registry.set_enabled("core", False)

    registry.set_enabled("workspace:git", False)
    registry.reset_overrides()
    assert registry.is_enabled("workspace:git")


def test_source_override_and_exact_id_override_precedence(tmp_path):
    registry = PromptFragmentRegistry([
        PromptFragment(
            id="alpha:a", source="alpha", title="A", content="A",
            cacheable=True),
        PromptFragment(
            id="alpha:b", source="alpha", title="B", content="B"),
    ])

    assert registry.set_enabled("alpha", False) == ("source", 2)
    assert not registry.is_enabled("alpha:a")
    assert not registry.is_enabled("alpha:b")

    registry.set_enabled("alpha:a", True)
    assert registry.is_enabled("alpha:a")       # 精确 id 覆盖 source
    assert not registry.is_enabled("alpha:b")

    kind, count, enabled = registry.toggle("alpha:b")
    assert (kind, count, enabled) == ("fragment", 1, True)
    assert registry.is_enabled("alpha:b")


def test_replace_group_is_atomic_on_conflict(tmp_path):
    bundle = make_workspace_bundle(tmp_path)
    registry = PromptFragmentRegistry([base_fragment(), *workspace_fragments(bundle)])
    before = registry.list()

    conflicting = PromptFragment(
        id="core:base", source="conflict", title="Conflict", content="X",
        group=GROUP_WORKSPACE)
    with pytest.raises(ValueError, match="已存在"):
        registry.replace_group(GROUP_WORKSPACE, [conflicting])

    assert registry.list() == before


def test_locked_fragment_cannot_become_toggleable(tmp_path):
    registry = PromptFragmentRegistry([base_fragment()])

    with pytest.raises(ValueError, match="toggleable"):
        registry.upsert(replace(base_fragment(), toggleable=True))


def test_cache_hash_tracks_stable_rules_but_not_dynamic_git(tmp_path):
    first = PromptFragmentRegistry(
        [base_fragment(), *workspace_fragments(make_workspace_bundle(tmp_path))])
    changed_dynamic = PromptFragmentRegistry([
        base_fragment(),
        *workspace_fragments(make_workspace_bundle(
            tmp_path, git="git branch: feature/changed")),
    ])
    changed_stable = PromptFragmentRegistry([
        base_fragment(),
        *workspace_fragments(make_workspace_bundle(
            tmp_path, rules="RULES: changed policy")),
    ])

    baseline = first.snapshot()
    dynamic = changed_dynamic.snapshot()
    stable = changed_stable.snapshot()
    assert dynamic.cache.stable_hash == baseline.cache.stable_hash
    assert dynamic.cache.full_hash != baseline.cache.full_hash
    assert stable.cache.stable_hash != baseline.cache.stable_hash


def test_build_loop_attaches_registry_and_cache_metadata(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
    ])
    loop, manager = asyncio.run(build_loop(args, {}, log=lambda _line: None))
    try:
        assert loop.prompt_registry is not None
        assert loop.prompt_snapshot is loop.prompt_registry.snapshot()
        assert loop.history[0].provider_meta == loop.prompt_snapshot.provider_meta()
        assert loop.prompt_snapshot.cache.stable_fragments[0] == "core:base"
    finally:
        asyncio.run(manager.shutdown())


def test_context_commands_list_toggle_cache_and_reset(tmp_path):
    bundle = make_workspace_bundle(tmp_path)
    registry = PromptFragmentRegistry([base_fragment(), *workspace_fragments(bundle)])
    loop = make_loop(tmp_path)
    loop.prompt_registry = registry
    loop.workspace_context = bundle
    apply_prompt_snapshot(loop, registry.snapshot())

    listed = run_context("/context fragments", loop, tmp_path)
    cache = run_context("/context cache", loop, tmp_path)
    disabled = run_context(
        "/context disable workspace:git", loop, tmp_path)
    assert "git branch: main" not in loop.history[0].content
    enabled = run_context(
        "/context enable workspace:git", loop, tmp_path)
    reset = run_context("/context reset", loop, tmp_path)

    assert any("[on ] core:base" in line for line in listed.output)
    assert any("stable fragments: core:base, workspace:rules" in line
               for line in cache.output)
    assert any("disabled fragment: workspace:git" in line for line in disabled.output)
    assert any("enabled fragment: workspace:git" in line for line in enabled.output)
    assert any("已清除" in line for line in reset.output)
    assert loop.history[0].provider_meta is not None


def test_context_refresh_uses_registry_and_preserves_skill_index(tmp_path):
    skill_dir = tmp_path / ".agents" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: refresh me\n---\nBODY\n",
        encoding="utf-8",
    )
    skills = SkillManager(tmp_path, home=tmp_path / "home")
    registry = PromptFragmentRegistry([base_fragment()])
    loop = make_loop(tmp_path)
    loop.prompt_registry = registry
    loop.skill_manager = skills
    apply_prompt_snapshot(loop, registry.snapshot())
    previous_generation = registry.generation

    result = run_context("/context refresh", loop, tmp_path, skills)

    assert any("已刷新" in line for line in result.output)
    assert registry.get("skills:index") is not None
    assert registry.get("workspace:tree") is not None
    assert "refresh me" in loop.history[0].content
    assert "<workspace_context>" in loop.history[0].content
    assert registry.generation > previous_generation


def test_anthropic_uses_fragment_cache_boundary_for_system_blocks():
    provider = AnthropicProvider(model="claude-x", api_key="k")
    messages = [
        Message.system(
            "STABLE\n\nDYNAMIC",
            provider_meta={"prompt_cache_boundaries": [len("STABLE")]},
        ),
        Message.user("hi"),
    ]

    request = provider.build_request(messages, None)
    payload = json.loads(request["body"].decode("utf-8"))

    assert payload["system"] == [
        {
            "type": "text",
            "text": "STABLE",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "\n\nDYNAMIC",
        },
    ]


def test_compact_preview_is_non_destructive_and_reports_boundary():
    history = [Message.system("SYS")]
    for index in range(6):
        history.append(Message.user(f"question-{index}"))
        history.append(Message.assistant(f"answer-{index}"))
    original = deepcopy(history)
    manager = ContextManager(
        MockProvider(), max_tokens=10**9, keep_recent=4)

    preview = manager.preview_compact(history, force=True)

    assert history == original
    assert preview.eligible
    assert preview.strategy == "summarize"
    assert preview.boundary is not None
    assert preview.boundary.head_end == 1
    assert preview.boundary.middle_start == 1
    assert preview.boundary.middle_end == len(history) - 4
    assert "question-0" in preview.preview_text
    assert "边界:" in preview.render()


def test_compact_preview_auto_skips_under_budget_history():
    history = [Message.system("SYS"), Message.user("hi"), Message.assistant("ok")]
    manager = ContextManager(MockProvider(), max_tokens=10**9)

    preview = manager.preview_compact(history, force=False)

    assert not preview.eligible
    assert preview.strategy == "none"
    assert "无需 compact" in preview.reason


def test_auto_eviction_does_not_report_summarize_boundary():
    history = [Message.system("SYS")]
    history.append(Message.user("question"))
    history.append(Message.assistant("answer"))
    history.append(Message.assistant(tool_calls=[ToolCall("t1", "read_file", {})]))
    history.append(Message.tool_result("t1", "X" * 500))
    history.append(Message.assistant(tool_calls=[ToolCall("t2", "read_file", {})]))
    history.append(Message.tool_result("t2", "recent"))
    for index in range(5):
        history.append(Message.user(f"q{index}"))
        history.append(Message.assistant(f"a{index}"))
    probe = deepcopy(history)
    ContextManager(
        MockProvider(), keep_recent_tools=1).evict_old_tool_results(probe)
    manager = ContextManager(
        MockProvider(), max_tokens=estimate_history_tokens(probe),
        keep_recent=4, keep_recent_tools=1)

    changed = asyncio.run(manager.maybe_compress(history))

    assert changed
    assert manager.last_compact_preview is not None
    assert manager.last_compact_preview.strategy == "evict_tools"
    assert manager.last_compact_preview.boundary is None


def test_compact_boundary_avoids_orphan_tool_result():
    history = [
        Message.system("SYS"),
        Message.user("inspect"),
        Message.assistant(tool_calls=[ToolCall("t1", "read_file", {})]),
        Message.tool_result("t1", "small result"),
        Message.assistant("one"),
        Message.user("two"),
        Message.assistant("three"),
        Message.user("four"),
        Message.assistant("five"),
        Message.user("six"),
    ]
    manager = ContextManager(
        MockProvider(), max_tokens=10**9, keep_recent=7)

    preview = manager.preview_compact(history, force=True)

    assert preview.boundary is not None
    assert preview.boundary.turn_aligned
    assert preview.boundary.middle_end == 5
    assert history[preview.boundary.tail_start].role == "user"
    assert history[preview.boundary.tail_start].role != "tool"


def test_compact_commands_preview_then_execute_with_boundary_details(tmp_path):
    history = [Message.system("SYS")]
    for index in range(6):
        history.append(Message.user(f"question-{index}"))
        history.append(Message.assistant(f"answer-{index}"))
    manager = ContextManager(
        MockProvider(scripted=["compressed summary"]),
        max_tokens=10**9,
        keep_recent=4,
    )
    loop = make_loop(tmp_path)
    loop.history = history
    loop.context_manager = manager
    original = deepcopy(history)

    preview = run_compact("/compact preview", loop, tmp_path)
    assert history == original
    assert any("strategy: summarize" in line for line in preview.output)
    assert any("边界:" in line for line in preview.output)

    compacted = run_compact("/compact", loop, tmp_path)
    assert any("已压缩" in line for line in compacted.output)
    assert any("strategy: summarize" in line for line in compacted.output)
    assert any("边界:" in line for line in compacted.output)
    assert history[1].content.startswith(SUMMARY_PREFIX)
    assert "compressed summary" in history[1].content
