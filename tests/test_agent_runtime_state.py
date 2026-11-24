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

    assert loop.runtime_state.turn.history is loop.history
    assert loop.runtime_state.context.pending_context_refresh is False
    assert loop.runtime_state.prompt.prompt_profile_name == ""

    loop.update_runtime_state(
        pending_context_refresh=True,
        prompt_profile_name="deepseek",
        skill_manager=object(),
    )

    assert loop.runtime_state.context.pending_context_refresh is True
    assert loop.pending_context_refresh is True
    assert loop.runtime_state.prompt.prompt_profile_name == "deepseek"
    assert loop.prompt_profile_name == "deepseek"
    assert loop.runtime_state.extensions.skill_manager is not None


def test_runtime_state_update_keeps_dependency_container_in_sync(tmp_path: Path):
    loop = _loop(tmp_path)
    original = loop.dependencies.llm
    replacement = MockProvider(scripted=["replacement"])

    loop.llm = replacement

    assert loop.dependencies.llm is replacement
    assert loop.runtime_state.turn.current_turn_id == ""
    assert original is not replacement
