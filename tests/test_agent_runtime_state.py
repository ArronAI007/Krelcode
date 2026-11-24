"""Explicit AgentLoop runtime-state ownership and compatibility view."""

from __future__ import annotations

from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.contracts.tool import ToolContext
from krelcode.core.events import EventBus
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.registry import ToolRegistry


def _loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        MockProvider(scripted=["ok"]),
        ToolRegistry([]),
        ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        system_prompt="system",
    )


def test_runtime_state_groups_are_the_owned_source_of_truth(tmp_path: Path):
    loop = _loop(tmp_path)
    skill_manager = object()
    memory_store = object()
    status_renderer = object()
    extension_refresh = lambda: None

    assert loop.runtime_state.turn.history is loop.history
    assert loop.runtime_state.context.pending_context_refresh is False
    assert loop.runtime_state.prompt.prompt_profile_name == ""
    assert loop.services.core.llm is loop.llm
    assert loop.services.context.working_context is loop.working_context_store
    assert loop.services.prompt.assembler is loop.prompt_assembler

    loop.update_runtime_state(
        pending_context_refresh=True,
        prompt_profile_name="deepseek",
        skill_manager=skill_manager,
        memory_store=memory_store,
        status_line_renderer=status_renderer,
        base_skill_paths=("skills",),
        extension_refresh=extension_refresh,
    )

    assert loop.runtime_state.context.pending_context_refresh is True
    assert loop.pending_context_refresh is True
    assert loop.runtime_state.prompt.prompt_profile_name == "deepseek"
    assert loop.prompt_profile_name == "deepseek"
    assert loop.services.extensions.skills is skill_manager
    assert loop.services.session.memory is memory_store
    assert loop.services.extensions.status_line_renderer is status_renderer
    assert loop.services.extensions.base_skill_paths == ("skills",)
    assert loop.services.extensions.extension_refresh is extension_refresh
    assert loop.skill_manager is skill_manager
    assert loop.memory_store is memory_store
    assert loop.status_line_renderer is status_renderer
    assert not loop.runtime_state.has_field("skill_manager")
    assert not loop.runtime_state.has_field("memory_store")
    assert not loop.runtime_state.has_field("status_line_renderer")
    assert not loop.runtime_state.has_field("extension_refresh")


def test_runtime_state_update_keeps_dependency_container_in_sync(tmp_path: Path):
    loop = _loop(tmp_path)
    original = loop.dependencies.llm
    replacement = MockProvider(scripted=["replacement"])

    loop.update_runtime_state(llm=replacement)

    assert loop.dependencies.llm is replacement
    assert loop.runtime_state.turn.current_turn_id == ""
    assert original is not replacement


def test_runtime_state_update_rejects_unknown_attributes(tmp_path: Path):
    loop = _loop(tmp_path)

    try:
        loop.update_runtime_state(not_an_owned_field=True)
    except AttributeError as exc:
        assert str(exc) == "not_an_owned_field"
    else:
        raise AssertionError("unknown runtime state was accepted")
