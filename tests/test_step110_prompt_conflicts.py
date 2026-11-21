"""Step 110: deterministic Prompt conflict analysis and diagnostics."""

from __future__ import annotations

import asyncio
import json

from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_plan import ContextPlanSnapshot
from krelcode.agent.context_source import ContextRequest
from krelcode.agent.fragments import (
    LAYER_L0_MANAGED_POLICY,
    LAYER_L2_USER_GLOBAL,
    LAYER_L3_PROJECT_RULES,
    STABILITY_SESSION,
    TRUST_MANAGED,
    TRUST_PROJECT,
    TRUST_USER,
    PromptFragment,
    PromptFragmentRegistry,
    base_fragments,
)
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.agent.prompt_conflicts import (
    STATUS_AMBIGUOUS,
    STATUS_RESOLVED,
    PromptConflictAnalyzer,
    PromptConflictReport,
)
from krelcode.agent.prompt_plan import PromptPlanSnapshot
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.controller import SessionController
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def _fragment(
    fragment_id: str,
    content: str,
    *,
    scope: str = "project",
    precedence: int = 20,
    layer: str = LAYER_L3_PROJECT_RULES,
    trust: str = TRUST_PROJECT,
) -> PromptFragment:
    return PromptFragment(
        id=fragment_id,
        source=fragment_id,
        title=fragment_id,
        content=content,
        cacheable=True,
        compactable=False,
        toggleable=False,
        metadata={
            "instruction": True,
            "source_role": "instruction",
            "rule_scope": scope,
            "rule_precedence": precedence,
        },
        scope=scope,
        layer=layer,
        stability=STABILITY_SESSION,
        trust=trust,
    )


def _rules_registry() -> PromptFragmentRegistry:
    return PromptFragmentRegistry([
        _fragment(
            "user:rules",
            "Use pytest -q for tests.",
            scope="user",
            precedence=10,
            layer=LAYER_L2_USER_GLOBAL,
            trust=TRUST_USER,
        ),
        _fragment(
            "project:rules",
            "Do not use pytest -q for tests.",
        ),
    ])


def _plan_with_conflicts(tmp_path):
    registry = _rules_registry()
    assembler = PromptAssembler(
        ContextAssembler([]),
        registry_provider=lambda: registry,
    )
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=ContextRequest(
            turn_id="turn-step110",
            step=1,
            history=(Message.user("run the test"),),
            workspace_root=tmp_path,
        ),
        prompt_snapshot=registry.snapshot(),
    )))
    return assembler, plan


def _context_plan(plan) -> ContextPlanSnapshot:
    prompt_snapshot = PromptPlanSnapshot.from_prompt_plan(
        plan,
        turn_id="turn-step110",
        step=1,
        history_digest="history",
    )
    return ContextPlanSnapshot(
        turn_id="turn-step110",
        step=1,
        prompt_plan=prompt_snapshot,
    )


