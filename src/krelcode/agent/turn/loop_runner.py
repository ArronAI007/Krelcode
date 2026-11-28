"""Explicit five-stage Agent Loop runner."""

from __future__ import annotations

import asyncio

from krelcode.contracts.acceptance import AcceptanceStatus
from krelcode.contracts.loop import LoopPhase, LoopTerminationReason
from krelcode.core.events import (
    QueryPhaseChanged,
    RunCancelled,
    RunFailed,
    RunFinished,
    StepStarted,
)
from .acceptance import AcceptanceGateError, AcceptanceReviewRequired
from .contract_gate import contract_verification_gaps
from .loop_state import AgentLoopStateMachine
from .model_turn import finish_reason_failure
from .runtime_ports import LoopRunnerHostPort


class TurnLoopRunner:
    """Drive Prepare → Model → Act → Observe → Verify with explicit states."""

    def __init__(
        self,
        host: LoopRunnerHostPort,
        *,
        turn_id: str,
        max_steps: int,
        token_budget: int | None,
    ) -> None:
        self.host = host
        self.turn_id = turn_id
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.state = AgentLoopStateMachine(turn_id, host._emit)

    async def run(self) -> str:
        host = self.host
        state = self.state
        await host._bind_loop_state_machine(state)
        host._failure_counts = {}
        host._token_total = 0
        try:
            for step in range(1, self.max_steps + 1):
                await state.set_step(step)
                if host.step_gate is not None:
                    await host.step_gate()
                await host._inject_steering()
                if (
                    self.token_budget is not None
                    and host._token_total > self.token_budget
                ):
                    await self._fail(
                        LoopTerminationReason.BUDGET_EXHAUSTED,
                        f"token 预算耗尽（已用 {host._token_total} > "
                        f"预算 {self.token_budget}）",
                        source="budget",
                    )
                await host._emit(StepStarted(step))
                await self._phase(
                    LoopPhase.MODEL,
                    f"模型判断下一步（Step {step}）",
                )
                llm_turn = await host._llm_turn(step)
                assistant = llm_turn.message
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
                if state.phase is LoopPhase.MODEL:
                    await self._phase(LoopPhase.ACT, "tool actions requested")
                abort_reason = await host._apply_tool_results(
                    assistant.tool_calls, llm_turn.tool_results)
                if abort_reason:
                    await self._fail(
                        LoopTerminationReason.LOOP_DETECTED,
                        abort_reason,
                        source="loop_guard",
                    )
                if state.phase is LoopPhase.ACT:
                    await self._phase(
                        LoopPhase.OBSERVE,
                        "tool observations collected",
                    )
                await self._phase(
                    LoopPhase.PREPARE,
                    "observations applied; rebuilding task view",
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

    async def _verify(self, text: str) -> str | None:
        host = self.host
        state = self.state
        attempt = await state.begin_verification(
            "model requested completion; running acceptance")
        result = await host._evaluate_acceptance(text)
        host.acceptance_result = result
        contract = host.agent_contract
        if result is None or result.passed or contract is None:
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

    async def _fail(
        self,
        reason: LoopTerminationReason,
        error: str,
        *,
        source: str,
        exception: BaseException | None = None,
    ) -> None:
        await self.host._record_turn_error(error, source)
        terminated = await self.state.fail(
            reason,
            error,
            metadata={"source": source},
        )
        await self.host._emit(RunFailed(
            reason=reason.value,
            termination=reason.value,
            turn_id=self.turn_id,
            detail=error,
        ))
        raise (exception or terminated)


__all__ = ["TurnLoopRunner"]
