"""Reliability recovery, evidence freshness and verification policy gates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from krelcode.agent.turn.acceptance import acceptance_evidence_from_history
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.plan_verification import apply_plan_completion
from krelcode.agent.turn.progress import ProgressMonitor
from krelcode.agent.turn.query import QueryEvent
from krelcode.agent.turn.verification_chain import apply_verification_policy
from krelcode.application.acceptance import DefaultAcceptanceEvaluator
from krelcode.contracts.acceptance import (
    AcceptanceResult,
    AcceptanceStatus,
)
from krelcode.contracts.agent import AgentContractFactory
from krelcode.contracts.application import (
    AcceptanceContract,
    ApplicationRequestFactory,
    RiskLevel,
    RiskProfile,
)
from krelcode.contracts.degradation import DegradationDecision
from krelcode.contracts.loop import (
    Observation,
    ObservationSource,
    ObservationStatus,
)
from krelcode.contracts.plan import (
    PlanControl,
    PlanControlMode,
    PlanNode,
    PlanRevision,
)
from krelcode.contracts.recovery import RecoveryStrategy
from krelcode.contracts.runtime import RuntimeRetryPolicy, RuntimeTask
from krelcode.contracts.task_lifecycle import (
    SideEffectStatus,
    TaskErrorClass,
    TaskSideEffectRecord,
    evaluate_side_effect_replay,
)
from krelcode.contracts.verification import (
    VerificationLevel,
    VerificationPolicy,
    VerificationRequest,
    VerificationResult,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.core.events import RecoveryDecided
from krelcode.llm.providers.mock import MockProvider
from krelcode.runtime.execution import LocalTaskRuntime
from krelcode.runtime.execution.task_errors import (
    RuntimeSideEffectRecoveryRequired,
)
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def test_side_effect_replay_fails_closed_for_unknown_effect() -> None:
    unknown = TaskSideEffectRecord(
        effect_id="effect-1",
        task_id="task-1",
        kind="external_state",
        resource="release",
        status=SideEffectStatus.UNKNOWN,
    )
    decision = evaluate_side_effect_replay((unknown,))
    assert decision.allowed is False
    assert decision.blocking_effect_ids == ("effect-1",)

    safe = TaskSideEffectRecord(
        effect_id="effect-2",
        task_id="task-1",
        kind="external_state",
        resource="upsert",
        status=SideEffectStatus.COMMITTED,
        idempotency_key="action-idempotent",
        metadata={"idempotent": True, "replay_safe": True},
    )
    assert evaluate_side_effect_replay((safe,)).allowed is True


def test_degradation_decision_records_quality_and_capability_delta() -> None:
    decision = DegradationDecision(
        dimension="model_profile",
        from_artifact="openai/model-a",
        to_artifact="openai/model-b",
        reason="fallback model is not calibrated",
        capability_diff=("model", "tool_calling"),
        quality_risk="high",
        evidence={"calibrated": False},
    )
    record = decision.to_dict()
    assert record["quality_risk"] == "high"
    assert record["capability_diff"] == ["model", "tool_calling"]


def test_recovery_event_has_stable_stream_json_contract() -> None:
    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="session-1",
        cwd="/workspace",
        provider="mock",
        model="mock-model",
    ))
    payload = encoder.encode(QueryEvent(
        "turn-1",
        7,
        RecoveryDecided(
            failure_class="no_progress",
            strategy="replan",
            reason="plan did not change",
            retryable=True,
            requires_replan=True,
            attempt=1,
        ),
    ))
    assert payload["type"] == "recovery"
    assert payload["subtype"] == "decided"
    assert payload["strategy"] == "replan"
    assert payload["sequence"] == 7


def test_task_retry_stops_when_prior_side_effect_is_unknown() -> None:
    runtime = LocalTaskRuntime()

    class EffectThenFail:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, task: RuntimeTask) -> str:
            self.calls += 1
            runtime.record_side_effect(
                task.task_id,
                TaskSideEffectRecord(
                    effect_id="effect-unknown",
                    task_id=task.task_id,
                    kind="external_state",
                    resource="publish",
                    status=SideEffectStatus.UNKNOWN,
                ),
            )
            raise RuntimeError("response lost after side effect")

    async def scenario() -> None:
        executor = EffectThenFail()
        task = RuntimeTask(
            task_id="retry-effect-task",
            turn_id="turn-retry-effect",
            message="publish",
            application=ApplicationRequestFactory.from_input_source(
                "headless", goal="publish"),
            retry_policy=RuntimeRetryPolicy(
                max_attempts=2,
                retry_on_failure=True,
            ),
        )
        handle = runtime.submit(task, executor)
        with pytest.raises(RuntimeSideEffectRecoveryRequired):
            await handle.wait()
        state = runtime.inspect(handle.task_id)
        assert executor.calls == 1
        assert state.status.value == "failed"
        assert state.error_info is not None
        assert state.error_info.category is TaskErrorClass.CONFLICT

    asyncio.run(scenario())


def test_stale_test_evidence_cannot_satisfy_acceptance() -> None:
    request = ApplicationRequestFactory.from_input_source(
        "sdk",
        goal="fix tests",
        acceptance=AcceptanceContract(
            criteria=("tests pass",),
            required_evidence=("tests",),
        ),
    )
    history = [
        Message.tool_result(
            "test-before",
            "1 passed",
            tool_name="run_command",
            is_error=False,
            metadata={"command": "pytest", "observed_at": 1.0},
        ),
        Message.tool_result(
            "write-after",
            "written",
            tool_name="write_file",
            is_error=False,
            metadata={
                "side_effect_kind": "file_mutation",
                "observed_at": 2.0,
            },
        ),
    ]
    evidence = acceptance_evidence_from_history(
        history, workspace_version={"tree_digest": "tree-1"})
    result = DefaultAcceptanceEvaluator().evaluate(
        request, "implemented", evidence)
    assert result.status is AcceptanceStatus.FAILED
    assert "过期" in result.reason

    history.append(Message.tool_result(
        "test-after",
        "1 passed",
        tool_name="run_command",
        is_error=False,
        metadata={"command": "pytest", "observed_at": 3.0},
    ))
    fresh = acceptance_evidence_from_history(
        history, workspace_version={"tree_digest": "tree-1"})
    passed = DefaultAcceptanceEvaluator().evaluate(
        request, "implemented", fresh)
    assert passed.status is AcceptanceStatus.PASSED


def test_progress_detection_replans_then_escalates() -> None:
    monitor = ProgressMonitor(max_replans=1)
    error = Observation(
        observation_id="obs-error",
        turn_id="turn-progress",
        source=ObservationSource.TOOL,
        kind="tool:write_file",
        status=ObservationStatus.ERROR,
        summary="validation failed",
        data={
            "tool_name": "write_file",
            "content_hash": "same",
            "diagnostics": {"error_kind": "schema_validation"},
        },
    )
    first = monitor.inspect(
        step=1,
        observations=(error, error, error),
        plan_control=None,
        abort_reason="same call failed three times",
    )
    assert first is not None
    assert first.strategy is RecoveryStrategy.REPLAN
    assert first.retryable is True

    second = monitor.inspect(
        step=2,
        observations=(error, error, error),
        plan_control=None,
        abort_reason="same call failed three times",
    )
    assert second is not None
    assert second.strategy is RecoveryStrategy.ESCALATE
    assert second.requires_user is True


def test_plan_pending_node_blocks_completion() -> None:
    request = ApplicationRequestFactory.from_input_source(
        "sdk", goal="ship")
    result = DefaultAcceptanceEvaluator().evaluate(request, "done", ())
    plan = PlanControl(
        mode=PlanControlMode.TODO,
        revision=PlanRevision(revision_id="plan-1"),
        nodes=(PlanNode(id="verify", objective="run tests"),),
    )
    checked = apply_plan_completion(result, plan)
    assert checked.status is AcceptanceStatus.FAILED
    assert any(
        item.criterion == "plan:verify" and not item.passed
        for item in checked.checks
    )


def test_critical_risk_requires_and_runs_external_verifier_chain() -> None:
    request = ApplicationRequestFactory.from_input_source(
        "sdk",
        goal="release",
        risk=RiskProfile(level=RiskLevel.CRITICAL),
    )
    policy = VerificationPolicy.for_request(request)
    assert VerificationLevel.INDEPENDENT_MODEL in policy.required_levels
    assert VerificationLevel.HUMAN in policy.required_levels
    result = DefaultAcceptanceEvaluator().evaluate(request, "done", ())

    no_ports = asyncio.run(apply_verification_policy(
        result=result,
        request=request,
        turn_id="turn-critical",
        final_response="done",
    ))
    assert no_ports.status is AcceptanceStatus.NEEDS_REVIEW

    class PassVerifier:
        def __init__(self, verifier_id: str) -> None:
            self.verifier_id = verifier_id

        async def verify(
            self,
            verification: VerificationRequest,
        ) -> VerificationResult:
            return VerificationResult(
                level=verification.level,
                verifier_id=self.verifier_id,
                status=AcceptanceStatus.PASSED,
                reason="approved",
            )

    verified = asyncio.run(apply_verification_policy(
        result=result,
        request=request,
        turn_id="turn-critical",
        final_response="done",
        ports={
            VerificationLevel.INDEPENDENT_MODEL: PassVerifier("model-verifier"),
            VerificationLevel.HUMAN: PassVerifier("release-owner"),
        },
    ))
    assert verified.status is AcceptanceStatus.PASSED
    assert verified.metadata["verifier_chain"] == [
        "structural", "independent_model", "human",
    ]


@dataclass
class _Exchange:
    answer: str

    @property
    def request_id(self) -> str:
        return "review-1"

    async def wait(self) -> Optional[str]:
        return self.answer

    def cancel(self) -> None:
        return None


class _InputPort:
    def submit(
        self,
        prompt: str,
        options: tuple[str, ...] = (),
        *,
        allow_free_text: bool = True,
        request_id: str = "",
    ) -> _Exchange:
        return _Exchange("approve")


def test_needs_review_can_be_approved_through_waiting(tmp_path: Path) -> None:
    request = ApplicationRequestFactory.from_input_source(
        "sdk",
        goal="human reviewed result",
        acceptance=AcceptanceContract(
            criteria=("manual sign-off",),
            required_evidence=("approval-ticket",),
        ),
    )
    contract = AgentContractFactory.from_application(request)
    loop = AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        user_input_port=_InputPort(),
    )
    loop.acceptance_evaluator = DefaultAcceptanceEvaluator()

    assert asyncio.run(loop.run(
        "human reviewed result",
        application_request=request,
        agent_contract=contract,
    )) == "done"
    assert loop.acceptance_result is not None
    assert loop.acceptance_result.status is AcceptanceStatus.PASSED
    assert loop.acceptance_result.metadata["human_review"] == "approved"