def _loop_with_plan(tmp_path, plan) -> AgentLoop:
    loop = AgentLoop(
        llm=MockProvider(scripted=["ok"]),
        registry=ToolRegistry([]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
    )
    loop.last_context_plan = _context_plan(plan)
    return loop


def test_direct_negation_resolves_project_over_user_without_prompt_body(tmp_path):
    _, plan = _plan_with_conflicts(tmp_path)
    report = plan.conflict_report

    assert report is not None
    assert report.conflict_count == 1
    conflict = report.conflicts[0]
    assert conflict.code == "direct_negation"
    assert conflict.status == STATUS_RESOLVED
    assert conflict.winner_fragment_id == "project:rules"
    assert conflict.resolution == "layer_precedence"
    assert conflict.topic_hash and conflict.left_directive_hash
    serialized = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "pytest -q" not in serialized
    assert "Do not use" not in serialized


def test_managed_policy_wins_and_equal_authority_is_ambiguous():
    managed = _fragment(
        "managed:rules",
        "Never run rm -rf.",
        scope="managed",
        precedence=0,
        layer=LAYER_L0_MANAGED_POLICY,
        trust=TRUST_MANAGED,
    )
    project = _fragment("project:rules", "Always run rm -rf.")
    first = _fragment("project:a", "Run tests before commit.")
    second = _fragment("project:b", "Do not run tests before commit.")

    managed_report = PromptConflictAnalyzer().analyze((managed, project))
    ambiguous_report = PromptConflictAnalyzer().analyze((first, second))

    assert managed_report.conflicts[0].winner_fragment_id == "managed:rules"
    assert managed_report.conflicts[0].resolution == "managed_policy"
    assert ambiguous_report.conflicts[0].status == STATUS_AMBIGUOUS
    assert ambiguous_report.unresolved_count == 1
    assert ambiguous_report.conflicts[0].winner_fragment_id == ""


def test_directory_rule_is_more_specific_than_project_rule():
    project = _fragment("project:rules", "Use spaces for indentation.")
    directory = _fragment(
        "project:src:rules",
        "Use tabs for indentation.",
        scope="directory",
    )

    report = PromptConflictAnalyzer().analyze((project, directory))

    assert report.conflict_count == 1
    assert report.conflicts[0].winner_fragment_id == "project:src:rules"
    assert report.conflicts[0].resolution == "authority_precedence"


def test_opposing_values_are_detected_without_topic_false_positive():
    tabs = _fragment("project:tabs", "Use tabs for indentation.")
    spaces = _fragment("project:spaces", "Use spaces for indentation.")
    unrelated = _fragment("project:docs", "Check documentation before release.")

    report = PromptConflictAnalyzer().analyze((tabs, spaces, unrelated))

    assert report.conflict_count == 1
    assert report.conflicts[0].code == "opposing_value"
    assert report.conflicts[0].status == STATUS_AMBIGUOUS


def test_production_base_prompt_is_coherent_without_conflict_noise():
    report = PromptConflictAnalyzer().analyze(base_fragments())

    assert report.analyzed_fragment_count == len(base_fragments())
    assert report.directive_count > 0
    assert report.conflict_count == 0


def test_chinese_sentences_and_opposing_values_are_supported():
    directives = PromptFragmentRegistry([
        _fragment("project:chinese-tabs", "使用 tabs 缩进。不要使用空格。"),
        _fragment("project:chinese-spaces", "不要使用 tabs 缩进。使用空格。"),
        _fragment("project:chinese-space-only", "使用空格缩进。"),
    ]).snapshot().fragments

    report = PromptConflictAnalyzer().analyze(directives)

    assert report.directive_count == 5
    assert report.conflict_count >= 2
    assert any(item.code == "opposing_value" for item in report.conflicts)
    assert any(item.code == "direct_negation" for item in report.conflicts)


def test_code_fences_are_ignored_and_chinese_aliases_match():
    with_code = _fragment(
        "project:with-code",
        "使用制表符缩进。\n```text\nDo not use tabs.\n```\n",
    )
    spaces = _fragment("project:spaces", "使用空格缩进。")

    report = PromptConflictAnalyzer().analyze((with_code, spaces))

    assert report.directive_count == 2
    assert report.conflict_count == 1
    assert report.conflicts[0].code == "opposing_value"


def test_candidate_index_is_bounded_and_marks_truncation():
    fragments = (
        _fragment("project:a", "Use shared-topic for this rule."),
        _fragment("project:b", "Always use shared-topic for this rule."),
        _fragment("project:c", "Do not use shared-topic for this rule."),
    )

    report = PromptConflictAnalyzer(max_comparisons=1).analyze(fragments)

    assert report.truncated is True


def test_report_deserialization_is_bounded_and_recomputes_counts():
    raw = {
        "version": "1",
        "fragment_count": 2,
        "analyzed_fragment_count": 2,
        "directive_count": 2,
        "conflict_count": 999,
        "unresolved_count": 999,
        "conflicts": [
            {
                "id": f"conflict-{index}",
                "code": "direct_negation",
                "severity": "error",
                "status": STATUS_AMBIGUOUS,
                "resolution": "equal_authority",
            }
            for index in range(501)
        ],
    }

    report = PromptConflictReport.from_dict(raw)

    assert report.conflict_count == 500
    assert report.unresolved_count == 500
    assert report.truncated is True
    assert len(report.conflicts) < len(raw["conflicts"])


def test_prompt_plan_snapshot_and_provider_channel_keep_conflicts_content_free(
    tmp_path,
):
    assembler, plan = _plan_with_conflicts(tmp_path)
    provider_messages = assembler.provider_messages(plan)
    snapshot = PromptPlanSnapshot.from_prompt_plan(
        plan,
        turn_id="turn-step110",
        step=1,
        history_digest="history",
    )
    restored = PromptPlanSnapshot.from_dict(snapshot.as_dict())
    channel = provider_messages.channel_record()["conflict_report"]

    assert snapshot.conflict_count == 1
    assert snapshot.unresolved_conflict_count == 0
    assert snapshot.conflict_analyzed_fragment_count == 2
    assert snapshot.conflict_directive_count == 2
    assert restored.conflicts == snapshot.conflicts
    assert restored.conflict_hash == snapshot.conflict_hash
    assert channel["conflict_count"] == 1
    assert "pytest -q" not in json.dumps(channel, ensure_ascii=False)

    corrupt = snapshot.as_dict()
    corrupt["conflict_count"] = 999
    corrupt["unresolved_conflict_count"] = 999
    corrupt["conflicts"][0]["status"] = STATUS_AMBIGUOUS
    normalized = PromptPlanSnapshot.from_dict(corrupt)
    assert normalized.conflict_count == 1
    assert normalized.unresolved_conflict_count == 1


def test_conflict_diagnostics_are_filterable_and_available_from_context_command(
    tmp_path,
):
    _, plan = _plan_with_conflicts(tmp_path)
    loop = _loop_with_plan(tmp_path, plan)
    controller = SessionController(loop, workspace_root=tmp_path)
    conflict_id = loop.last_context_plan.prompt_plan.conflicts[0]["id"]

    listing = "\n".join(asyncio.run(
        controller.execute_command("/context conflicts")).output)
    why = "\n".join(asyncio.run(
        controller.execute_command(f"/context why conflict:{conflict_id}")).output)
    related = "\n".join(asyncio.run(
        controller.execute_command("/context why project:rules")).output)
    plan_output = "\n".join(asyncio.run(
        controller.execute_command("/context plan")).output)

    assert "[conflict]" in listing
    assert "winner=project:rules" in listing
    assert "confidence=" in listing
    assert "[conflict]" in why
    assert "[conflict]" in related
    assert "prompt conflicts: 1" in plan_output
    assert "pytest -q" not in listing + why + related + plan_output


def test_context_command_metadata_lists_conflicts():
    spec = builtin_command_registry().resolve("context")

    assert spec is not None
    assert "conflicts" in spec.argument_hint
    assert "/context conflicts" in spec.help


def test_agent_loop_persists_conflict_report_and_hash_is_deterministic(tmp_path):
    registry = _rules_registry()
    first = PromptConflictAnalyzer().analyze(registry.snapshot().fragments)
    second = PromptConflictAnalyzer().analyze(registry.snapshot().fragments)

    loop = AgentLoop(
        llm=MockProvider(scripted=["ok"]),
        registry=ToolRegistry([]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
    )
    loop.prompt_registry = registry
    loop.prompt_snapshot = registry.snapshot()

    assert asyncio.run(loop.run("run the test", turn_id="step110-loop")) == "ok"
    snapshot = loop.last_context_plan.prompt_plan

    assert first.report_hash == second.report_hash
    assert snapshot.conflict_count == 1
    assert snapshot.conflicts[0]["winner_fragment_id"] == "project:rules"
    actual = PromptConflictAnalyzer().analyze(loop.last_prompt_plan.fragments)
    assert snapshot.conflict_hash == actual.report_hash
