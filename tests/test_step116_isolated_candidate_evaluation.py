"""Step 116: isolated Candidate Prompt evaluation before human review."""

from __future__ import annotations

import asyncio
import json

from krelcode.agent.fragments import (
    GROUP_SYSTEM,
    LAYER_L0_MANAGED_POLICY,
    LAYER_L5_MODE_CONTRACT,
    LAYER_L6_CATALOG,
    STABILITY_SESSION,
    TRUST_MANAGED,
    PromptFragment,
    PromptFragmentRegistry,
    base_fragments,
)
from krelcode.agent.prompt_candidate import PromptCandidate
from krelcode.agent.prompt_isolation import (
    CACHE_STABILITY_CHECK,
    CURRENT_FAILURE_CHECK,
    EVIDENCE_INJECTION_CHECK,
    INSTRUCTION_PRECEDENCE_CHECK,
    PERMISSION_SANDBOX_CHECK,
    STABLE_REGRESSION_CHECK,
    PromptCandidateIsolator,
)
from krelcode.agent.prompt_lab import PromptLabService
from krelcode.tools.base import PermissionMode, RuntimeSnapshot, ToolContext
from krelcode.agent.prompt_lab_tool import PromptLabTool


_CHECK_IDS = (
    CURRENT_FAILURE_CHECK,
    STABLE_REGRESSION_CHECK,
    INSTRUCTION_PRECEDENCE_CHECK,
    EVIDENCE_INJECTION_CHECK,
    PERMISSION_SANDBOX_CHECK,
    CACHE_STABILITY_CHECK,
)


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
            "metric": "assert focused tests exit_code == 0",
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
    target: str = "project:isolation-policy",
    prompt: str = "Read only the files required by the current request.",
    cases: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "target_fragment": target,
        "objective": "Reduce unrelated reads without expanding authority.",
        "proposed_prompt": prompt,
        "constraints": ["Do not expand tools, approval, or Sandbox authority."],
        "expected_behavior": ["Read fewer unrelated files."],
        "risk": "low",
        "evaluation_cases": _cases() if cases is None else cases,
        "rollback_plan": "Discard the unapplied candidate review.",
    }


def _registry() -> PromptFragmentRegistry:
    return PromptFragmentRegistry(base_fragments())


def _context(
    registry: PromptFragmentRegistry,
    tmp_path,
    *,
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
    sandbox_enabled: bool = False,
) -> ToolContext:
    return ToolContext(
        workspace_root=tmp_path,
        sandbox_enabled=sandbox_enabled,
        runtime=RuntimeSnapshot(permission_mode=permission_mode),
        prompt_registry=registry,
    )


def _register_fragment(
    registry: PromptFragmentRegistry,
    *,
    fragment_id: str,
    content: str,
    layer: str,
    source: str = "managed",
    stability: str = STABILITY_SESSION,
) -> None:
    registry.register(PromptFragment(
        id=fragment_id,
        source=source,
        title=fragment_id,
        content=content,
        group=GROUP_SYSTEM,
        layer=layer,
        stability=stability,
        trust=TRUST_MANAGED,
        cacheable=False,
        metadata={
            "instruction": True,
            "source_role": "instruction",
            "instruction_source": source,
        },
    ))


def test_isolation_runs_the_six_gates_in_fixed_order(tmp_path):
    registry = _registry()
    context = _context(registry, tmp_path)
    result = PromptLabService(lambda: registry, isolation_context=context).evaluate(
        _candidate())

    isolation = result["isolation"]
    assert tuple(check["id"] for check in isolation["checks"]) == _CHECK_IDS
    assert isolation["mode"] == "in_memory_temporary_prompt_plan"
    assert isolation["passed"] is True
    assert result["passed"] is True
    assert result["review_allowed"] is True
    assert result["prompt_release_eligible"] is True
    assert result["release_requires_manual_review"] is True
    assert result["applied"] is False
    assert result["production_modified"] is False


