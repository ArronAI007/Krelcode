"""Step 124: managed cyber-risk safety floor across prompt surfaces."""

from __future__ import annotations

import asyncio

from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import ContextRequest
from krelcode.agent.cyber_risk import (
    CYBER_RISK_FRAGMENT_ID,
    CYBER_RISK_PROMPT,
    MAX_CYBER_RISK_PROMPT_CHARS,
    cyber_risk_fragment,
    prepend_cyber_risk_prompt,
)
from krelcode.agent.fragments import (
    LAYER_L0_MANAGED_POLICY,
    STABILITY_STATIC,
    TRUST_MANAGED,
    PromptFragmentRegistry,
    base_fragment,
)
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.core.messages import Message
from krelcode.subagent.instructions import build_subagent_instructions


def make_context_request(tmp_path) -> ContextRequest:
    return ContextRequest(
        turn_id="turn-cyber-risk",
        step=1,
        history=(Message.user("Review auth middleware for vulnerabilities"),),
        workspace_root=tmp_path,
    )


def test_cyber_risk_is_an_immutable_bounded_l0_managed_fragment():
    fragment = cyber_risk_fragment()

    assert fragment.id == CYBER_RISK_FRAGMENT_ID
    assert fragment.source == "managed:cyber-risk"
    assert fragment.layer == LAYER_L0_MANAGED_POLICY
    assert fragment.stability == STABILITY_STATIC
    assert fragment.trust == TRUST_MANAGED
    assert fragment.cacheable is True
    assert fragment.compactable is False
    assert fragment.toggleable is False
    assert fragment.metadata["policy"] == "cyber_risk"
    assert fragment.metadata["instruction_source"] == "managed"
    assert len(CYBER_RISK_PROMPT) <= MAX_CYBER_RISK_PROMPT_CHARS


def test_cyber_risk_prompt_balances_defensive_work_and_harmful_actions():
    normalized = " ".join(CYBER_RISK_PROMPT.split())
    for allowed in (
        "Review, design, and implement secure software",
        "remediate vulnerabilities",
        "defensive controls",
        "isolated local sandbox",
        "CTFs, labs, and authorized penetration tests",
        "do not refuse ordinary secure development",
    ):
        assert allowed in normalized
    for prohibited in (
        "malware, ransomware",
        "credential or session-token theft",
        "unauthorized access",
        "destructive or disruptive attacks",
        "defense evasion and anti-forensics",
        "supply-chain compromise",
        "partial payload or operational bridge",
    ):
        assert prohibited in normalized


def test_prompt_assembler_places_cyber_risk_first_in_stable_instructions(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        cyber_risk_fragment(),
    ])
    assembler = PromptAssembler(
        ContextAssembler([]),
        registry_provider=lambda: registry,
    )

    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=make_context_request(tmp_path),
        prompt_snapshot=registry.snapshot(),
    )))

    assert plan.fragments[0].id == CYBER_RISK_FRAGMENT_ID
    assert plan.stable_system_blocks[0].id == CYBER_RISK_FRAGMENT_ID
    assert plan.stable_system_blocks[0].text.startswith(
        '<cyber_risk_contract version="1"')
    assert 'authority="managed"' in plan.stable_system_blocks[0].text


def test_subagents_always_receive_the_l0_cyber_risk_floor(tmp_path):
    bundle = build_subagent_instructions(
        tmp_path,
        "Review a proposed security fix without modifying files",
        base_prompt="SUBAGENT BASE",
        home=tmp_path / "home",
    )

    fragments = bundle.fragments()
    assert fragments[0].id == CYBER_RISK_FRAGMENT_ID
    assert fragments[0].layer == LAYER_L0_MANAGED_POLICY
    assert all(item.toggleable is False for item in fragments[:1])


def test_legacy_string_prompt_paths_prepend_cyber_risk_idempotently():
    prompt = "Review only read-only code."
    guarded = prepend_cyber_risk_prompt(prompt)

    assert guarded.startswith('<cyber_risk_contract version="1"')
    assert guarded.endswith(prompt)
    assert prepend_cyber_risk_prompt(guarded) == guarded


def test_primary_agent_and_review_surfaces_install_cyber_risk(tmp_path):
    from krelcode.app import build_loop, build_parser
    from krelcode.review import REVIEW_SYSTEM_PROMPT

    args = build_parser().parse_args([
        "--mock",
        "--workspace", str(tmp_path),
        "--no-context",
        "--no-session",
        "--no-checkpoint",
    ])
    loop, manager = asyncio.run(build_loop(args, {}, log=lambda _line: None))
    try:
        fragment = loop.prompt_registry.get(CYBER_RISK_FRAGMENT_ID)
        assert fragment is not None
        assert fragment.layer == LAYER_L0_MANAGED_POLICY
        assert REVIEW_SYSTEM_PROMPT.startswith(
            '<cyber_risk_contract version="1"')
    finally:
        asyncio.run(manager.shutdown())
