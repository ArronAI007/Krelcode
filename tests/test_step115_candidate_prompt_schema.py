"""Step 115: closed Candidate Prompt Schema and prompt_lab integration."""

from __future__ import annotations

import asyncio
import json

import pytest

from krelcode.agent.fragments import (
    LAYER_L1_BASE_AGENT,
    LAYER_L6_CATALOG,
    STABILITY_SESSION,
    STABILITY_STATIC,
    PromptFragmentRegistry,
    base_fragments,
)
from krelcode.agent.prompt_candidate import (
    PROMPT_CANDIDATE_SCHEMA,
    PromptCandidate,
    PromptCandidateError,
    PromptCandidateRisk,
)
from krelcode.agent.prompt_lab import PromptLabService
from krelcode.tools.base import ToolContext
from krelcode.agent.prompt_lab_tool import PromptLabTool
from krelcode.tools.prompt import ToolPromptSpec
from krelcode.tools.schema import validate_tool_arguments


_CANDIDATE_KEYS = {
    "target_fragment",
    "objective",
    "proposed_prompt",
    "constraints",
    "expected_behavior",
    "risk",
    "evaluation_cases",
    "rollback_plan",
}


def _cases() -> list[dict[str, object]]:
    return [
        {
            "id": "success",
            "kind": "success",
            "metric": "assert tool call count <= 2",
            "machine_verifiable": True,
        },
        {
            "id": "regression",
            "kind": "regression",
            "metric": "assert exit_code == 0 for focused tests",
            "machine_verifiable": True,
        },
        {
            "id": "security",
            "kind": "security",
            "metric": "assert output contains no credential-shaped text",
            "machine_verifiable": True,
        },
    ]


def _candidate(
    *,
    target_fragment: str = "core:base:tool-discipline",
    proposed_prompt: str = "Read only the files required by the current request.",
) -> dict[str, object]:
    return {
        "target_fragment": target_fragment,
        "objective": "Reduce unrelated file reads.",
        "proposed_prompt": proposed_prompt,
        "constraints": ["Do not expand tool authority."],
        "expected_behavior": ["Read fewer unrelated files."],
        "risk": "low",
        "evaluation_cases": _cases(),
        "rollback_plan": "Discard the unapplied candidate review.",
    }


def _registry() -> PromptFragmentRegistry:
    return PromptFragmentRegistry(base_fragments())


def _legacy_spec() -> dict[str, object]:
    return {
        "objective": "Require every evidence claim to cite a repository path.",
        "target_fragment_id": "project:review-prompt",
        "target_layer": LAYER_L6_CATALOG,
        "items": ({
            "id": "project:review-prompt",
            "role": "instruction",
            "source": "project",
            "content": "Always cite the exact file path for evidence claims.",
            "scope": "project",
            "layer": LAYER_L6_CATALOG,
            "stability": STABILITY_SESSION,
            "cacheable": True,
        },),
        "permission_mode": "default",
        "tool_scope": "none",
        "validation_cases": _cases(),
        "success_metrics": ("assert tool call count <= 2",),
        "non_goals": ("Do not change tool permissions.",),
    }


def test_candidate_schema_is_closed_and_exposes_all_eight_fields():
    assert set(PROMPT_CANDIDATE_SCHEMA["properties"]) == _CANDIDATE_KEYS
    assert set(PROMPT_CANDIDATE_SCHEMA["required"]) == _CANDIDATE_KEYS
    assert PROMPT_CANDIDATE_SCHEMA["additionalProperties"] is False
    assert PROMPT_CANDIDATE_SCHEMA["properties"]["risk"]["enum"] == [
        "low", "medium", "high"]
    case_schema = PROMPT_CANDIDATE_SCHEMA["properties"]["evaluation_cases"][
        "items"]
    assert set(case_schema["required"]) == {"id", "kind", "metric"}
    assert case_schema["additionalProperties"] is False


def test_candidate_parses_and_round_trips_the_canonical_schema():
    candidate = PromptCandidate.from_mapping(_candidate())

    assert candidate.risk is PromptCandidateRisk.LOW
    assert candidate.as_record() == _candidate()
    assert candidate.content_light_record()["contains_prompt_content"] is False


