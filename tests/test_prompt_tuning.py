"""Failure-trajectory attribution and Prompt tuning candidate tests."""

from __future__ import annotations

import json

from krelcode.benchmarks import (
    BEHAVIOR_CASES,
    BehaviorBenchmarkRunner,
    BehaviorCheck,
    BehaviorMetric,
    BehaviorReport,
    BehaviorTrialResult,
    FailureCause,
    FailureTrajectoryAnalyzer,
    analyze_failure_trajectories,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider


def _case(case_id: str):
    return next(case for case in BEHAVIOR_CASES if case.id == case_id)


def _run(case_id: str, script):
    return BehaviorBenchmarkRunner(
        lambda: MockProvider(scripted=list(script)),
        cases=(_case(case_id),),
        sandbox_backend="static",
        provider_name="scripted",
        model_name="scripted-model",
    ).run()


def test_scope_failure_maps_to_active_prompt_and_playbooks():
    report = _run("read-scope", [
        Message.assistant(tool_calls=[
            ToolCall("read-a", "read_file", {"path": "src/a.py"}),
            ToolCall("read-b", "read_file", {"path": "src/b.py"}),
        ]),
        "SECRET_ANSWER_MARKER_42",
    ])
    tuning = analyze_failure_trajectories(report)

    causes = {item.cause for item in tuning.attributions}
    assert FailureCause.SCOPE_OVERREACH in causes
    candidate = next(
        item for item in tuning.candidates
        if item.cause is FailureCause.SCOPE_OVERREACH
    )
    assert "core:base:tool-discipline" in candidate.targets
    assert "tool_playbook:file-read" in candidate.targets
    assert candidate.auto_apply is False
    assert any("src/b.py" in evidence for evidence in candidate.evidence)
    assert "SECRET_ANSWER_MARKER_42" not in json.dumps(
        tuning.as_dict(), ensure_ascii=False)


def test_missing_verification_maps_to_verification_prompt_and_command_playbook():
    report = _run(
        "edit-verify",
        ["Done: I changed src/a.py and the tests passed."],
    )
    tuning = FailureTrajectoryAnalyzer().analyze(report)

    candidate = next(
        item for item in tuning.candidates
        if item.cause is FailureCause.VERIFICATION_MISSING
    )
    assert "core:base:verification" in candidate.targets
    assert "tool_playbook:command-execution" in candidate.targets
    assert "exact non-interactive command" in candidate.proposed_instruction
    assert candidate.validation_plan[-1].startswith("Require no new failed metric")


def test_runtime_failure_is_attributed_but_does_not_emit_prompt_candidate():
    def fail_provider():
        raise RuntimeError("provider unavailable")

    report = BehaviorBenchmarkRunner(
        fail_provider,
        cases=(_case("read-scope"),),
        sandbox_backend="static",
    ).run()
    tuning = analyze_failure_trajectories(report)

    assert len(tuning.attributions) == 1
    assert tuning.attributions[0].cause is FailureCause.RUNTIME_ERROR
    assert tuning.attributions[0].error_kind == "internal"
    assert tuning.candidates == ()


def test_workflow_mismatch_targets_expected_workflow_surface():
    result = BehaviorTrialResult(
        case_id="review-no-edit",
        trial=1,
        passed=False,
        checks=(BehaviorCheck(
            BehaviorMetric.WORKFLOW_FOLLOWED,
            False,
            "selected general, expected code_review",
        ),),
        facts={
            "task_workflow": "general",
            "active_prompt_fragments": ("workflow:general",),
        },
    )
    report = BehaviorReport(
        schema_version=2,
        generated_at="test",
        provider="scripted",
        model="scripted-model",
        passed=False,
        results=(result,),
        duration_ms=1,
        profile={"suite_hash": "abc"},
    )
    tuning = analyze_failure_trajectories(report)

    attribution = tuning.attributions[0]
    assert attribution.cause is FailureCause.WORKFLOW_MISMATCH
    assert attribution.targets == ("workflow:code_review",)
    assert tuning.candidates[0].risk.value == "medium"


def test_inactive_prompt_surfaces_are_not_recommended():
    result = BehaviorTrialResult(
        case_id="read-scope",
        trial=1,
        passed=False,
        checks=(BehaviorCheck(
            BehaviorMetric.TRAJECTORY_SCOPE,
            False,
            "read an unrelated file",
        ),),
        facts={
            "active_prompt_fragments": ("core:base:tool-discipline",),
            "task_workflow": "code_explanation",
        },
    )
    report = BehaviorReport(
        schema_version=2,
        generated_at="test",
        provider="scripted",
        model="scripted-model",
        passed=False,
        results=(result,),
        duration_ms=1,
        profile={"suite_hash": "abc"},
    )
    tuning = analyze_failure_trajectories(report)

    assert tuning.candidates[0].targets == ("core:base:tool-discipline",)


def test_tuning_report_is_content_light_and_human_readable():
    report = _run(
        "edit-verify",
        ["Done without tools or verification."],
    )
    tuning = analyze_failure_trajectories(report)
    payload = tuning.as_dict()
    text = tuning.render_text()

    assert payload["kind"] == "prompt_tuning"
    assert payload["summary"]["auto_apply"] is False
    assert payload["candidates"]
    assert "Prompt Tuning Candidates" in text
    assert "auto_apply=false" in text
    assert "Done without tools or verification." not in json.dumps(payload)
