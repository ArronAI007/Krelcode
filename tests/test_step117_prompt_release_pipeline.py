"""Step 117: Candidate → Benchmark → Release proposal → human approval."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from krelcode.agent.fragments import PromptFragmentRegistry, base_fragments
from krelcode.agent.prompt_candidate import PromptCandidate
from krelcode.agent.prompt_release import build_prompt_release
from krelcode.agent.prompt_release_pipeline import (
    PromptPipelineError,
    PromptPipelineRequest,
    PromptPipelineStageStatus,
    PromptProposalState,
    PromptReleaseApprovalService,
    PromptReleasePipeline,
    PromptReleaseProposalStore,
)
from krelcode.agent.prompts import (
    CURRENT_PROMPT_RELEASE,
    FROZEN_PROMPT_RELEASE_HASH,
    PROMPT_BASE_SECTIONS,
    PROMPT_PROFILES,
)
from krelcode.benchmarks import (
    BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
    FROZEN_PROMPT_PARITY_SUITE_HASH,
    PROMPT_PARITY_CONTRACT,
    BehaviorCheck,
    BehaviorMetric,
    BehaviorReport,
    BehaviorTrialResult,
    PromptParityBaseline,
)
from krelcode.session.commands import CommandRegistry, CommandRequest
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_prompt_release
from krelcode.skills.manager import BUILTIN_SKILL_ROOT, SkillManager
from krelcode.tools.base import ToolContext
from krelcode.agent.prompt_lab_tool import PromptLabTool


def _candidate(
    *,
    prompt: str = "Read only files required by the current request.",
    objective: str = "Reduce unrelated file reads without changing authority.",
    rollback_plan: str = "Discard this unapplied candidate review.",
) -> PromptCandidate:
    return PromptCandidate.from_mapping({
        "target_fragment": "project:step117-policy",
        "objective": objective,
        "proposed_prompt": prompt,
        "constraints": ["Do not expand tools, approvals, or Sandbox authority."],
        "expected_behavior": ["Read fewer unrelated files."],
        "risk": "low",
        "evaluation_cases": [
            {
                "id": "success",
                "kind": "success",
                "metric": "assert unrelated file reads == 0",
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
                "metric": "assert output has no credential-shaped text",
                "machine_verifiable": True,
            },
        ],
        "rollback_plan": rollback_plan,
    })


def _offline(*, passed: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        passed=passed,
        as_dict=lambda: {
            "schema_version": 1,
            "passed": passed,
            "summary": {"cases": 9, "passed": 9 if passed else 8},
            "results": [],
            "contains_prompt_content": False,
        },
    )


def _live(
    *,
    provider: str = "deepseek",
    model: str = "deepseek-chat",
    passed: bool = True,
) -> BehaviorReport:
    profile = CURRENT_PROMPT_RELEASE.profile("deepseek")
    results = tuple(
        BehaviorTrialResult(
            case_id=case_id,
            trial=trial,
            passed=passed,
            checks=(
                BehaviorCheck(
                    BehaviorMetric.TASK_SUCCESS,
                    passed,
                    "step117 fixture",
                ),
            ),
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        for case_id in PROMPT_PARITY_CONTRACT.required_case_ids
        for trial in range(1, 4)
    )
    return BehaviorReport(
        schema_version=BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        generated_at="2026-11-21T00:00:00+00:00",
        provider=provider,
        model=model,
        passed=passed,
        results=results,
        duration_ms=1,
        profile={
            "suite_hash": FROZEN_PROMPT_PARITY_SUITE_HASH,
            "parity_contract_version": PROMPT_PARITY_CONTRACT.version,
            "parity_contract_hash": PROMPT_PARITY_CONTRACT.hash,
            "cases": list(PROMPT_PARITY_CONTRACT.required_case_ids),
            "trials": 3,
            "prompt_release_id": CURRENT_PROMPT_RELEASE.release_id,
            "prompt_release_hash": CURRENT_PROMPT_RELEASE.release_hash,
            "prompt_profile": profile.name,
            "prompt_profile_hash": profile.profile_hash,
            "profile_calibration_status": "uncalibrated",
        },
    )


def _pipeline(tmp_path, *, release=CURRENT_PROMPT_RELEASE):
    registry = PromptFragmentRegistry(base_fragments())
    context = ToolContext(
        workspace_root=tmp_path,
        sandbox_enabled=False,
        prompt_registry=registry,
    )
    store = PromptReleaseProposalStore(tmp_path)
    pipeline = PromptReleasePipeline(
        lambda: registry,
        release=release,
        isolation_context=context,
        store=store,
        clock=lambda: "2026-11-21T00:00:00+00:00",
    )
    return pipeline, store


def _prepare(tmp_path, **request_updates):
    pipeline, store = _pipeline(tmp_path)
    request = PromptPipelineRequest(
        candidate=_candidate(),
        offline_report=_offline(),
        live_report=_live(),
        run_offline=False,
        run_live=False,
    )
    request = replace(request, **request_updates)
    proposal = asyncio.run(pipeline.prepare_async(request))
    return pipeline, store, proposal


def test_pipeline_runs_every_existing_gate_and_stops_at_human_approval(tmp_path):
    proposal = asyncio.run(_pipeline(tmp_path)[0].prepare_async(
        PromptPipelineRequest(
            candidate=_candidate(),
            offline_report=_offline(),
            live_report=_live(),
            run_offline=False,
            run_live=False,
        )
    ))

    assert proposal.state is PromptProposalState.PENDING_APPROVAL
    assert tuple(stage.id for stage in proposal.stages) == (
        "authoring",
        "candidate",
        "isolated_prompt_assembler",
        "offline_benchmark",
        "conflict_analysis",
        "live_benchmark",
        "prompt_parity",
        "release_calibration",
        "human_approval",
        "release_artifacts",
    )
    assert proposal.stages[-2].status is PromptPipelineStageStatus.PENDING
    assert proposal.stages[-1].status is PromptPipelineStageStatus.NOT_CREATED
    assert proposal.calibration is not None
    assert proposal.approval_required is True
    assert proposal.production_modified is False
    assert proposal.applied is False


def test_missing_offline_or_live_evidence_cannot_reach_approval(tmp_path):
    pipeline, _ = _pipeline(tmp_path)
    candidate = _candidate()

    offline_missing = asyncio.run(pipeline.prepare_async(PromptPipelineRequest(
        candidate=candidate,
        run_offline=False,
        run_live=False,
    )))
    live_missing = asyncio.run(pipeline.prepare_async(PromptPipelineRequest(
        candidate=candidate,
        offline_report=_offline(),
        run_offline=False,
        run_live=False,
    )))

    assert offline_missing.state is PromptProposalState.PENDING_OFFLINE
    assert live_missing.state is PromptProposalState.PENDING_LIVE
    assert live_missing.approval_pending is False


def test_failed_offline_or_live_benchmark_blocks_release(tmp_path):
    pipeline, _ = _pipeline(tmp_path)
    candidate = _candidate()

    offline = asyncio.run(pipeline.prepare_async(PromptPipelineRequest(
        candidate=candidate,
        offline_report=_offline(passed=False),
        run_offline=False,
    )))
    live = asyncio.run(pipeline.prepare_async(PromptPipelineRequest(
        candidate=candidate,
        offline_report=_offline(),
        live_report=_live(passed=False),
        run_offline=False,
    )))

    assert offline.state is PromptProposalState.BLOCKED_OFFLINE
    assert live.state is PromptProposalState.BLOCKED_LIVE


def test_parity_baseline_failure_blocks_calibration_and_approval(tmp_path):
    accepted = PromptParityBaseline.from_report(_live(model="deepseek-chat"))
    pipeline, _ = _pipeline(tmp_path)

    proposal = asyncio.run(pipeline.prepare_async(PromptPipelineRequest(
        candidate=_candidate(),
        offline_report=_offline(),
        live_report=_live(model="deepseek-reasoner"),
        parity_baseline=accepted,
        run_offline=False,
    )))

    assert proposal.state is PromptProposalState.BLOCKED_PARITY
    assert proposal.calibration is None
    parity_stage = next(
        stage for stage in proposal.stages if stage.id == "prompt_parity")
    assert parity_stage.status is PromptPipelineStageStatus.FAILED


def test_human_approval_binds_proposal_and_candidate_hash(tmp_path):
    _, store, proposal = _prepare(tmp_path)
    service = PromptReleaseApprovalService(store)

    with pytest.raises(PromptPipelineError, match="不匹配"):
        service.approve(
            proposal.proposal_id,
            "0" * 64,
            approved_by="arron",
        )
    with pytest.raises(PromptPipelineError, match="人类"):
        service.approve(
            proposal.proposal_id,
            proposal.candidate_hash,
            approved_by="agent",
        )

    approved = service.approve(
        proposal.proposal_id,
        proposal.candidate_hash,
        approved_by="arron",
        reason="reviewed isolated evidence and costs",
    )

    assert approved.state is PromptProposalState.APPROVED
    assert approved.approved_by == "arron"
    assert approved.production_modified is False
    assert approved.applied is False
    assert store.load_approval(proposal.proposal_id).decision == "approved"
    assert (tmp_path / ".krelcode/prompt-releases/calibrations"
            / f"{proposal.proposal_id}.json").is_file()
    assert (tmp_path / ".krelcode/prompt-releases/artifacts"
            / f"{proposal.proposal_id}.json").is_file()


def test_identity_drift_marks_proposal_stale_and_refuses_approval(tmp_path):
    _, store, proposal = _prepare(tmp_path)
    other_release = build_prompt_release(
        "other-release",
        PROMPT_BASE_SECTIONS,
        PROMPT_PROFILES,
        frozen_at="2026-11-22T00:00:00+00:00",
    )
    service = PromptReleaseApprovalService(store, release=other_release)

    with pytest.raises(PromptPipelineError, match="stale"):
        service.approve(
            proposal.proposal_id,
            proposal.candidate_hash,
            approved_by="arron",
        )

    stored = store.load_proposal(proposal.proposal_id)
    assert stored.state is PromptProposalState.STALE


def test_proposal_and_tool_status_are_content_light(tmp_path):
    secret = "sk-step117-never-return-this"
    prompt = f"Read only the required file. {secret}"
    pipeline, _ = _pipeline(tmp_path)
    proposal = asyncio.run(pipeline.prepare_async(PromptPipelineRequest(
        candidate=_candidate(prompt=prompt),
        offline_report=_offline(),
        run_offline=False,
    )))
    serialized = json.dumps(proposal.as_dict(), ensure_ascii=False)

    assert secret not in serialized
    assert prompt not in serialized
    assert proposal.contains_prompt_content is False

    context = pipeline.isolation_context
    context.prompt_release_pipeline = pipeline
    result = asyncio.run(PromptLabTool().execute({
        "action": "status",
        "proposal_id": proposal.proposal_id,
    }, context))
    assert result.is_error is False
    assert secret not in result.content
    assert "agent approval: forbidden" in result.content


def test_prompt_lab_can_only_propose_not_approve(tmp_path):
    pipeline, _ = _pipeline(tmp_path)
    pipeline.offline_runner = SimpleNamespace(run=lambda: _offline())
    context = pipeline.isolation_context
    context.prompt_release_pipeline = pipeline
    tool = PromptLabTool()

    async def go():
        proposed = await tool.execute({
            "action": "propose",
            "spec": _candidate().as_record(),
            "run_offline": True,
        }, context)
        approve = await tool.execute({
            "action": "approve",
            "proposal_id": "x",
        }, context)
        return proposed, approve

    proposed, approve = asyncio.run(go())
    assert proposed.is_error is False
    assert proposed.data["state"] == "pending_live"
    assert "human command: /prompt-release approve" in proposed.content
    assert approve.is_error is True
    actions = tool.parameters["properties"]["action"]["enum"]
    assert "approve" not in actions
    assert "propose" in actions
    assert "status" in actions


def test_slash_command_is_the_human_approval_path(tmp_path, monkeypatch):
    pipeline, store, proposal = _prepare(tmp_path)
    monkeypatch.setenv("USER", "terminal-human")
    loop = SimpleNamespace(
        services=SimpleNamespace(
            extensions=SimpleNamespace(
                prompt_release_pipeline=pipeline,
                prompt_release_approval_service=PromptReleaseApprovalService(store),
            ),
        ),
    )
    context = CommandContext(
        request=CommandRequest(
            "prompt-release",
            arguments=(
                f"approve {proposal.proposal_id} "
                f"{proposal.candidate_hash} reviewed"),
            raw_text="",
        ),
        loop=loop,
        command_registry=CommandRegistry(),
        workspace_root=tmp_path,
    )

    result = asyncio.run(handle_prompt_release(context))

    assert result.handled is True
    assert "approved" in "\n".join(result.output)
    assert store.load_proposal(proposal.proposal_id).approved_by == "terminal-human"


def test_prompt_authoring_skill_is_builtin_and_loadable(tmp_path):
    manager = SkillManager(
        tmp_path,
        home=tmp_path / "home",
        builtin_paths=(BUILTIN_SKILL_ROOT,),
    )

    skill = manager.get("prompt-authoring")
    loaded = manager.load("prompt-authoring")

    assert skill is not None
    assert skill.allowed_tools == ("prompt_lab",)
    assert "Isolated PromptAssembler" not in str(manager.issues)
    assert "Never approve or reject your own Candidate" in loaded


def test_release_identity_is_pinned_and_command_is_builtin():
    assert CURRENT_PROMPT_RELEASE.release_hash == FROZEN_PROMPT_RELEASE_HASH
    spec = builtin_command_registry().resolve("prompt-release")
    assert spec is not None
    assert spec.kind.value == "command"
    assert "approve" in spec.argument_hint
