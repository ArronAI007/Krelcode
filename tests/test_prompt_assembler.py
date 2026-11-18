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
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.mock import MockProvider
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
    assert payload["system"][1]["text"].startswith("\n\n")


def test_legacy_fragment_constructor_remains_compatible():
    fragment = PromptFragment(
        id="legacy", source="legacy", title="Legacy", content="TEXT")

    assert fragment.layer == LAYER_L1_BASE_AGENT
    assert fragment.stability == "session"
    assert fragment.trust == TRUST_MANAGED


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
    assert provider.calls[0][0].content == loop.last_prompt_plan.system_text
