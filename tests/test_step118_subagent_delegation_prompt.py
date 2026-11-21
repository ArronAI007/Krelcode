"""Step 118: parent-facing delegation meta Prompt and fork handoff guidance."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from krelcode.agent.tool_playbooks import DEFAULT_TOOL_PLAYBOOK_CATALOG
from krelcode.subagent.prompt import (
    SPAWN_AGENT_BASE_PROMPT,
    SPAWN_AGENT_FORK_ADDENDUM,
    SPAWN_AGENT_PROMPT_VERSION,
    build_spawn_agent_prompt,
)
from krelcode.subagent.tools import SpawnAgentTool
from krelcode.tools.base import ToolContext
from krelcode.tools.prompt import ToolPromptSpec


def test_base_delegation_prompt_teaches_mental_model_no_guessing_and_examples():
    prompt = build_spawn_agent_prompt(include_fork=False)

    assert "Inject the mental model" in prompt
    assert "Current state:" in prompt
    assert "Why delegate:" in prompt
    assert "Starting points:" in prompt
    assert "Boundaries:" in prompt
    assert "Verification:" in prompt
    assert "Do not make the subagent guess" in prompt
    assert "Good delegation example:" in prompt
    assert "Bad delegation examples:" in prompt
    assert "Investigate the bug we discussed" in prompt
    assert "Fork-context handoff" not in prompt
    assert SPAWN_AGENT_FORK_ADDENDUM not in prompt


def test_fork_addendum_requires_explicit_state_instead_of_copied_history():
    prompt = build_spawn_agent_prompt(include_fork=True)
    flat = " ".join(prompt.split())

    assert SPAWN_AGENT_BASE_PROMPT.strip() in prompt
    assert SPAWN_AGENT_FORK_ADDENDUM.strip() in prompt
    assert "does not copy the parent's full conversation history" in flat
    assert "confirmed facts, unresolved assumptions" in flat
    assert "completed work, remaining goal, and active blockers" in flat
    assert "continue as before" in flat
    assert "not a copied brain" in flat


def test_spawn_agent_exposes_canonical_tool_prompt_spec():
    tool = SpawnAgentTool(manager=None)  # type: ignore[arg-type]
    spec = ToolPromptSpec.from_tool(tool, source="builtin")

    assert spec.name == "spawn_agent"
    assert spec.instructions == build_spawn_agent_prompt(include_fork=True)
    assert spec.version == SPAWN_AGENT_PROMPT_VERSION
    assert spec.when_to_use
    assert spec.when_not_to_use
    assert len(spec.examples) == 2
    assert spec.examples[0].input["isolated"] is True
    assert spec.examples[1].input["fork_context"] is True
    assert "fork_context 仅保留兼容标记" in spec.description
    assert "不会继承父代理完整对话历史" in spec.description
    assert len(spec.content_hash) == 64


def test_spawn_agent_prompt_hash_is_deterministic():
    first = ToolPromptSpec.from_tool(
        SpawnAgentTool(manager=None), source="builtin")  # type: ignore[arg-type]
    second = ToolPromptSpec.from_tool(
        SpawnAgentTool(manager=None), source="builtin")  # type: ignore[arg-type]

    assert first.content_hash == second.content_hash
    assert first.wire_hash == second.wire_hash


def test_spawn_agent_schema_still_uses_the_existing_argument_contract():
    properties = SpawnAgentTool.parameters["properties"]

    assert set(properties) == {"task", "isolated", "fork_context", "definition"}
    assert SpawnAgentTool.parameters["required"] == ["task"]
    assert "心智模型" in properties["task"]["description"]
    assert "不得依赖父对话" in properties["task"]["description"]


def test_spawn_agent_playbook_renders_meta_prompt_and_fork_guidance():
    tool = SpawnAgentTool(manager=None)  # type: ignore[arg-type]
    spec = ToolPromptSpec.from_tool(tool, source="builtin")
    enriched = DEFAULT_TOOL_PLAYBOOK_CATALOG.enrich_prompt_spec(spec)
    flat = " ".join(enriched.instructions.split())

    assert enriched.playbook_id == "subagents"
    assert enriched.instructions.startswith(build_spawn_agent_prompt().strip())
    assert "mental-model handoff" in flat
    assert "does not copy the parent's full history" in flat

    fragment = DEFAULT_TOOL_PLAYBOOK_CATALOG.fragments_for_prompt_specs(
        [spec])[0]
    assert fragment.id == "tool_playbook:subagents"
    assert "Inject the mental model" in fragment.content
    assert "Fork-context handoff" in fragment.content
    assert "Do not make the child guess" in fragment.content


class _CapturingManager:
    def __init__(self) -> None:
        self.task = ""
        self.fork_context = None

    async def spawn(self, task, isolated=None, fork_context=None, *, definition=None):
        self.task = task
        self.fork_context = fork_context
        return SimpleNamespace(
            id="a1",
            branch=None,
            definition=definition,
            forked=bool(fork_context),
            sidechain_path=None,
        )


def test_meta_prompt_is_not_injected_into_the_child_task(tmp_path):
    async def go():
        manager = _CapturingManager()
        tool = SpawnAgentTool(manager)  # type: ignore[arg-type]
        task = (
            "Confirmed bug in src/a.py: parse() drops blank fields. "
            "Reproduce with tests/test_a.py::test_blank. Apply the minimal fix, "
            "run the focused test, and report remaining uncertainty."
        )

        result = await tool.execute(
            {"task": task, "fork_context": True},
            ToolContext(workspace_root=tmp_path),
        )

        assert not result.is_error
        assert manager.task == task
        assert manager.fork_context is True
        assert build_spawn_agent_prompt() not in manager.task
        assert "ToolPromptSpec" not in manager.task

    asyncio.run(go())
