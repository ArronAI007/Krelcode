"""生产级 Base Prompt section 与模型 profile 解析测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from krelcode.agent.fragments import (
    LAYER_L1_BASE_AGENT,
    STABILITY_STATIC,
    TRUST_MANAGED,
    PromptFragmentRegistry,
    apply_prompt_snapshot,
    base_fragment,
    base_fragments,
    replace_base_fragments,
)
from krelcode.agent.context_inspection import ContextInspectionService
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompts import (
    DEFAULT_PROMPT_PROFILE,
    PROMPT_BASE_SECTIONS,
    ModelPromptProfile,
    build_system_prompt,
    resolve_prompt_profile,
)
from krelcode.agent.usage import UsageTracker
from krelcode.app import build_loop, build_model_switcher, build_parser
from krelcode.core.errors import ConfigError
from krelcode.core.events import EventBus
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def test_default_and_model_profiles_resolve_deterministically():
    assert resolve_prompt_profile("mock", "mock-model").name == "default"
    assert resolve_prompt_profile(
        "openai", "deepseek-chat").name == "deepseek"
    assert resolve_prompt_profile(
        "openai", "deepseek-reasoner").name == "deepseek"
    assert resolve_prompt_profile(
        "anthropic", "claude-sonnet-4-5").name == "claude"
    assert resolve_prompt_profile("openai", "gpt-4o-mini").name == "openai"
    assert resolve_prompt_profile(
        "gemini", "gemini-2.5-flash").name == "gemini"
    assert resolve_prompt_profile(
        "openai-compatible", "custom-chat-model").name == "openai"


def test_model_family_match_wins_over_compatible_provider():
    resolution = resolve_prompt_profile("openai", "claude-sonnet-4-5")
    assert resolution.name == "claude"
    assert resolution.matched_by == "model"


def test_explicit_profile_overrides_auto_and_unknown_profile_fails():
    resolution = resolve_prompt_profile(
        "openai", "gpt-4o-mini", configured="deepseek")
    assert resolution.name == "deepseek"
    assert resolution.matched_by == "configured"
    with pytest.raises(ConfigError, match="未知 prompt profile"):
        resolve_prompt_profile(
            "openai", "gpt-4o-mini", configured="not-a-profile")


def test_longest_model_pattern_wins(monkeypatch):
    import krelcode.agent.prompts as prompts

    short = replace(
        DEFAULT_PROMPT_PROFILE,
        name="short",
        model_patterns=("gpt-*",),
    )
    long = replace(
        DEFAULT_PROMPT_PROFILE,
        name="long",
        model_patterns=("gpt-4*",),
    )
    monkeypatch.setattr(prompts, "PROMPT_PROFILES", (short, long))

    resolution = prompts.resolve_prompt_profile("openai", "gpt-4.1-mini")
    assert resolution.name == "long"
    assert resolution.matched_by == "model"


def test_base_fragments_are_stable_l1_sections_with_trace_metadata():
    resolution = resolve_prompt_profile("openai", "deepseek-chat")
    fragments = base_fragments(resolution)

    assert tuple(fragment.id for fragment in fragments) == tuple(
        section.id for section in PROMPT_BASE_SECTIONS) + (
            "core:base:profile:deepseek",
        )
    assert all(fragment.layer == LAYER_L1_BASE_AGENT for fragment in fragments)
    assert all(fragment.stability == STABILITY_STATIC for fragment in fragments)
    assert all(fragment.cacheable for fragment in fragments)
    assert all(fragment.trust == TRUST_MANAGED for fragment in fragments)
    assert all(not fragment.toggleable for fragment in fragments)
    assert all(fragment.metadata["prompt_profile"] == "deepseek"
               for fragment in fragments)
    assert all(fragment.metadata["prompt_profile_match"] == "model"
               for fragment in fragments)
    assert all(fragment.metadata["base_prompt_hash"]
               == resolution.base_prompt_hash for fragment in fragments)
    assert "For DeepSeek models" in fragments[-1].content


def test_profile_cannot_override_required_safety_sections():
    safety = next(
        section for section in PROMPT_BASE_SECTIONS
        if section.id == "core:base:safety"
    )
    with pytest.raises(ValueError, match="不能覆盖"):
        ModelPromptProfile(
            name="unsafe",
            title="Unsafe",
            additional_sections=(replace(safety, required=False),),
        )

    for profile in ("claude", "deepseek", "openai", "gemini"):
        fragments = base_fragments(profile)
        installed_safety = next(
            fragment for fragment in fragments
            if fragment.id == "core:base:safety"
        )
        assert installed_safety.content == safety.content


def test_compatibility_base_fragment_and_profile_builder_remain_available():
    aggregate = base_fragment()
    assert aggregate.id == "core:base"
    assert aggregate.content == build_system_prompt()
    assert "For DeepSeek models" in build_system_prompt(
        provider="openai", model="deepseek-chat")


def test_replace_base_fragments_is_atomic_and_removes_old_profile(tmp_path):
    registry = PromptFragmentRegistry(base_fragments("deepseek"))
    registry.register(base_fragment_for_retained_source(tmp_path))
    old_ids = {fragment.id for fragment in registry.list()}

    replace_base_fragments(registry, "claude", provider="anthropic")
    new_ids = {fragment.id for fragment in registry.list()}

    assert "core:base:profile:deepseek" in old_ids
    assert "core:base:profile:deepseek" not in new_ids
    assert "core:base:profile:claude" in new_ids
    assert "project:retained" in new_ids
    assert len(new_ids) == len(registry.list())


def base_fragment_for_retained_source(_tmp_path):
    from krelcode.agent.fragments import PromptFragment

    return PromptFragment(
        id="project:retained",
        source="project",
        title="Retained",
        content="PROJECT RETAINED",
        cacheable=True,
        scope="project",
    )


def test_build_loop_uses_split_base_and_configured_profile(tmp_path):
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
    ])
    loop, manager = asyncio.run(build_loop(
        args, {"prompt": {"profile": "deepseek"}}, log=lambda _line: None))
    try:
        ids = tuple(fragment.id for fragment in loop.prompt_registry.list())
        assert ids[0] == "core:base"
        assert "core:base:operating-loop" in ids
        assert "core:base:safety" in ids
        assert "core:base:profile:deepseek" in ids
        assert loop.prompt_profile_name == "deepseek"
        assert loop.prompt_profile_resolution.matched_by == "configured"
        assert loop.prompt_snapshot is loop.prompt_registry.snapshot()
    finally:
        asyncio.run(manager.shutdown())


def test_model_switch_refreshes_auto_profile_and_stable_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")
    registry = PromptFragmentRegistry(base_fragments())
    loop = AgentLoop(
        llm=MockProvider(),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt=registry.snapshot().text,
    )
    loop.prompt_registry = registry
    loop.prompt_profile_config = "auto"
    apply_prompt_snapshot(loop, registry.snapshot())
    old_stable_hash = loop.prompt_snapshot.cache.stable_hash
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path),
    ])
    switch = build_model_switcher(
        args, {}, loop, UsageTracker(model="mock-model"))

    result = switch("openai/gpt-4o-mini")
    assert "prompt openai" in result
    assert loop.prompt_profile_name == "openai"
    assert loop.prompt_snapshot.cache.stable_hash != old_stable_hash
    assert loop.prompt_registry.get("core:base:profile:openai") is not None

    openai_hash = loop.prompt_snapshot.cache.stable_hash
    result = switch("anthropic/claude-sonnet-4-5")
    assert "prompt claude" in result
    assert loop.prompt_profile_name == "claude"
    assert loop.prompt_snapshot.cache.stable_hash != openai_hash
    assert loop.prompt_registry.get("core:base:profile:openai") is None
    assert loop.prompt_registry.get("core:base:profile:claude") is not None


def test_profile_provenance_is_visible_without_prompt_body(tmp_path):
    resolution = resolve_prompt_profile("openai", "deepseek-chat")
    registry = PromptFragmentRegistry(base_fragments(resolution))
    loop = AgentLoop(
        llm=MockProvider(fallback="finished"),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt=registry.snapshot().text,
    )
    loop.prompt_registry = registry
    loop.prompt_profile = resolution.profile
    loop.prompt_profile_resolution = resolution
    loop.prompt_profile_name = resolution.name
    apply_prompt_snapshot(loop, registry.snapshot())

    asyncio.run(loop.run("inspect the request"))

    lines = ContextInspectionService(loop).why_lines("core:base:safety")
    output = "\n".join(lines)
    assert "prompt profile: deepseek" in output
    assert "match=model" in output
    assert "provider=openai" in output
    assert f"base hash: {resolution.base_prompt_hash}" in output
    assert "Instruction authority is strict" not in output


def test_subagent_resolves_profile_from_its_own_model(tmp_path):
    class DeepSeekCompatibleProvider(MockProvider):
        name = "openai"

        def __init__(self):
            super().__init__(fallback="delegated result")
            self.model = "deepseek-reasoner"

    async def go():
        manager = SubagentManager(
            llm=DeepSeekCompatibleProvider(),
            tool_factory=lambda: [],
            tool_context=ToolContext(workspace_root=tmp_path),
            policy=AllowAllPolicy(),
            bus=EventBus(),
        )
        handle = await manager.spawn("analyze the delegated task")
        handle = await manager.wait(handle.id, timeout=5)
        assert handle.status == "completed"
        assert handle.prompt_profile == "deepseek"
        assert handle.loop.prompt_profile_name == "deepseek"
        base = handle.loop.prompt_registry.get("subagent:base:0")
        assert base is not None
        assert base.metadata["prompt_profile"] == "deepseek"
        assert base.metadata["prompt_profile_match"] == "model"
        assert "For DeepSeek models" in base.content
        await manager.shutdown()

    asyncio.run(go())