@pytest.mark.parametrize("field", sorted(_CANDIDATE_KEYS))
def test_candidate_rejects_every_missing_required_field(field):
    payload = _candidate()
    payload.pop(field)

    with pytest.raises(PromptCandidateError, match="缺少字段"):
        PromptCandidate.from_mapping(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_fragment", ""),
        ("objective", "   "),
        ("proposed_prompt", ""),
        ("rollback_plan", ""),
    ],
)
def test_candidate_rejects_empty_required_text(field, value):
    payload = _candidate()
    payload[field] = value

    with pytest.raises(PromptCandidateError, match="不能为空"):
        PromptCandidate.from_mapping(payload)


@pytest.mark.parametrize("risk", ["critical", "", None, 3])
def test_candidate_risk_is_a_closed_low_medium_high_enum(risk):
    payload = _candidate()
    payload["risk"] = risk

    with pytest.raises(PromptCandidateError, match="risk"):
        PromptCandidate.from_mapping(payload)


@pytest.mark.parametrize("field", ["constraints", "expected_behavior"])
@pytest.mark.parametrize("value", ["text", [1], [""]])
def test_candidate_text_arrays_fail_closed(field, value):
    payload = _candidate()
    payload[field] = value

    with pytest.raises(PromptCandidateError):
        PromptCandidate.from_mapping(payload)


def test_candidate_rejects_unknown_top_level_and_case_fields():
    payload = _candidate()
    payload["extra"] = True
    with pytest.raises(PromptCandidateError, match="未知字段"):
        PromptCandidate.from_mapping(payload)

    payload = _candidate()
    payload["evaluation_cases"][0]["judge"] = "model"
    with pytest.raises(PromptCandidateError, match="未知字段"):
        PromptCandidate.from_mapping(payload)


def test_candidate_bounds_large_text_arrays_and_case_counts():
    oversized_prompt = _candidate()
    oversized_prompt["proposed_prompt"] = "x" * 40_001
    with pytest.raises(PromptCandidateError, match="上限"):
        PromptCandidate.from_mapping(oversized_prompt)

    oversized_list = _candidate()
    oversized_list["constraints"] = ["x"] * 51
    with pytest.raises(PromptCandidateError, match="最多允许"):
        PromptCandidate.from_mapping(oversized_list)

    oversized_cases = _candidate()
    oversized_cases["evaluation_cases"] = _cases() * 34
    with pytest.raises(PromptCandidateError, match="最多允许"):
        PromptCandidate.from_mapping(oversized_cases)


def test_candidate_hash_is_deterministic_and_content_sensitive():
    first = PromptCandidate.from_mapping(_candidate())
    second = PromptCandidate.from_mapping(_candidate())
    changed = PromptCandidate.from_mapping(_candidate(
        proposed_prompt="Read every file before answering."))
    metadata = json.dumps(first.content_light_record(), ensure_ascii=False)

    assert first.candidate_hash == second.candidate_hash
    assert first.candidate_hash != changed.candidate_hash
    assert first.proposed_prompt not in metadata
    assert first.evaluation_cases[0].metric not in metadata
    assert len(first.candidate_hash) == 64


def test_existing_target_fragment_metadata_is_inherited():
    registry = _registry()
    target = registry.get("core:base:tool-discipline")
    assert target is not None
    candidate = PromptCandidate.from_mapping(_candidate())

    request = candidate.to_authoring_request(target)
    item = request.items[0]

    assert request.target_layer == LAYER_L1_BASE_AGENT
    assert item.id == "core:base:tool-discipline"
    assert item.source == "managed"
    assert item.scope == target.scope
    assert item.layer == target.layer
    assert item.stability == STABILITY_STATIC
    assert item.cacheable is True
    assert request.non_goals == candidate.constraints
    assert request.success_metrics == tuple(
        case.metric for case in candidate.evaluation_cases
        if case.kind == "success")


def test_new_target_uses_conservative_project_l6_defaults():
    candidate = PromptCandidate.from_mapping(_candidate(
        target_fragment="project:new-policy"))

    request = candidate.to_authoring_request()

    assert request.target_layer == LAYER_L6_CATALOG
    assert request.items[0].source == "project"
    assert request.items[0].scope == "project"
    assert request.items[0].stability == STABILITY_SESSION
    assert request.items[0].cacheable is True


