"""P0: one PromptAssembler from fragments to ProviderMessages."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import (
    CATEGORY_MEMORY,
    ContextLayer,
    ContextRequest,
)
from krelcode.agent.fragments import (
    LAYER_L0_MANAGED_POLICY,
    LAYER_L1_BASE_AGENT,
    LAYER_L3_PROJECT_RULES,
    LAYER_L4_AGENT_STYLE,
    LAYER_L6_CATALOG,
    LAYER_L7_MEMORY,
    LAYER_L8_ENVIRONMENT,
    LAYER_L10_HISTORY,
    LAYER_L11_USER_REQUEST,
    LAYER_L12_REMINDER,
    PROMPT_LAYERS,
    STABILITY_STATIC,
    STABILITY_TURN,
    TRUST_MANAGED,
    TRUST_PROJECT,
    TRUST_UNTRUSTED,
    PromptFragment,
    PromptFragmentRegistry,
    base_fragment,
)
from krelcode.agent.context_usage import build_context_usage_snapshot
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.core.messages import Message, ToolCall
from krelcode.llm import (
    EvidenceBlock,
    EvidenceChannel,
    SystemPromptBlock,
    SystemPromptBlocks,
    explicit_evidence_blocks,
)
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.gemini import GeminiProvider
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.providers.openai import OpenAIProvider, to_openai_messages
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


class StaticSource:
    name = "static_memory"

    def __init__(self, layers=()):
        self.layers = tuple(layers)

    def collect(self, request):
        return self.layers


def request(tmp_path: Path, history=(), **kwargs) -> ContextRequest:
    return ContextRequest(
        turn_id="turn-p0",
        step=1,
        history=tuple(history),
        workspace_root=tmp_path,
        **kwargs,
    )


def assemble(
    tmp_path: Path,
    registry: PromptFragmentRegistry,
    *,
    history=(),
    layers=(),
    reminders=(),
):
    assembler = PromptAssembler(
        ContextAssembler([StaticSource(layers)]),
        registry_provider=lambda: registry,
    )
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=request(tmp_path, history=history),
        prompt_snapshot=registry.snapshot(),
        per_turn_reminders=tuple(reminders),
    )))
    return assembler, plan


def test_fragments_are_ordered_by_fixed_layers_even_when_registered_chaotically(
    tmp_path,
):
    registry = PromptFragmentRegistry([
        PromptFragment(
            id="style", source="style", title="Style", content="STYLE",
            layer=LAYER_L4_AGENT_STYLE, cacheable=True,
            stability=STABILITY_STATIC),
        PromptFragment(
            id="project", source="project", title="Project", content="PROJECT",
            layer=LAYER_L3_PROJECT_RULES, cacheable=True,
            stability=STABILITY_STATIC, trust=TRUST_PROJECT),
        PromptFragment(
            id="policy", source="policy", title="Policy", content="POLICY",
            layer=LAYER_L0_MANAGED_POLICY, cacheable=True,
            stability=STABILITY_STATIC),
        base_fragment(),
        PromptFragment(
            id="catalog", source="catalog", title="Catalog", content="CATALOG",
            layer=LAYER_L6_CATALOG, cacheable=True,
            stability=STABILITY_STATIC, trust=TRUST_PROJECT),
    ])
    memory_layer = ContextLayer(
        id="memory:one", source="memory", category=CATEGORY_MEMORY,
        content="MEMORY", trust="untrusted", priority=10,
    )

    _, plan = assemble(
        tmp_path,
        registry,
        history=(Message.user("CURRENT"),),
        layers=(memory_layer,),
        reminders=("REMINDER",),
    )

    assert [layer.layer for layer in plan.layers] == [
        LAYER_L0_MANAGED_POLICY,
        LAYER_L1_BASE_AGENT,
        LAYER_L3_PROJECT_RULES,
        LAYER_L4_AGENT_STYLE,
        LAYER_L6_CATALOG,
        LAYER_L7_MEMORY,
        LAYER_L10_HISTORY,
        LAYER_L11_USER_REQUEST,
        LAYER_L12_REMINDER,
    ]
    assert [item.layer for item in plan.fragments] == [
        layer for layer in PROMPT_LAYERS
        if layer in {item.layer for item in plan.fragments}
    ]


def test_turn_and_untrusted_fragments_cannot_enter_cache_prefix():
    with pytest.raises(ValueError, match="turn fragment"):
        PromptFragment(
            id="bad:turn", source="test", title="bad", content="bad",
            cacheable=True, layer=LAYER_L7_MEMORY,
            stability=STABILITY_TURN,
        )
    with pytest.raises(ValueError, match="untrusted fragment"):
        PromptFragment(
            id="bad:trust", source="test", title="bad", content="bad",
            cacheable=True, stability=STABILITY_STATIC,
            trust=TRUST_UNTRUSTED,
        )


def test_dynamic_environment_and_reminder_never_enter_stable_prefix(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        PromptFragment(
            id="environment", source="environment", title="Environment",
            content="DATE 2026-11-18", layer=LAYER_L8_ENVIRONMENT,
            cacheable=False, stability=STABILITY_TURN,
        ),
    ])

    _, plan = assemble(
        tmp_path,
        registry,
        history=(Message.user("CURRENT"),),
        reminders=("TOKEN REMINDER",),
    )

    assert "DATE 2026-11-18" not in plan.stable_prefix
    assert "TOKEN REMINDER" not in plan.stable_prefix
    assert "DATE 2026-11-18" in plan.system_text
    assert "TOKEN REMINDER" in plan.system_text
    assert plan.cache.volatile_fragments == (
        "environment",
        "conversation:history",
        "conversation:current-user",
        "reminder:turn:1",
    )


def test_dynamic_changes_keep_stable_hash_but_change_full_hash(tmp_path):
    def registry_for(env: str) -> PromptFragmentRegistry:
        return PromptFragmentRegistry([
            base_fragment(),
            PromptFragment(
                id="environment", source="environment", title="Environment",
                content=env, layer=LAYER_L8_ENVIRONMENT,
                cacheable=False, stability=STABILITY_TURN,
            ),
        ])

    _, first = assemble(tmp_path, registry_for("BRANCH main"))
    _, second = assemble(tmp_path, registry_for("BRANCH feature"))

    assert first.cache.stable_hash == second.cache.stable_hash
    assert first.cache.dynamic_hash != second.cache.dynamic_hash
    assert first.cache.full_hash != second.cache.full_hash
    assert first.cache.stable_hash == hashlib.sha256(
        first.stable_prefix.encode("utf-8")).hexdigest()


def test_current_user_is_sent_once_and_history_is_not_mutated(tmp_path):
    registry = PromptFragmentRegistry([base_fragment()])
    history = (
        Message.user("OLDER"),
        Message.assistant("ANSWER"),
        Message.user("CURRENT"),
    )
    original = deepcopy(history)

    assembler, plan = assemble(tmp_path, registry, history=history)
    provider = assembler.provider_messages(plan, tools=({"name": "read"},))

    assert history == original
    assert [message.content for message in provider.messages].count("CURRENT") == 1
    assert provider.messages[0].role == "system"
    assert provider.messages[1:] == history
    assert provider.tools == ({"name": "read"},)


def test_tool_results_stay_out_of_system_and_stable_prefix(tmp_path):
    registry = PromptFragmentRegistry([base_fragment()])
    history = (
        Message.user("inspect"),
        Message.assistant(tool_calls=[ToolCall("t1", "read_file", {})]),
        Message.tool_result("t1", "TOOL-RESULT-SENTINEL"),
    )

    assembler, plan = assemble(tmp_path, registry, history=history)
    provider = assembler.provider_messages(plan)

    assert "TOOL-RESULT-SENTINEL" not in plan.system_text
    assert "TOOL-RESULT-SENTINEL" not in plan.stable_prefix
    assert "TOOL-RESULT-SENTINEL" not in (provider.messages[0].content or "")
    assert any(
        message.role == "tool"
        and "TOOL-RESULT-SENTINEL" in (message.content or "")
        for message in provider.messages
    )


def test_provider_messages_feed_anthropic_cache_boundary(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        PromptFragment(
            id="environment", source="environment", title="Environment",
            content="DYNAMIC", layer=LAYER_L8_ENVIRONMENT,
            cacheable=False, stability=STABILITY_TURN,
        ),
    ])
    assembler, plan = assemble(
        tmp_path,
        registry,
        history=(Message.user("CURRENT"),),
    )
    provider_messages = assembler.provider_messages(plan)
    provider = AnthropicProvider(model="claude-x", api_key="k")

    payload = json.loads(
        provider.build_request(provider_messages.messages, None)["body"])

    assert payload["system"][0]["text"] == plan.stable_prefix
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["system"][1]["text"] == "DYNAMIC"


def test_anthropic_omits_cache_control_when_no_stable_block_exists(tmp_path):
    registry = PromptFragmentRegistry([
        PromptFragment(
            id="environment", source="environment", title="Environment",
            content="DYNAMIC ONLY", layer=LAYER_L8_ENVIRONMENT,
            cacheable=False, stability=STABILITY_TURN,
        ),
    ])
    assembler, plan = assemble(tmp_path, registry)
    provider_messages = assembler.provider_messages(plan)
    provider = AnthropicProvider(model="claude-x", api_key="k")

    payload = json.loads(
        provider.build_request(provider_messages.messages, None)["body"])
    prepared = provider.prepare_request(provider_messages.messages, None)

    assert payload["system"] == [{"type": "text", "text": "DYNAMIC ONLY"}]
    assert plan.stable_system_blocks == ()
    assert len(plan.dynamic_system_blocks) == 1
    assert prepared.stable_system_block_count == 0
    assert prepared.dynamic_system_block_count == 1
    assert prepared.stable_cache_hash == ""
    assert prepared.dynamic_cache_hash == plan.cache.dynamic_hash
    assert prepared.cache_control_boundary == 0


def test_openai_legacy_system_messages_merge_in_fixed_order():
    wire = to_openai_messages([
        Message.system(
            "STATIC",
            provider_meta={"prompt_cache_boundaries": [len("STATIC")]}),
        Message.system("DYNAMIC"),
        Message.user("question"),
    ])

    system_messages = [
        message for message in wire if message["role"] == "system"]
    assert system_messages == [{
        "role": "system",
        "content": "STATIC\n\nDYNAMIC",
    }]


def test_legacy_fragment_constructor_remains_compatible():
    fragment = PromptFragment(
        id="legacy", source="legacy", title="Legacy", content="TEXT")

    assert fragment.layer == LAYER_L1_BASE_AGENT
    assert fragment.stability == "session"
    assert fragment.trust == TRUST_MANAGED


def test_public_system_prompt_block_contract_is_provider_neutral():
    blocks = SystemPromptBlocks(
        stable_system_blocks=(SystemPromptBlock(
            id="stable", text="STABLE", cacheable=True),),
        dynamic_system_blocks=(SystemPromptBlock(
            id="dynamic", text="DYNAMIC", stability="turn"),),
    )

    assert blocks.merged_text == "STABLE\n\nDYNAMIC"
    assert blocks.stable_hash == hashlib.sha256(b"STABLE").hexdigest()
    assert blocks.dynamic_hash == hashlib.sha256(b"DYNAMIC").hexdigest()
    assert blocks.cache_control_block_index == 0
    assert [block.id for block in blocks.instruction_blocks] == [
        "stable", "dynamic"]
    assert blocks.instruction_hash
    assert "STABLE" not in json.dumps(blocks.as_record(), ensure_ascii=False)

    with pytest.raises(ValueError, match="独立 EvidenceChannel"):
        SystemPromptBlocks(
            stable_system_blocks=(),
            dynamic_system_blocks=(SystemPromptBlock(
                id="evidence", text="EVIDENCE", channel="evidence",
                requires_validation=True),),
        )


def test_agent_loop_uses_prompt_assembler_as_production_entry(tmp_path):
    provider = MockProvider(scripted=["done"])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
    )

    assert asyncio.run(loop.run("hello", turn_id="turn-p0")) == "done"
    assert loop.last_prompt_plan is not None
    assert loop.last_prompt_plan.system_text.startswith("SYSTEM")
    assert loop.last_provider_messages is not None
    assert loop.last_provider_messages.messages[0].content == (
        loop.last_prompt_plan.system_text)
    assert provider.calls[0][0].content == loop.last_prompt_plan.system_text


def test_prompt_plan_exposes_stable_and_dynamic_system_blocks(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        PromptFragment(
            id="project", source="project", title="Project", content="PROJECT",
            layer=LAYER_L3_PROJECT_RULES, cacheable=True,
            stability=STABILITY_STATIC, trust=TRUST_PROJECT,
        ),
        PromptFragment(
            id="environment", source="environment", title="Environment",
            content="DYNAMIC", layer=LAYER_L8_ENVIRONMENT,
            cacheable=False, stability=STABILITY_TURN,
        ),
    ])

    assembler, plan = assemble(
        tmp_path, registry, history=(Message.user("CURRENT"),))
    provider_messages = assembler.provider_messages(plan)
    blocks = provider_messages.system_blocks

    assert blocks is not None
    assert [block.id for block in blocks.stable_system_blocks] == [
        "core:base", "project"]
    assert [block.id for block in blocks.dynamic_system_blocks] == [
        "environment"]
    assert blocks.stable_text == plan.stable_prefix
    assert blocks.stable_hash == hashlib.sha256(
        blocks.stable_text.encode("utf-8")).hexdigest()
    assert blocks.dynamic_hash == hashlib.sha256(b"DYNAMIC").hexdigest()
    assert plan.cache.dynamic_hash == blocks.dynamic_hash
    assert plan.stable_hash == blocks.stable_hash
    assert plan.dynamic_hash == blocks.dynamic_hash
    assert plan.full_hash == hashlib.sha256(
        plan.system_text.encode("utf-8")).hexdigest()
    assert provider_messages.stable_hash == plan.stable_hash
    assert provider_messages.dynamic_hash == plan.dynamic_hash
    assert provider_messages.full_hash == plan.full_hash
    assert provider_messages.messages[0].provider_meta[
        "system_prompt_blocks"] is blocks


def test_provider_messages_preserve_instruction_and_evidence_channels(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        PromptFragment(
            id="project", source="project", title="Project", content="PROJECT",
            layer=LAYER_L3_PROJECT_RULES, cacheable=True,
            stability=STABILITY_STATIC, trust=TRUST_PROJECT,
        ),
    ])
    memory = ContextLayer(
        id="memory:scoped",
        source="memory",
        category=CATEGORY_MEMORY,
        content=(
            "[Evidence: Memory]\n"
            "source: manual\n"
            "requires_validation: true\n\nMEMORY FACT"
        ),
        trust=TRUST_UNTRUSTED,
        priority=10,
        metadata={
            "source_role": "evidence",
            "evidence_type": "memory",
            "evidence_ids": ("memory-1",),
            "evidence_origin": "scoped_memory",
            "requires_validation": True,
            "confidence_min": 0.8,
            "confidence_max": 0.8,
        },
    )
    assembler, plan = assemble(
        tmp_path,
        registry,
        history=(Message.user("CURRENT"),),
        layers=(memory,),
    )
    provider_messages = assembler.provider_messages(plan)

    assert [block.id for block in provider_messages.instruction_blocks] == [
        "core:base", "project"]
    assert [block.id for block in provider_messages.evidence_blocks] == [
        "memory:scoped"]
    evidence = provider_messages.evidence_blocks[0]
    assert isinstance(evidence, EvidenceBlock)
    assert evidence.evidence_type == "memory"
    assert evidence.requires_validation is True
    assert evidence.scope == "session"
    assert evidence.origin == "scoped_memory"
    assert "MEMORY FACT" in evidence.text
    assert evidence.as_record().get("metadata") is None
    assert "MEMORY FACT" not in plan.stable_prefix
    assert "MEMORY FACT" not in plan.system_text
    assert plan.instruction_hash == provider_messages.instruction_hash
    assert plan.evidence_hash == provider_messages.evidence_hash
    assert plan.evidence_hash
    assert provider_messages.evidence_channel.anchor_message_index == 1

    system_message = provider_messages.messages[0]
    assert system_message.content == plan.system_text
    assert "MEMORY FACT" not in (system_message.content or "")
    assert explicit_evidence_blocks(provider_messages.messages) == ()
    assert "evidence_blocks" not in (provider_messages.provider_meta or {})

    provider = OpenAIProvider(model="deepseek-chat", api_key="k")
    evidence = provider_messages.evidence_channel
    prepared = provider.prepare_request(
        provider_messages.messages, None, evidence=evidence)
    payload = json.loads(provider.build_request(
        provider_messages.messages, None, evidence=evidence)["body"])
    system_wire = next(
        item for item in payload["messages"] if item["role"] == "system")
    user_wire = next(
        item for item in payload["messages"] if item["role"] == "user")
    assert system_wire["content"] == plan.system_text
    assert "MEMORY FACT" not in system_wire["content"]
    assert "MEMORY FACT" in user_wire["content"]
    assert user_wire["content"].endswith("CURRENT")
    assert prepared.instruction_system_block_count == 2
    assert prepared.evidence_block_count == 1
    assert prepared.instruction_hash == plan.instruction_hash
    assert prepared.evidence_hash == plan.evidence_hash
    snapshot = build_context_usage_snapshot(
        provider_messages.messages,
        turn_id="turn-evidence",
        step=1,
        prepared_request=prepared,
    )
    assert snapshot.evidence_block_count == 1
    assert snapshot.evidence_hash == plan.evidence_hash


def test_evidence_channel_reaches_each_provider_without_system_pollution(tmp_path):
    registry = PromptFragmentRegistry([base_fragment()])
    memory = ContextLayer(
        id="memory:scoped",
        source="memory",
        category=CATEGORY_MEMORY,
        content="[Evidence: Memory]\nrequires_validation: true\n\nMEMORY FACT",
        trust=TRUST_UNTRUSTED,
        metadata={
            "source_role": "evidence",
            "evidence_type": "memory",
            "requires_validation": True,
        },
    )
    assembler, plan = assemble(
        tmp_path,
        registry,
        history=(Message.user("CURRENT"),),
        layers=(memory,),
    )
    provider_messages = assembler.provider_messages(plan)
    evidence = provider_messages.evidence_channel

    anthropic = AnthropicProvider(model="claude-x", api_key="k")
    anthropic_payload = json.loads(anthropic.build_request(
        provider_messages.messages, None, evidence=evidence)["body"])
    assert all("MEMORY FACT" not in block["text"]
               for block in anthropic_payload["system"])
    assert anthropic_payload["messages"][0]["content"][0]["text"].startswith(
        "[Evidence: Memory]")
    assert anthropic_payload["messages"][0]["content"][-1]["text"] == "CURRENT"

    gemini = GeminiProvider(model="gemini-x", api_key="k")
    gemini_payload = json.loads(gemini.build_request(
        provider_messages.messages, None, evidence=evidence)["body"])
    assert "MEMORY FACT" not in gemini_payload["systemInstruction"]["parts"][0]["text"]
    assert gemini_payload["contents"][0]["parts"][0]["text"].startswith(
        "[Evidence: Memory]")
    assert gemini_payload["contents"][0]["parts"][-1]["text"] == "CURRENT"


def test_evidence_channel_survives_tool_result_turns_in_all_providers():
    messages = (
        Message.user("CURRENT"),
        Message.assistant(tool_calls=[ToolCall("t1", "read_file", {})]),
        Message.tool_result("t1", "TOOL RESULT"),
    )
    evidence = EvidenceChannel(
        blocks=(EvidenceBlock(
            id="memory:scoped",
            text="[Evidence: Memory]\n\nMEMORY FACT",
            evidence_type="memory",
            requires_validation=True,
        ),),
        anchor_message_index=0,
    )

    openai_wire = to_openai_messages(messages, evidence=evidence)
    assert "MEMORY FACT" in openai_wire[0]["content"]
    assert "TOOL RESULT" in json.dumps(openai_wire[-1], ensure_ascii=False)

    anthropic = AnthropicProvider(model="claude-x", api_key="k")
    anthropic_wire = json.loads(anthropic.build_request(
        messages, None, evidence=evidence)["body"])["messages"]
    assert anthropic_wire[0]["content"][0]["text"].startswith("[Evidence: Memory]")
    assert anthropic_wire[-1]["content"][0]["type"] == "tool_result"

    gemini = GeminiProvider(model="gemini-x", api_key="k")
    gemini_wire = json.loads(gemini.build_request(
        messages, None, evidence=evidence)["body"])["contents"]
    assert gemini_wire[0]["parts"][0]["text"].startswith("[Evidence: Memory]")
    assert "functionResponse" in gemini_wire[-1]["parts"][0]


def test_prompt_plan_exposes_omitted_fragment_ids(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        PromptFragment(
            id="memory:hidden", source="memory", title="Memory",
            content="MEMORY MUST NOT REACH PROVIDER",
            layer=LAYER_L7_MEMORY, stability=STABILITY_TURN,
            trust=TRUST_UNTRUSTED,
        ),
    ])

    assembler, plan = assemble(
        tmp_path, registry, history=(Message.user("CURRENT"),))
    provider_messages = assembler.provider_messages(plan)

    assert plan.omitted_fragments == ("memory:hidden",)
    assert plan.omitted_details[0].reason == "memory-evidence-separation"
    assert "MEMORY MUST NOT REACH PROVIDER" not in plan.system_text
    assert "MEMORY MUST NOT REACH PROVIDER" not in "\n".join(
        message.content or "" for message in provider_messages.messages)


def test_anthropic_preserves_each_stable_system_block_and_boundary(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        PromptFragment(
            id="project", source="project", title="Project", content="PROJECT",
            layer=LAYER_L3_PROJECT_RULES, cacheable=True,
            stability=STABILITY_STATIC, trust=TRUST_PROJECT,
        ),
        PromptFragment(
            id="environment", source="environment", title="Environment",
            content="DYNAMIC", layer=LAYER_L8_ENVIRONMENT,
            cacheable=False, stability=STABILITY_TURN,
        ),
    ])
    assembler, plan = assemble(
        tmp_path, registry, history=(Message.user("CURRENT"),))
    provider_messages = assembler.provider_messages(plan)
    provider = AnthropicProvider(model="claude-x", api_key="k")

    request = provider.build_request(provider_messages.messages, None)
    payload = json.loads(request["body"])
    prepared = provider.prepare_request(provider_messages.messages, None)

    assert [block["text"] for block in payload["system"]] == [
        plan.stable_system_blocks[0].text,
        plan.stable_system_blocks[1].text,
        plan.dynamic_system_blocks[0].text,
    ]
    assert "cache_control" not in payload["system"][0]
    assert payload["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["system"][2]
    assert prepared.stable_system_block_count == 2
    assert prepared.dynamic_system_block_count == 1
    assert prepared.stable_cache_hash == plan.cache.stable_hash
    assert prepared.dynamic_cache_hash == plan.cache.dynamic_hash
    assert prepared.cache_control_boundary == len(plan.stable_prefix)


def test_openai_merges_blocks_in_fixed_order_but_keeps_hashes(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        PromptFragment(
            id="project", source="project", title="Project", content="PROJECT",
            layer=LAYER_L3_PROJECT_RULES, cacheable=True,
            stability=STABILITY_STATIC, trust=TRUST_PROJECT,
        ),
        PromptFragment(
            id="environment", source="environment", title="Environment",
            content="DYNAMIC", layer=LAYER_L8_ENVIRONMENT,
            cacheable=False, stability=STABILITY_TURN,
        ),
    ])
    assembler, plan = assemble(
        tmp_path, registry, history=(Message.user("CURRENT"),))
    provider_messages = assembler.provider_messages(plan)
    provider = OpenAIProvider(model="deepseek-chat", api_key="k")

    request = provider.build_request(provider_messages.messages, None)
    payload = json.loads(request["body"])
    prepared = provider.prepare_request(provider_messages.messages, None)
    snapshot = build_context_usage_snapshot(
        provider_messages.messages,
        turn_id="turn-p0-blocks",
        step=1,
        prepared_request=prepared,
    )

    system_messages = [
        message for message in payload["messages"] if message["role"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"] == plan.system_text
    assert plan.system_text == plan.system_blocks.merged_text
    assert prepared.stable_system_block_count == 2
    assert prepared.dynamic_system_block_count == 1
    assert prepared.stable_cache_hash == plan.cache.stable_hash
    assert prepared.dynamic_cache_hash == plan.cache.dynamic_hash
    assert snapshot.stable_hash == plan.cache.stable_hash
    assert snapshot.dynamic_hash == plan.cache.dynamic_hash
    assert snapshot.stable_system_block_count == 2
    assert snapshot.dynamic_system_block_count == 1
    assert snapshot.cache_control_boundary == len(plan.stable_prefix)
    assert "PROJECT" not in json.dumps(snapshot.as_record(), ensure_ascii=False)
    assert "PROJECT" not in json.dumps(
        plan.system_blocks.as_record(), ensure_ascii=False)