def test_missing_success_case_blocks_current_failure_gate():
    registry = _registry()
    cases = [case for case in _cases() if case["kind"] != "success"]
    result = PromptLabService(lambda: registry).evaluate(_candidate(cases=cases))

    check = next(
        item for item in result["isolation"]["checks"]
        if item["id"] == CURRENT_FAILURE_CHECK)
    assert check["passed"] is False
    assert "missing-success-case" in check["issue_codes"]
    assert result["review_allowed"] is False
    assert result["prompt_release_eligible"] is False


def test_missing_regression_case_blocks_stable_regression_gate():
    registry = _registry()
    cases = [case for case in _cases() if case["kind"] != "regression"]
    result = PromptLabService(lambda: registry).evaluate(_candidate(cases=cases))

    check = next(
        item for item in result["isolation"]["checks"]
        if item["id"] == STABLE_REGRESSION_CHECK)
    assert check["passed"] is False
    assert "missing-regression-case" in check["issue_codes"]
    assert result["review_allowed"] is False


def test_candidate_cannot_modify_l5_permission_contract(tmp_path):
    registry = _registry()
    _register_fragment(
        registry,
        fragment_id="managed:test-mode-contract",
        content="Tool contract for the test runtime.",
        layer=LAYER_L5_MODE_CONTRACT,
    )
    context = _context(registry, tmp_path, permission_mode=PermissionMode.PLAN)
    result = PromptLabService(lambda: registry, isolation_context=context).evaluate(
        _candidate(target="managed:test-mode-contract"))

    check = next(
        item for item in result["isolation"]["checks"]
        if item["id"] == PERMISSION_SANDBOX_CHECK)
    assert check["passed"] is False
    assert "candidate-cannot-modify-mode-contract" in check["issue_codes"]
    assert check["evidence"]["permission_mode"] == "plan"
    assert result["review_allowed"] is False


def test_instruction_precedence_rejects_candidate_overridden_by_managed_rule():
    registry = _registry()
    _register_fragment(
        registry,
        fragment_id="managed:indent",
        content="Use tabs for indentation.",
        layer=LAYER_L0_MANAGED_POLICY,
    )
    result = PromptLabService(lambda: registry).evaluate(_candidate(
        target="project:spaces",
        prompt="Use spaces for indentation.",
    ))

    check = next(
        item for item in result["isolation"]["checks"]
        if item["id"] == INSTRUCTION_PRECEDENCE_CHECK)
    assert check["passed"] is False
    assert "candidate-overridden-by-precedence" in check["issue_codes"]
    assert result["review_allowed"] is False


def test_evidence_injection_stays_in_the_evidence_channel(tmp_path):
    registry = _registry()
    context = _context(registry, tmp_path)
    result = PromptLabService(lambda: registry, isolation_context=context).evaluate(
        _candidate())

    check = next(
        item for item in result["isolation"]["checks"]
        if item["id"] == EVIDENCE_INJECTION_CHECK)
    assert check["passed"] is True
    assert check["evidence"]["isolation_marker_separated"] is True
    assert check["evidence"]["evidence_ids"] == [
        "prompt-isolation:evidence-injection"]
    assert "prompt-isolation:evidence-injection" not in check["evidence"][
        "instruction_ids"]


def test_isolation_does_not_mutate_registry_or_write_candidate(tmp_path):
    registry = _registry()
    before_generation = registry.generation
    before_fragments = tuple(registry.list())
    context = _context(registry, tmp_path)
    service = PromptLabService(lambda: registry, isolation_context=context)

    evaluated = service.evaluate(_candidate())
    generated = service.generate(_candidate())

    assert evaluated["applied"] is False
    assert evaluated["production_modified"] is False
    assert generated["applied"] is False
    assert generated["production_modified"] is False
    assert registry.generation == before_generation
    assert tuple(registry.list()) == before_fragments
    assert registry.get("project:isolation-policy") is None


