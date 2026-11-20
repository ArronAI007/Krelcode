"""Step 109: one ToolPromptSpec feeds prompt, discovery and provider wires."""

from __future__ import annotations

import asyncio
import json

import pytest

from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_inspection import ContextInspectionService
from krelcode.agent.context_plan import ContextPlanSnapshot
from krelcode.agent.context_source import ContextRequest
from krelcode.agent.fragments import PromptFragmentRegistry, base_fragments
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.agent.prompt_plan import PromptPlanSnapshot
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.core.messages import Message
from krelcode.llm.providers.anthropic import tool_specs_to_anthropic
from krelcode.llm.providers.openai import tool_specs_to_openai
from krelcode.tools.base import ToolResult
from krelcode.tools.prompt import ToolPromptExample, ToolPromptSpec
from krelcode.tools.registry import ToolRegistry


class PromptTool:
    name = "inspect_widget"
    description = "legacy description"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    read_only = True

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


def _explicit_spec(name: str = "inspect_widget") -> ToolPromptSpec:
    return ToolPromptSpec(
        name=name,
        description="Inspect one widget without scanning the workspace.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        instructions="Read only the requested widget and cite its path.",
        when_to_use=("The user names a widget path",),
        when_not_to_use=("A repository-wide census is required",),
        examples=(ToolPromptExample(
            title="Inspect one widget",
            input={"path": "widgets/main.toml"},
            description="Use a narrow path.",
        ),),
        aliases=("widget",),
        source="plugin",
        version="7",
    )


def _assembler():
    prompt_registry = PromptFragmentRegistry(base_fragments())
    return PromptAssembler(
        ContextAssembler([]),
        registry_provider=lambda: prompt_registry,
    ), prompt_registry


def test_registry_pool_and_provider_wire_derive_from_one_prompt_spec():
    spec = _explicit_spec()
    registry = ToolRegistry()
    registry.register(PromptTool(), prompt_spec=spec)
    pool = TurnToolPool(registry)

    prompt_spec = pool.prompt_spec("inspect_widget")
    wire_specs = pool.tool_specs()

    assert prompt_spec is spec
    assert wire_specs == [{
        "name": "inspect_widget",
        "description": spec.description,
        "parameters": spec.as_tool_spec()["parameters"],
    }]
    assert tool_specs_to_anthropic(wire_specs)[0]["description"] == spec.description
    assert (
        tool_specs_to_openai(wire_specs)[0]["function"]["description"]
        == spec.description
    )
    assert pool.search_catalog("inspect widget")[0].description == spec.description
    assert pool.search_catalog("widget")[0].name == "inspect_widget"


def test_registry_rejects_prompt_spec_name_mismatch():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="不一致"):
        registry.register(PromptTool(), prompt_spec=_explicit_spec("other_tool"))


def test_prompt_assembler_renders_guidance_examples_and_keeps_wire_description(
    tmp_path,
):
    registry = ToolRegistry()
    registry.register(PromptTool(), prompt_spec=_explicit_spec())
    pool = TurnToolPool(registry)
    pool.tool_specs()
    assembler, prompt_registry = _assembler()
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=ContextRequest(
            turn_id="turn-step109",
            step=1,
            history=(Message.user("inspect widgets/main.toml"),),
            workspace_root=tmp_path,
            tool_pool=pool,
        ),
        prompt_snapshot=prompt_registry.snapshot(),
        tools=tuple(pool.prompt_specs()),
    )))
    provider_messages = assembler.provider_messages(
        plan, tools=pool.tool_specs())

    fragment = next(
        item for item in plan.fragments
        if item.id == "tool_playbook:tool:inspect_widget")
    assert "Read only the requested widget" in fragment.content
    assert "When to use:" in fragment.content
    assert "Do not use when:" in fragment.content
    assert "widgets/main.toml" in fragment.content
    assert provider_messages.tools[0]["description"] == (
        "Inspect one widget without scanning the workspace.")
    assert "instructions" not in provider_messages.tools[0]
    assert "when_to_use" not in provider_messages.tools[0]
    assert provider_messages.tool_prompt_specs[0].content_hash == (
        plan.tool_prompt_specs[0].content_hash)
    channel_records = provider_messages.channel_record()["tool_prompt_specs"]
    assert channel_records[0]["name"] == "inspect_widget"
    assert "Read only the requested widget" not in json.dumps(channel_records)


