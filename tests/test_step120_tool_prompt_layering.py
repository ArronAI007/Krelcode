"""Step 120: base identity Prompt vs concrete tool-owned operating Prompts."""

from __future__ import annotations

import asyncio
import json

from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import ContextRequest
from krelcode.agent.fragments import PromptFragmentRegistry, base_fragments
from krelcode.agent.intent import MODE_EXPLAIN_FILES, TurnIntent
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.agent.prompts import PROMPT_BASE_SECTIONS
from krelcode.agent.tool_playbooks import DEFAULT_TOOL_PLAYBOOK_CATALOG
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.app import default_tools
from krelcode.core.messages import Message
from krelcode.lsp.tools import build_lsp_tools
from krelcode.skills.tools import (
    ListSkillResourcesTool,
    ListSkillsTool,
    LoadSkillTool,
    ReadSkillResourceTool,
    RunSkillScriptTool,
)
from krelcode.subagent.tools import build_subagent_tools
from krelcode.tools.extension.ask_user import AskUserTool
from krelcode.tools.builtin.edit_file import EditFileTool
from krelcode.tools.builtin.memory_search import MemorySearchTool
from krelcode.tools.builtin.prompt_lab import PromptLabTool
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.run_command import RunCommandTool
from krelcode.tools.base import ToolResult
from krelcode.tools.prompt import ToolPromptSpec
from krelcode.tools.registry import ToolRegistry
from krelcode.web.tools import WebFetchTool, WebSearchTool


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _all_known_tools():
    manager = object()
    return [
        *default_tools(),
        MemorySearchTool(lambda: None),
        PromptLabTool(),
        *build_subagent_tools(manager),
        *build_lsp_tools(manager),
        WebFetchTool(manager),
        WebSearchTool(manager),
        ListSkillsTool(manager),
        LoadSkillTool(manager),
        ListSkillResourcesTool(manager),
        ReadSkillResourceTool(manager),
        RunSkillScriptTool(manager),
    ]


def test_base_prompt_is_identity_and_principles_not_tool_playbooks():
    text = _normalized(" ".join(
        section.content for section in PROMPT_BASE_SECTIONS)).lower()

    for concrete_rule in (
        "read_file",
        "write_file",
        "edit_file",
        "run_command",
        "spawn_agent",
        "old_string",
        "git reset --hard",
        "replace_all",
        "read-only tools may run in parallel",
        "write tools must run serially",
    ):
        assert concrete_rule not in text

    assert "select the most direct available tool" in text
    assert "tool-specific usage, ordering, safety, and permission rules" in text
    assert "that tool's own prompt and runtime contract" in text


def test_every_known_tool_owns_usage_when_and_exclusion_contract():
    tools = _all_known_tools()
    names = [tool.name for tool in tools]

    assert len(names) == len(set(names))
    assert len(names) >= 30
    for tool in tools:
        spec = ToolPromptSpec.from_tool(tool, source="canonical")
        assert spec.instructions.strip(), tool.name
        assert spec.when_to_use, tool.name
        assert spec.when_not_to_use, tool.name
        assert spec.version, tool.name
        assert len(spec.content_hash) == 64
        assert len(spec.wire_hash) == 64


def test_complex_tool_prompts_cover_their_special_safety_protocols():
    run = ToolPromptSpec.from_tool(RunCommandTool(), source="builtin")
    run_text = _normalized(run.instructions).lower()
    assert "prefer read_file, grep, glob, edit_file, and write_file" in run_text
    assert "git safety protocol" in run_text
    assert "git reset --hard" in run_text
    assert "force-push" in run_text
    assert "sandbox" in run_text
    assert "approval policy" in run_text
    assert len(run.examples) == 2

    edit = ToolPromptSpec.from_tool(EditFileTool(), source="builtin")
    edit_text = _normalized(edit.instructions).lower()
    assert "old_string must match" in edit_text
    assert "replace_all" in edit_text
    assert "inspect the unified diff" in edit_text
    assert "file mutations must remain ordered" in edit_text
    assert len(edit.examples) == 2

    ask = ToolPromptSpec.from_tool(AskUserTool(), source="canonical")
    ask_text = _normalized(ask.instructions).lower()
    assert "first inspect the repository" in ask_text
    assert "single decision" in ask_text
    assert "routine reversible work" in ask_text
    assert len(ask.examples) == 1


def test_tool_owned_prompt_wins_and_playbook_is_only_legacy_fallback():
    class LegacyReadTool:
        name = "read_file"
        description = "Legacy reader without an owned operating Prompt."
        parameters = {"type": "object", "properties": {}}
        read_only = True

        async def execute(self, args, ctx):
            return ToolResult(content="ok")

    owned = ToolPromptSpec.from_tool(ReadFileTool(), source="builtin")
    enriched_owned = DEFAULT_TOOL_PLAYBOOK_CATALOG.enrich_prompt_spec(owned)
    assert enriched_owned.playbook_id == "file-read"
    assert enriched_owned.instructions == owned.instructions

    legacy = ToolPromptSpec.from_tool(LegacyReadTool(), source="builtin")
    enriched_legacy = DEFAULT_TOOL_PLAYBOOK_CATALOG.enrich_prompt_spec(legacy)
    assert enriched_legacy.playbook_id == "file-read"
    assert "Read the exact file and line range" in enriched_legacy.instructions


def test_prompt_assembler_renders_tool_prompt_as_l5_without_wire_leakage(tmp_path):
    registry = ToolRegistry([ReadFileTool(), RunCommandTool()])
    pool = TurnToolPool(registry)
    prompt_registry = PromptFragmentRegistry(base_fragments())
    assembler = PromptAssembler(
        ContextAssembler([]),
        registry_provider=lambda: prompt_registry,
    )

    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=ContextRequest(
            turn_id="step120-layering",
            step=1,
            history=(Message.user("inspect and verify the parser fix"),),
            workspace_root=tmp_path,
            tool_pool=pool,
        ),
        prompt_snapshot=prompt_registry.snapshot(),
        tools=tuple(pool.prompt_specs()),
    )))
    provider_messages = assembler.provider_messages(
        plan, tools=pool.tool_specs())

    run_fragment = next(
        fragment for fragment in plan.fragments
        if fragment.id == "tool_playbook:command-execution")
    assert "Git safety protocol" in run_fragment.content
    assert run_fragment.layer == "L5_MODE_PERMISSION_TOOL_CONTRACT"

    wire = provider_messages.tools
    assert {item["name"] for item in wire} == {"read_file", "run_command"}
    for item in wire:
        assert set(item) == {"name", "description", "parameters"}
        assert "Git safety protocol" not in json.dumps(item)
        assert "old_string must match" not in json.dumps(item)


def test_filtered_tool_prompt_never_leaks_into_the_request(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    registry = ToolRegistry([ReadFileTool(), RunCommandTool()])
    pool = TurnToolPool(
        registry,
        intent=TurnIntent(mode=MODE_EXPLAIN_FILES, target_files=(target,)),
    )

    fragment_text = "\n".join(
        fragment.content
        for fragment in DEFAULT_TOOL_PLAYBOOK_CATALOG.fragments_for_prompt_specs(
            pool.prompt_specs())
    )

    assert pool.loaded_names() == ["read_file"]
    assert "Use read_file" in fragment_text
    assert "run_command" not in fragment_text
    assert "Git safety protocol" not in fragment_text
