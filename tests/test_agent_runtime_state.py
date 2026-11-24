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
    )

    assert loop.runtime_state.context.pending_context_refresh is True
    assert loop.pending_context_refresh is True
    assert loop.runtime_state.prompt.prompt_profile_name == "deepseek"
    assert loop.prompt_profile_name == "deepseek"
    assert loop.services.extensions.skills is skill_manager
    assert loop.services.session.memory is memory_store
    assert loop.skill_manager is skill_manager
    assert loop.memory_store is memory_store
    assert not loop.runtime_state.has_field("skill_manager")
    assert not loop.runtime_state.has_field("memory_store")


def test_runtime_state_update_keeps_dependency_container_in_sync(tmp_path: Path):
    loop = _loop(tmp_path)
    original = loop.dependencies.llm
    replacement = MockProvider(scripted=["replacement"])

    loop.llm = replacement

    assert loop.dependencies.llm is replacement
    assert loop.runtime_state.turn.current_turn_id == ""
    assert original is not replacement