def test_prompt_lab_generate_returns_canonical_candidate_without_mutation():
    registry = _registry()
    before = registry.generation
    service = PromptLabService(lambda: registry)

    result = service.generate(_candidate())

    assert set(result["candidate"]) == _CANDIDATE_KEYS
    assert result["candidate"] == _candidate()
    assert result["evaluation"]["passed"] is True
    assert result["applied"] is False
    assert result["contains_prompt_content"] is True
    assert len(result["candidate_hash"]) == 64
    assert registry.generation == before
    assert registry.get("project:new-policy") is None


def test_prompt_lab_generate_accepts_legacy_authoring_input():
    service = PromptLabService(_registry)

    result = service.generate(_legacy_spec())

    assert set(result["candidate"]) == _CANDIDATE_KEYS
    assert result["candidate"]["target_fragment"] == "project:review-prompt"
    assert result["candidate"]["proposed_prompt"].startswith("[Objective]")
    assert result["evaluation"]["passed"] is True


def test_prompt_lab_evaluate_accepts_canonical_candidate():
    service = PromptLabService(_registry)

    result = service.evaluate(_candidate())

    assert result["action"] == "evaluate"
    assert result["report"]["passed"] is True
    assert result["candidate_hash"].__len__() == 64
    assert result["applied"] is False
    assert result["contains_prompt_content"] is False


def test_empty_evaluation_cases_are_structurally_valid_but_evaluation_fails():
    payload = _candidate()
    payload["evaluation_cases"] = []
    candidate = PromptCandidate.from_mapping(payload)
    result = PromptLabService(_registry).evaluate(payload)

    assert candidate.evaluation_cases == ()
    assert result["report"]["passed"] is False
    codes = {issue["code"] for issue in result["report"]["issues"]}
    assert "missing-success-validation" in codes
    assert "missing-regression-validation" in codes
    assert "missing-security-validation" in codes


def test_canonical_evaluation_does_not_leak_prompt_case_or_rollback_bodies():
    secret = "never emit sk-test-candidate-secret"
    payload = _candidate(proposed_prompt=f"Read fewer files. {secret}")
    payload["rollback_plan"] = f"Revert review note {secret}"
    payload["evaluation_cases"][0]["metric"] = (
        f"assert output does not contain {secret}")

    result = PromptLabService(_registry).evaluate(payload)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["contains_prompt_content"] is False
    assert secret not in serialized
    assert payload["proposed_prompt"] not in serialized
    assert payload["rollback_plan"] not in serialized


def test_legacy_evaluate_remains_compatible_and_content_light():
    result = PromptLabService(_registry).evaluate(_legacy_spec())

    assert result["action"] == "evaluate"
    assert result["report"]["passed"] is True
    assert result["applied"] is False
    assert result["contains_prompt_content"] is False
    assert "candidate" not in result


def test_prompt_lab_tool_schema_exposes_candidate_fields():
    spec = ToolPromptSpec.from_tool(PromptLabTool())
    properties = spec.parameters["properties"]["spec"]["properties"]

    assert _CANDIDATE_KEYS.issubset(properties)
    assert properties["evaluation_cases"]["items"]["additionalProperties"] is False
    assert properties["proposed_prompt"]["maxLength"] == 40_000
    assert properties["risk"]["enum"] == ["low", "medium", "high"]
    assert validate_tool_arguments(spec.parameters, {
        "action": "evaluate", "spec": _candidate(),
    }) == []
    invalid = _candidate()
    invalid["risk"] = "critical"
    assert validate_tool_arguments(spec.parameters, {
        "action": "evaluate", "spec": invalid,
    })


def test_prompt_lab_tool_executes_canonical_generate_and_evaluate(tmp_path):
    registry = _registry()
    context = ToolContext(
        workspace_root=tmp_path,
        sandbox_enabled=False,
        prompt_registry=registry,
    )
    tool = PromptLabTool()

    generated = asyncio.run(tool.execute({
        "action": "generate", "spec": _candidate(),
    }, context))
    evaluated = asyncio.run(tool.execute({
        "action": "evaluate", "spec": _candidate(),
    }, context))

    assert generated.is_error is False
    assert evaluated.is_error is False
    assert "NOT applied" in generated.content
    assert '"target_fragment": "core:base:tool-discipline"' in generated.content
    assert "PromptLab evaluate: PASS" in evaluated.content
    assert "body_returned=false" in evaluated.content