def test_known_tool_spec_is_enriched_with_default_playbook_without_drift(tmp_path):
    class ReadTool(PromptTool):
        name = "read_file"

    spec = ToolPromptSpec(
        name="read_file",
        description="Canonical targeted file reader.",
        parameters=PromptTool.parameters,
    )
    registry = ToolRegistry()
    registry.register(ReadTool(), prompt_spec=spec)
    pool = TurnToolPool(registry)
    pool.tool_specs()
    assembler, prompt_registry = _assembler()
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=ContextRequest(
            turn_id="turn-step109-read",
            step=1,
            history=(Message.user("read one file"),),
            workspace_root=tmp_path,
            tool_pool=pool,
        ),
        prompt_snapshot=prompt_registry.snapshot(),
        tools=tuple(pool.prompt_specs()),
    )))

    resolved = plan.tool_prompt_specs[0]
    assert resolved.description == "Canonical targeted file reader."
    assert resolved.playbook_id == "file-read"
    assert "exact file and line range" in resolved.instructions
    assert any(
        fragment.id == "tool_playbook:file-read"
        for fragment in plan.fragments)


def test_prompt_plan_snapshot_persists_content_free_tool_spec_records(tmp_path):
    registry = ToolRegistry()
    registry.register(PromptTool(), prompt_spec=_explicit_spec())
    pool = TurnToolPool(registry)
    pool.tool_specs()
    assembler, prompt_registry = _assembler()
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=ContextRequest(
            turn_id="turn-step109-snapshot",
            step=1,
            history=(Message.user("inspect"),),
            workspace_root=tmp_path,
            tool_pool=pool,
        ),
        prompt_snapshot=prompt_registry.snapshot(),
        tools=tuple(pool.prompt_specs()),
    )))

    snapshot = PromptPlanSnapshot.from_prompt_plan(
        plan,
        turn_id="turn-step109-snapshot",
        step=1,
        history_digest="history",
        tools=plan.tool_prompt_specs,
    )
    restored = PromptPlanSnapshot.from_dict(snapshot.as_dict())
    record = restored.tool_prompt_specs[0]

    assert restored.tool_count == 1
    assert record["name"] == "inspect_widget"
    assert record["instructions_chars"] > 0
    assert "Read only the requested widget" not in json.dumps(
        restored.as_dict())
    assert record["wire_hash"] == _explicit_spec().wire_hash
    assert record["content_hash"] == _explicit_spec().content_hash


def test_prompt_spec_mapping_normalizes_examples_aliases_and_input_schema():
    spec = ToolPromptSpec.from_mapping({
        "name": "mapping_tool",
        "description": "Mapping-compatible tool.",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        "examples": [{
            "title": "Use mapping input",
            "input": {"value": "x"},
            "description": "Mapping examples are accepted.",
        }],
        "aliases": "mapping alias",
    })

    assert spec.parameters["type"] == "object"
    assert spec.examples[0].title == "Use mapping input"
    assert spec.examples[0].input == {"value": "x"}
    assert spec.aliases == ("mapping alias",)


def test_context_inspection_exposes_tool_spec_metadata_without_body(tmp_path):
    registry = ToolRegistry()
    registry.register(PromptTool(), prompt_spec=_explicit_spec())
    pool = TurnToolPool(registry)
    pool.tool_specs()
    assembler, prompt_registry = _assembler()
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=ContextRequest(
            turn_id="turn-step109-context",
            step=1,
            history=(Message.user("inspect widgets/main.toml"),),
            workspace_root=tmp_path,
            tool_pool=pool,
        ),
        prompt_snapshot=prompt_registry.snapshot(),
        tools=tuple(pool.prompt_specs()),
    )))
    snapshot = PromptPlanSnapshot.from_prompt_plan(
        plan,
        turn_id="turn-step109-context",
        step=1,
        history_digest="history",
        tools=plan.tool_prompt_specs,
    )

    class Loop:
        last_context_plan = ContextPlanSnapshot(
            turn_id="turn-step109-context",
            step=1,
            prompt_plan=snapshot,
        )

    service = ContextInspectionService(Loop())
    output = "\n".join(service.plan_lines())
    why = "\n".join(service.why_lines("inspect_widget"))

    assert "tool prompt specs: 1" in output
    assert "guidance=1+1" in output
    assert "examples=1" in output
    assert "wire=" in output and "content=" in output
    assert "tool prompt specs: 1" in why
    assert "Inspect one widget" not in output + why
    assert "Read only the requested widget" not in output + why
