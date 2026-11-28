"""Explicit five-stage Agent Loop runner."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import cast

from krelcode.contracts.acceptance import AcceptanceStatus
from krelcode.contracts.application import TaskBudget
from krelcode.contracts.loop import LoopPhase, LoopTerminationReason
from krelcode.contracts.middleware import (
    ExtensionLifecyclePoint,
    MiddlewareAction,
    MiddlewareContext,
)
from krelcode.contracts.recovery import (
    FailureClass,
    RecoveryDecision,
    RecoveryStrategy,
)
from krelcode.contracts.budget import (
    BudgetAction,
    BudgetDimension,
    BudgetSnapshot,
    UnfinishedManifest,
)
from krelcode.core.events import (
    QueryPhaseChanged,
    RunCancelled,
    RunFailed,
    RunFinished,
    StepStarted,
    MiddlewareEvaluated,
    RecoveryDecided,
)
from .acceptance import AcceptanceGateError, AcceptanceReviewRequired
from .contract_gate import contract_verification_gaps
from .loop_state import AgentLoopStateMachine
from .model_turn import finish_reason_failure
from .progress import ProgressMonitor, record_recovery_decision
from .runtime_ports import LoopRunnerHostPort
from .budget_control import (
    BudgetControlHost,
    build_unfinished_manifest,
    handle_budget_gate,
    initialize_budget,
    partial_delivery_text,
    record_llm_budget,
)


class TurnLoopRunner:
    """Drive Prepare → Model → Act → Observe → Verify with explicit states."""

    def __init__(
        self,
        host: LoopRunnerHostPort,
        *,
        turn_id: str,
        max_steps: int,
        token_budget: int | None,
        budget_model: str = "",
    ) -> None:
        self.host = host
        self.turn_id = turn_id
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.budget_model = budget_model
        self.state = AgentLoopStateMachine(turn_id, host._emit)
        self.progress = ProgressMonitor()

    async def run(self) -> str:
        host = self.host
        state = self.state
        await host._bind_loop_state_machine(state)
        host._failure_counts = {}
        host._token_total = 0
        budget_host = cast(BudgetControlHost, host)
        initialize_budget(
            budget_host,
            replace(
                (
                    host.application_request.budget
                    if host.application_request is not None else TaskBudget()
                ),
                max_steps=self.max_steps,
                max_tokens=(
                    self.token_budget
                    if self.token_budget is not None
                    else (
                        host.application_request.budget.max_tokens
                        if host.application_request is not None else None
                    )
                ),
            ),
            model=self.budget_model,
        )
        self.progress = ProgressMonitor(
            max_replans=(
                host.agent_contract.max_recovery_attempts
                if host.agent_contract is not None else 1
            )
        )
        try:
            terminal = await self._extension(
                ExtensionLifecyclePoint.TASK_START)
            if terminal is not None:
                return terminal
            terminal = await self._extension(
                ExtensionLifecyclePoint.PREPARE_BEFORE)
            if terminal is not None:
                return terminal
            for step in range(1, self.max_steps + 1):
                await state.set_step(step)
                await host._emit(StepStarted(step))
                if host.step_gate is not None:
                    await host.step_gate()
                await host._inject_steering()
                budget_snapshot = await handle_budget_gate(
                    budget_host)
                if budget_snapshot.action in {
                    BudgetAction.PARTIAL_DELIVERY,
                    BudgetAction.STOP,
                }:
                    hard_detail = "; ".join(
                        f"{BudgetDimension(item.dimension).value} "
                        f"{item.used:g}/{item.hard_limit:g}"
                        for item in budget_snapshot.dimensions
                        if BudgetDimension(item.dimension).value
                        in budget_snapshot.hard_dimensions
                        and item.hard_limit is not None
                    )
                    await self._budget_partial(
                        budget_snapshot,
                        reason=(
                            (
                                "user cancelled at budget threshold"
                                if budget_host.user_input_port is not None
                                else "hard budget exhausted"
                            )
                            if budget_snapshot.action is BudgetAction.STOP
                            else (
                                "hard budget threshold reached"
                                if budget_snapshot.hard_dimensions
                                else "user selected partial delivery at soft limit"
                            )
                        ),
                        detail=hard_detail,
                        termination_reason=(
                            LoopTerminationReason.BUDGET_EXHAUSTED
                            if budget_snapshot.action is BudgetAction.STOP
                            else LoopTerminationReason.PARTIAL_DELIVERY
                        ),
                    )
                terminal = await self._extension(
                    ExtensionLifecyclePoint.MODEL_BEFORE,
                    metadata={"step": step},
                )
                if terminal is not None:
                    return terminal
                await self._phase(
                    LoopPhase.MODEL,
                    f"模型判断下一步（Step {step}）",
                )
                llm_turn = await host._llm_turn(step)
                if host.budget_ledger is not None:
                    host.budget_ledger.record_step()
                record_llm_budget(
                    budget_host,
                    dict(llm_turn.usage),
                    total_tokens=host._token_total,
                )
                assistant = llm_turn.message
                terminal = await self._extension(
                    ExtensionLifecyclePoint.MODEL_AFTER,
                    metadata={
                        "step": step,
                        "finish_reason": llm_turn.finish_reason or "",
                        "tool_call_count": len(assistant.tool_calls or ()),
                    },
                )
                if terminal is not None:
                    return terminal
                finish_failure = finish_reason_failure(
                    llm_turn.finish_reason,
                    has_tool_calls=bool(assistant.tool_calls),
                )
                if finish_failure is not None:
                    if assistant.tool_calls:
                        await host._apply_tool_results(
                            assistant.tool_calls, llm_turn.tool_results)
                    await self._fail(
                        LoopTerminationReason.PROTOCOL_ERROR,
                        finish_failure,
                        source="provider",
                    )
                if not assistant.tool_calls:
                    terminal = await self._verify(
                        assistant.content or "",
                    )
                    if terminal is not None:
                        return terminal
                    continue
                terminal = await self._extension(
                    ExtensionLifecyclePoint.ACTION_BEFORE,
                    metadata={"tool_call_count": len(assistant.tool_calls)},
                )
                if terminal is not None:
                    return terminal
                if state.phase is LoopPhase.MODEL:
                    await self._phase(LoopPhase.ACT, "tool actions requested")
                abort_reason = await host._apply_tool_results(
                    assistant.tool_calls, llm_turn.tool_results)
                recovery = self.progress.inspect(
                    step=step,
                    observations=state.observations,
                    plan_control=host._turn_tool_context.plan_control,
                    abort_reason=abort_reason or "",
                )
                if recovery is not None:
                    await self._handle_recovery(recovery)
                terminal = await self._extension(
                    ExtensionLifecyclePoint.STATE_COMMIT,
                    metadata={
                        "tool_call_count": len(assistant.tool_calls),
                        "result_count": len(llm_turn.tool_results),
                    },
                )
                if terminal is not None:
                    return terminal
                terminal = await self._extension(
                    ExtensionLifecyclePoint.ACTION_AFTER,
                    metadata={"abort_reason": abort_reason or ""},
                )
                if terminal is not None:
                    return terminal
                if state.phase is LoopPhase.ACT:
                    terminal = await self._extension(
                        ExtensionLifecyclePoint.OBSERVE_BEFORE)
                    if terminal is not None:
                        return terminal
                    await self._phase(
                        LoopPhase.OBSERVE,
                        "tool observations collected",
                    )
                    terminal = await self._extension(
                        ExtensionLifecyclePoint.OBSERVE_AFTER)
                    if terminal is not None:
                        return terminal
                await self._phase(
                    LoopPhase.PREPARE,
                    "observations applied; rebuilding task view",
                )
                terminal = await self._extension(
                    ExtensionLifecyclePoint.PREPARE_AFTER)
                if terminal is not None:
                    return terminal
            ledger = host.budget_ledger
            if ledger is not None:
                await self._budget_partial(
                    ledger.snapshot(),
                    reason=f"单轮达到 {self.max_steps} 个模型步骤上限",
                    detail=f"steps {self.max_steps}/{self.max_steps}",
                    termination_reason=LoopTerminationReason.MAX_STEPS,
                )
            await self._fail(
                LoopTerminationReason.MAX_STEPS,
                f"单轮达到 {self.max_steps} 个模型步骤上限",
                source="max_steps",
            )
        except asyncio.CancelledError:
            host._turn_abort_signal.cancel()
            host._repair_after_cancel()
            await state.fail(
                LoopTerminationReason.CANCELLED,
                "turn cancelled",
            )
            await host._emit(RunCancelled(turn_id=self.turn_id))
            raise
        except Exception as exc:
            if state.phase is LoopPhase.FAILED:
                raise
            await self._fail(
                LoopTerminationReason.FAILED,
                f"{type(exc).__name__}: {exc}",
                source="loop",
                exception=exc,
            )
        raise AssertionError("Loop runner exhausted without terminal state")

    async def _handle_recovery(self, decision: RecoveryDecision) -> None:
        await record_recovery_decision(self.host, decision)
        await self.host._emit(RecoveryDecided(
            failure_class=FailureClass(decision.failure_class).value,
            strategy=RecoveryStrategy(decision.strategy).value,
            reason=decision.reason,
            retryable=decision.retryable,
            requires_replan=decision.requires_replan,
            requires_user=decision.requires_user,
            attempt=decision.attempt,
            evidence=decision.evidence,
        ))
        if not decision.retryable:
            await self._fail(
                LoopTerminationReason.NO_PROGRESS,
                decision.reason,
                source="recovery",
            )
        self.progress.reset_after_replan()

    async def _verify(self, text: str) -> str | None:
        host = self.host
        state = self.state
        terminal = await self._extension(
            ExtensionLifecyclePoint.VERIFY_BEFORE,
            metadata={"candidate_text": text[:2_000]},
        )
        if terminal is not None:
            return terminal
        attempt = await state.begin_verification(
            "model requested completion; running acceptance")
        result = await host._evaluate_acceptance(text)
        host.acceptance_result = result
        terminal = await self._extension(
            ExtensionLifecyclePoint.VERIFY_AFTER,
            metadata={
                "attempt": attempt,
                "acceptance_passed": bool(result is not None and result.passed),
                "acceptance_status": (
                    result.status.value if result is not None else ""),
            },
        )
        if terminal is not None:
            return terminal
        contract = host.agent_contract
        if result is None or result.passed or contract is None:
            terminal = await self._extension(
                ExtensionLifecyclePoint.TASK_COMPLETE,
                metadata={"candidate_text": text[:2_000]},
            )
            if terminal is not None:
                return terminal
            await state.complete(text)
            await host._emit(RunFinished(text, turn_id=self.turn_id))
            return text
        gaps = contract_verification_gaps(result)
        retryable = (
            contract is not None
            and contract.enforce_acceptance
            and result.status is AcceptanceStatus.FAILED
            and attempt < contract.max_verification_attempts
            and all(gap.retryable for gap in gaps)
        )
        if retryable:
            await host._record_verification_gaps(gaps, attempt)
            await self._phase(
                LoopPhase.PREPARE,
                f"verification gap {attempt}; returning to Prepare",
            )
            return None
        if result.status is AcceptanceStatus.NEEDS_REVIEW:
            answer = await host.request_user_input(
                "任务已完成但验收需要人工确认。是否批准完成？",
                ("approve", "reject"),
                allow_free_text=False,
            )
            if answer is not None and answer.casefold() in {
                "approve", "approved", "yes", "y", "批准", "通过",
            }:
                host.acceptance_result = replace(
                    result,
                    status=AcceptanceStatus.PASSED,
                    reason="human review approved",
                    metadata={
                        **dict(result.metadata),
                        "human_review": "approved",
                    },
                )
                terminal = await self._extension(
                    ExtensionLifecyclePoint.TASK_COMPLETE,
                    metadata={
                        "candidate_text": text[:2_000],
                        "human_review": "approved",
                    },
                )
                if terminal is not None:
                    return terminal
                await state.complete(text)
                await host._emit(RunFinished(text, turn_id=self.turn_id))
                return text
            await self._fail(
                LoopTerminationReason.REVIEW_REQUIRED,
                f"验收需要人工复核: {result.reason}",
                source="acceptance",
                exception=AcceptanceReviewRequired(result),
            )
        await self._fail(
            LoopTerminationReason.ACCEPTANCE_FAILED,
            f"验收未通过: {result.reason}",
            source="acceptance",
            exception=AcceptanceGateError(result),
        )
        return None

    async def _phase(self, phase: LoopPhase, reason: str) -> None:
        transition = await self.state.transition(
            phase,
            reason,
            step=self.state.step,
        )
        await self.host._emit(QueryPhaseChanged(
            phase.value,
            reason,
        ))
        _ = transition

    async def _extension(
        self,
        point: ExtensionLifecyclePoint,
        *,
        metadata: dict[str, object] | None = None,
        enforce: bool = True,
    ) -> str | None:
        host = self.host
        pipeline = host.middleware_pipeline
        if pipeline is None:
            return None
        application = host.application_request
        plan_control = host._turn_tool_context.plan_control
        budget = host.budget_ledger
        context = MiddlewareContext(
            turn_id=self.turn_id,
            phase=self.state.phase.value,
            step=self.state.step,
            lifecycle_point=point,
            goal=application.goal if application is not None else "",
            permission_mode=(
                host._turn_tool_context.runtime.permission_mode.value),
            plan=(
                plan_control.as_dict()
                if plan_control is not None else {}),
            budget=(
                budget.snapshot().as_record()
                if budget is not None else {}),
            observation_ids=tuple(
                item.observation_id for item in self.state.observations),
            metadata=metadata or {},
        )
        result = await pipeline.evaluate(point, context)
        applied = await host._apply_middleware_result(result)
        if result.evaluations or applied:
            await host._emit(MiddlewareEvaluated(
                lifecycle_point=point.value,
                action=result.action.value,
                reason=result.reason,
                evaluations=tuple(
                    item.to_dict() for item in result.evaluations),
                applied_patches=tuple(
                    patch.to_dict() for patch in applied),
            ))
        if not enforce:
            return None
        if result.action is MiddlewareAction.DENY:
            await self._fail(
                LoopTerminationReason.POLICY_DENIED,
                result.reason or f"middleware denied at {point.value}",
                source="middleware",
            )
        if result.action is MiddlewareAction.SHORT_CIRCUIT:
            text = result.short_circuit_text
            if self.state.phase is not LoopPhase.VERIFY:
                await self._phase(
                    LoopPhase.VERIFY,
                    f"middleware short-circuit at {point.value}",
                )
            await self.state.complete(text)
            await host._emit(RunFinished(text, turn_id=self.turn_id))
            return text
        return None

    async def _fail(
        self,
        reason: LoopTerminationReason,
        error: str,
        *,
        source: str,
        exception: BaseException | None = None,
        unfinished: UnfinishedManifest | None = None,
    ) -> None:
        await self._extension(
            ExtensionLifecyclePoint.TASK_FAIL,
            metadata={"source": source, "error": error[:2_000]},
            enforce=False,
        )
        await self.host._record_turn_error(error, source)
        terminated = await self.state.fail(
            reason,
            error,
            metadata={"source": source},
            unfinished=unfinished,
        )
        await self.host._emit(RunFailed(
            reason=reason.value,
            termination=reason.value,
            turn_id=self.turn_id,
            detail=error,
            unfinished=(
                unfinished.as_record() if unfinished is not None else {}),
        ))
        raise (exception or terminated)

    async def _budget_partial(
        self,
        snapshot: BudgetSnapshot,
        *,
        reason: str,
        detail: str,
        termination_reason: LoopTerminationReason,
    ) -> None:
        host = self.host
        manifest = build_unfinished_manifest(
            cast(BudgetControlHost, host),
            snapshot,
            reason=reason + (f": {detail}" if detail else ""),
        )
        await self._fail(
            termination_reason,
            partial_delivery_text(manifest),
            source="budget",
            unfinished=manifest,
        )


__all__ = ["TurnLoopRunner"]