def test_repeated_temporary_plan_has_deterministic_cache_boundary(tmp_path):
    registry = _registry()
    context = _context(registry, tmp_path)
    isolator = PromptCandidateIsolator(lambda: registry, context=context)
    candidate = PromptCandidate.from_mapping(_candidate())

    first = isolator.evaluate(candidate)
    second = isolator.evaluate(candidate)

    assert first.candidate_plan["stable_hash"] == second.candidate_plan["stable_hash"]
    assert first.candidate_plan["dynamic_hash"] == second.candidate_plan["dynamic_hash"]
    assert first.candidate_plan["full_hash"] == second.candidate_plan["full_hash"]
    assert first.candidate_plan["cache_boundary_chars"] == (
        second.candidate_plan["cache_boundary_chars"])
    check = next(
        item for item in first.checks if item.id == CACHE_STABILITY_CHECK)
    assert check.passed is True


def test_candidate_cache_placement_accounts_for_earlier_dynamic_l5(tmp_path):
    registry = _registry()
    context = _context(registry, tmp_path)
    result = PromptLabService(lambda: registry, isolation_context=context).evaluate(
        _candidate())

    check = next(
        item for item in result["isolation"]["checks"]
        if item["id"] == CACHE_STABILITY_CHECK)
    assert check["passed"] is True
    assert "candidate-cache-placement-mismatch" not in check["issue_codes"]


def test_isolation_report_does_not_leak_candidate_or_metric_body():
    secret = "sk-test-step116-never-return-this"
    payload = _candidate(prompt=f"Read fewer files. {secret}")
    payload["rollback_plan"] = f"Rollback note {secret}"
    payload["evaluation_cases"][0]["metric"] = (
        f"assert output does not contain {secret}")
    result = PromptLabService(_registry).evaluate(payload)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result["contains_prompt_content"] is False
    assert secret not in serialized
    assert payload["proposed_prompt"] not in serialized
    assert payload["rollback_plan"] not in serialized
    assert payload["evaluation_cases"][0]["metric"] not in serialized


def test_legacy_authoring_evaluation_cannot_bypass_isolation_release_gate():
    result = PromptLabService(_registry).evaluate({
        "objective": "Cite repository paths.",
        "target_fragment_id": "project:legacy-policy",
        "target_layer": LAYER_L6_CATALOG,
        "items": ({
            "id": "project:legacy-policy",
            "role": "instruction",
            "source": "project",
            "content": "Cite repository paths for evidence claims.",
            "scope": "project",
            "layer": LAYER_L6_CATALOG,
            "stability": STABILITY_SESSION,
            "cacheable": True,
        },),
        "permission_mode": "default",
        "tool_scope": "none",
        "validation_cases": _cases(),
        "success_metrics": ("assert response contains a path",),
        "non_goals": ("Do not expand authority.",),
    })

    assert result["report"]["passed"] is True
    assert result["legacy_authoring_only"] is True
    assert result["passed"] is False
    assert result["review_allowed"] is False
    assert result["prompt_release_eligible"] is False


def test_tool_result_renders_six_isolation_checks_and_review_status(tmp_path):
    registry = _registry()
    context = _context(registry, tmp_path)
    tool = PromptLabTool()

    result = asyncio.run(tool.execute({
        "action": "evaluate",
        "spec": _candidate(),
    }, context))

    assert result.is_error is False
    assert "isolation: PASS" in result.content
    assert "review: eligible for human review" in result.content
    assert "release_requires_manual_review=true" in result.content
    assert "production_modified=false" in result.content
    for check_id in _CHECK_IDS:
        assert check_id in result.content


def test_tool_uses_the_current_tool_context_permission_mode(tmp_path):
    registry = _registry()
    context = _context(registry, tmp_path, permission_mode=PermissionMode.PLAN)
    result = asyncio.run(PromptLabTool().execute({
        "action": "evaluate",
        "spec": _candidate(),
    }, context))

    assert result.is_error is False
    check = next(
        item for item in result.data["isolation"]["checks"]
        if item["id"] == PERMISSION_SANDBOX_CHECK)
    assert check["evidence"]["permission_mode"] == "plan"
