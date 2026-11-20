"""Tool-level Prompt Playbook selection and assembly tests."""

from __future__ import annotations

import asyncio

import pytest

from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import ContextRequest
from krelcode.agent.fragments import (
    LAYER_L5_MODE_CONTRACT,
    STABILITY_SESSION,
    TRUST_MANAGED,
    PromptFragmentRegistry,
    base_fragments,
)
from krelcode.agent.intent import MODE_EXPLAIN_FILES, TurnIntent
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.agent.tool_playbooks import (
    ToolGuidance,
    ToolPlaybook,
    ToolPlaybookCatalog,
    tool_playbook_fragments,
)
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.core.messages import Message
from krelcode.tools.base import ToolResult, ToolTier
from krelcode.tools.registry import ToolRegistry


class StubTool:
    read_only = True
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, tier: ToolTier = ToolTier.CORE) -> None:
        self.name = name
        self.description = f"Stub tool {name}"
        self.tool_tier = tier

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


def test_catalog_selects_only_loaded_tool_guidance_in_stable_order():
    fragments = tool_playbook_fragments(
        ["run_command", "read_file", "edit_file", "read_file"])

    assert [fragment.id for fragment in fragments] == [
        "tool_playbook:file-read",
        "tool_playbook:workspace-edit",
        "tool_playbook:command-execution",
    ]
    assert all(fragment.layer == LAYER_L5_MODE_CONTRACT for fragment in fragments)
    assert all(fragment.stability == STABILITY_SESSION for fragment in fragments)
    assert all(fragment.cacheable for fragment in fragments)
    assert all(not fragment.compactable for fragment in fragments)
    assert all(not fragment.toggleable for fragment in fragments)
    assert all(fragment.scope == "managed" for fragment in fragments)
    assert all(fragment.trust == TRUST_MANAGED for fragment in fragments)
    assert all(fragment.metadata["instruction"] is True for fragment in fragments)
    assert fragments[0].metadata["tool_playbook_active_tools"] == (
        "read_file",
    )
    assert "read_file" in fragments[0].content
    assert "edit_file" in fragments[1].content
    assert "glob" not in fragments[1].content
    assert "run_command" in fragments[2].content


def test_group_playbook_renders_only_the_active_member_guidance():
    selected = tool_playbook_fragments(["grep", "repo_map"])

    assert len(selected) == 1
    playbook = selected[0]
    assert playbook.id == "tool_playbook:workspace-search"
    assert playbook.metadata["tool_playbook_active_tools"] == (
        "grep", "repo_map")
    assert "tool name=\"grep\"" in playbook.content
    assert "tool name=\"repo_map\"" in playbook.content
    assert "tool name=\"glob\"" not in playbook.content
    assert "tool name=\"find_refs\"" not in playbook.content


def test_unknown_and_unloaded_discoverable_tools_do_not_add_playbooks():
    registry = ToolRegistry([
        StubTool("read_file"),
        StubTool("repo_map", ToolTier.DISCOVERABLE),
    ])
    pool = TurnToolPool(registry)

    assert [fragment.id for fragment in
            tool_playbook_fragments(pool.loaded_names())] == [
        "tool_playbook:file-read"]

    assert pool.load_tools(["repo_map"]) == ("repo_map",)
    assert [fragment.id for fragment in
            tool_playbook_fragments(pool.loaded_names())] == [
        "tool_playbook:file-read",
        "tool_playbook:workspace-search",
    ]
    assert tool_playbook_fragments(["unknown_tool"]) == ()


def test_runtime_filtered_tools_cannot_leak_playbook_instructions(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    registry = ToolRegistry([
        StubTool("read_file"),
        StubTool("edit_file"),
        StubTool("run_command"),
    ])
    intent = TurnIntent(
        mode=MODE_EXPLAIN_FILES,
        target_files=(target,),
    )
    pool = TurnToolPool(registry, intent=intent)

    assert [fragment.id for fragment in
            tool_playbook_fragments(pool.loaded_names())] == [
        "tool_playbook:file-read"]


def test_prompt_assembler_injects_playbooks_as_l5_cacheable_fragments(tmp_path):
    registry = ToolRegistry([
        StubTool("read_file"),
        StubTool("edit_file"),
    ])
    pool = TurnToolPool(registry)
    prompt_registry = PromptFragmentRegistry(base_fragments())
    assembler = PromptAssembler(
        ContextAssembler([]),
        registry_provider=lambda: prompt_registry,
    )
    context_request = ContextRequest(
        turn_id="turn-playbook",
        step=1,
        history=(Message.user("fix the file"),),
        workspace_root=tmp_path,
        tool_pool=pool,
    )

    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=context_request,
        prompt_snapshot=prompt_registry.snapshot(),
        tools=tuple(pool.tool_specs()),
    )))
    provider_messages = assembler.provider_messages(plan, tools=pool.tool_specs())

    playbooks = [
        fragment for fragment in plan.fragments
        if fragment.metadata.get("kind") == "tool_playbook"
    ]
    assert [fragment.id for fragment in playbooks] == [
        "tool_playbook:file-read",
        "tool_playbook:workspace-edit",
    ]
    assert plan.cache.offset > 0
    assert all(fragment.id in plan.cache.stable_fragments for fragment in playbooks)
    assert '<tool_playbook id="file-read"' in plan.system_text
    assert '<tool_playbook id="workspace-edit"' in plan.system_text
    assert any(
        contribution.source.startswith("tool_playbook:")
        for contribution in plan.token_contributions
    )
    block = next(
        item for item in provider_messages.instruction_blocks
        if item.id == "tool_playbook:file-read")
    assert block.metadata["tool_playbook"] == "file-read"
    assert block.metadata["instruction_source"] == "managed"


def test_provider_without_advertised_tools_receives_no_playbooks(tmp_path):
    registry = ToolRegistry([StubTool("read_file")])
    pool = TurnToolPool(registry)
    prompt_registry = PromptFragmentRegistry(base_fragments())
    assembler = PromptAssembler(
        ContextAssembler([]),
        registry_provider=lambda: prompt_registry,
    )
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=ContextRequest(
            turn_id="turn-no-tools",
            step=1,
            history=(Message.user("hello"),),
            workspace_root=tmp_path,
            tool_pool=pool,
        ),
        prompt_snapshot=prompt_registry.snapshot(),
        tools=(),
    )))

    assert not [
        fragment for fragment in plan.fragments
        if fragment.metadata.get("kind") == "tool_playbook"
    ]


def test_playbook_content_hash_changes_with_active_subset():
    by_grep = tool_playbook_fragments(["grep"])[0]
    by_repo_map = tool_playbook_fragments(["repo_map"])[0]

    assert by_grep.id == by_repo_map.id
    assert by_grep.metadata["tool_playbook_active_tools"] == ("grep",)
    assert by_repo_map.metadata["tool_playbook_active_tools"] == ("repo_map",)
    assert (
        by_grep.metadata["tool_playbook_content_hash"]
        != by_repo_map.metadata["tool_playbook_content_hash"]
    )


def test_catalog_rejects_ambiguous_tool_ownership():
    with pytest.raises(ValueError, match="同时属于"):
        ToolPlaybookCatalog([
            ToolPlaybook(
                id="one", title="One", priority=1,
                guidance=(ToolGuidance("read_file", "first"),),
            ),
            ToolPlaybook(
                id="two", title="Two", priority=2,
                guidance=(ToolGuidance("read_file", "second"),),
            ),
        ])
