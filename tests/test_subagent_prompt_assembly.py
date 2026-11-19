"""Subagent prompts use the same fragment and evidence contracts as main."""

from __future__ import annotations

import asyncio

from krelcode.agent.context_inspection import ContextInspectionService
from krelcode.agent.fragments import (
    LAYER_L0_MANAGED_POLICY,
    LAYER_L1_BASE_AGENT,
    LAYER_L2_USER_GLOBAL,
    LAYER_L3_PROJECT_RULES,
    LAYER_L4_AGENT_STYLE,
    LAYER_L5_MODE_CONTRACT,
    LAYER_L6_CATALOG,
    LAYER_L7_EVIDENCE,
    skills_fragment,
)
from krelcode.core.events import EventBus
from krelcode.core.messages import Message
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.memory import MemoryKind, MemoryStore
from krelcode.subagent.definition import AgentDefinition
from krelcode.subagent.instructions import build_subagent_instructions
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.read_file import ReadFileTool


class EchoProvider:
    name = "echo-subagent"
    model = "echo"
    capabilities = ProviderCapabilities()

    async def stream(self, messages, tools=None):
        yield StreamFinished(
            message=Message.assistant("done"),
            finish_reason="stop",
            usage={},
        )


def test_instruction_bundle_maps_policy_sources_to_fragments(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (home / ".krelcode").mkdir(parents=True)
    (workspace / "pkg").mkdir(parents=True)
    (home / ".krelcode" / "AGENTS.md").write_text(
        "USER RULE", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("PROJECT RULE", encoding="utf-8")
    (workspace / "pkg" / "AGENTS.md").write_text(
        "DIRECTORY RULE", encoding="utf-8")
    (workspace / "pkg" / "file.py").write_text("value = 1\n", encoding="utf-8")
    managed = tmp_path / "managed.md"
    managed.write_text("MANAGED RULE", encoding="utf-8")

    bundle = build_subagent_instructions(
        workspace,
        "解释 pkg/file.py",
        base_prompt="BASE RULE",
        home=home,
        managed_paths=(managed,),
        definition=AgentDefinition(
            name="reviewer",
            description="review",
            system_prompt="DEFINITION RULE",
            source="project",
        ),
    )
    fragments = bundle.fragments()

    by_layer = {fragment.layer: fragment for fragment in fragments}
    l3_content = "\n".join(
        fragment.content for fragment in fragments
        if fragment.layer == LAYER_L3_PROJECT_RULES
    )
    assert "MANAGED RULE" in by_layer[LAYER_L0_MANAGED_POLICY].content
    assert by_layer[LAYER_L0_MANAGED_POLICY].id == "managed:rules"
    assert by_layer[LAYER_L1_BASE_AGENT].content == "BASE RULE"
    assert "USER RULE" in by_layer[LAYER_L2_USER_GLOBAL].content
    assert "PROJECT RULE" in l3_content
    assert "DIRECTORY RULE" in l3_content
    assert by_layer[LAYER_L2_USER_GLOBAL].id == "user:rules"
    assert by_layer[LAYER_L4_AGENT_STYLE].id == "agent_definition:reviewer"
    assert "DEFINITION RULE" in by_layer[LAYER_L4_AGENT_STYLE].content
    l3 = [
        fragment for fragment in fragments
        if fragment.layer == LAYER_L3_PROJECT_RULES
    ]
    assert l3[0].id == "project:rules"
    assert l3[0].source == "project:rules"
    assert l3[1].id == "directory:rules"
    assert l3[1].source == "directory:rules"
    assert all(fragment.cacheable for fragment in fragments)
    assert len({fragment.id for fragment in fragments}) == len(fragments)


def test_subagent_uses_prompt_assembler_and_l7_inherited_memory(tmp_path):
    async def go():
        (tmp_path / "AGENTS.md").write_text("PROJECT RULE", encoding="utf-8")
        memory_store = MemoryStore(
            tmp_path,
            path=tmp_path / "memory.jsonl",
            user_path=tmp_path / "user-memory.jsonl",
        )
        memory = memory_store.add(
            "prompt architecture memory",
            kind=MemoryKind.KNOWLEDGE,
        )
        catalog = skills_fragment(
            "<available_skills>\n- test-skill: test catalog\n"
            "</available_skills>")
        assert catalog is not None
        manager = SubagentManager(
            llm=EchoProvider(),
            tool_factory=lambda: [ReadFileTool()],
            tool_context=ToolContext(workspace_root=tmp_path),
            policy=AllowAllPolicy(),
            bus=EventBus(),
            memory_store=memory_store,
            catalog_fragments_provider=lambda: (catalog,),
        )

        handle = await manager.spawn("explain prompt architecture")
        handle = await manager.wait("a1", timeout=5)
        assert handle.status == "completed"

        plan = handle.loop.last_prompt_plan
        assert plan is not None
        fragments = {fragment.id: fragment for fragment in plan.fragments}
        assert "subagent:base:0" in fragments
        assert "project:rules" in fragments
        assert "legacy:system" not in fragments
        tool_directory = next(
            fragment for fragment in plan.fragments
            if fragment.source == "tool_directory"
        )
        assert tool_directory.layer == LAYER_L5_MODE_CONTRACT
        memory_fragment = fragments["subagent:inherited-memory"]
        assert memory_fragment.layer == LAYER_L7_EVIDENCE
        assert memory_fragment.source == "inherited_memory"
        assert memory_fragment.stability == "turn"
        assert memory_fragment.trust == "untrusted"
        assert memory_fragment.cacheable is False
        assert memory_fragment.metadata["source_role"] == "evidence"
        assert memory_fragment.metadata["evidence_type"] == "memory"
        assert memory_fragment.metadata["instruction"] is False
        assert memory_fragment.metadata["requires_validation"] is True
        catalog_fragment = fragments["skills:index"]
        assert catalog_fragment.layer == LAYER_L6_CATALOG
        assert catalog_fragment.source == "skills"

        assert "subagent:base:0" in plan.cache.stable_fragments
        assert "project:rules" in plan.cache.stable_fragments
        assert "subagent:inherited-memory" not in plan.cache.stable_fragments
        assert plan.cache.enabled
        assert plan.stable_hash == plan.cache.stable_hash
        assert plan.dynamic_hash == plan.cache.dynamic_hash
        assert plan.stable_hash
        assert plan.dynamic_hash
        assert not any(
            fragment.id.startswith("source:runtime-hook")
            for fragment in plan.fragments
        )

        provider_messages = handle.loop.prompt_assembler.provider_messages(plan)
        assert provider_messages.stable_hash == plan.stable_hash
        assert provider_messages.dynamic_hash == plan.dynamic_hash
        assert provider_messages.full_hash
        system_messages = [
            message for message in provider_messages.messages
            if message.role == "system"
        ]
        assert len(system_messages) == 1
        assert "[Evidence: Memory]" in system_messages[0].content

        prompt_snapshot = handle.loop.last_context_plan.prompt_plan
        assert prompt_snapshot is not None
        assert any(hit.id == memory.id for hit in prompt_snapshot.memory_hits)
        why = "\n".join(ContextInspectionService(handle.loop).why_lines(memory.id))
        assert "role=evidence" in why
        assert "instruction=false" in why
        assert "subagent:inherited-memory" in why
        await manager.shutdown()

    asyncio.run(go())
