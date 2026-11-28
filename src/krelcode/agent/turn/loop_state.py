"""Validated explicit state machine for one Agent Loop turn."""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from krelcode.contracts.loop import (
    LoopPhase,
    LoopTermination,
    LoopTerminationReason,
    LoopTransition,
    Observation,
    VerificationGap,
    WaitCondition,
    WaitKind,
)
from krelcode.core.events import AgentEvent, LoopPhaseChanged, ObservationRecorded

EmitEvent = Callable[[AgentEvent], Awaitable[None]]

_ALLOWED: dict[LoopPhase, frozenset[LoopPhase]] = {
    LoopPhase.PREPARE: frozenset({
        LoopPhase.MODEL, LoopPhase.WAITING, LoopPhase.FAILED,
    }),
    LoopPhase.MODEL: frozenset({
        LoopPhase.ACT, LoopPhase.VERIFY, LoopPhase.WAITING,
        LoopPhase.FAILED,
    }),
    LoopPhase.ACT: frozenset({
        LoopPhase.OBSERVE, LoopPhase.WAITING, LoopPhase.FAILED,
    }),
    LoopPhase.OBSERVE: frozenset({
        LoopPhase.PREPARE, LoopPhase.ACT, LoopPhase.FAILED,
    }),
    LoopPhase.VERIFY: frozenset({
        LoopPhase.COMPLETED, LoopPhase.PREPARE, LoopPhase.WAITING,
        LoopPhase.FAILED,
    }),
    LoopPhase.WAITING: frozenset({
        LoopPhase.PREPARE, LoopPhase.MODEL, LoopPhase.ACT,
        LoopPhase.VERIFY, LoopPhase.FAILED,
    }),
    LoopPhase.COMPLETED: frozenset(),
    LoopPhase.FAILED: frozenset(),
}


class LoopTransitionError(RuntimeError):
    """An invalid explicit Loop transition was requested."""


class AgentLoopTerminated(RuntimeError):
    """Terminal Loop outcome represented as a runtime exception."""

    def __init__(self, termination: LoopTermination) -> None:
        message = termination.error or (
            "Agent Loop terminated: "
            f"{LoopTerminationReason(termination.reason).value}")
        super().__init__(message)
        self.termination = termination


class AgentLoopStateMachine:
    def __init__(
        self,
        turn_id: str,
        emit: EmitEvent,
    ) -> None:
        self.turn_id = turn_id
        self.emit = emit
        self.phase = LoopPhase.PREPARE
        self.step = 0
        self.verification_attempts = 0
        self.resume_phase = LoopPhase.PREPARE
        self.transitions: list[LoopTransition] = []
        self.observations: list[Observation] = []
        self.termination: Optional[LoopTermination] = None

    async def transition(
        self,
        phase: LoopPhase | str,
        reason: str,
        *,
        step: Optional[int] = None,
        observation: Optional[Observation] = None,
        waiting: Optional[WaitCondition] = None,
        gap: Optional[VerificationGap] = None,
    ) -> LoopTransition:
        target = LoopPhase(phase)
        if step is not None:
            self.step = max(0, int(step))
        if target is self.phase:
            return LoopTransition(
                turn_id=self.turn_id,
                previous_phase=self.phase,
                phase=target,
                reason=reason,
                step=self.step,
                observation_id=(
                    observation.observation_id
                    if observation is not None else ""),
                waiting=waiting,
                gap=gap,
            )
        if target not in _ALLOWED[self.phase]:
            raise LoopTransitionError(
                f"非法 Loop 转场: {self.phase.value} -> {target.value}")
        transition = LoopTransition(
            turn_id=self.turn_id,
            previous_phase=self.phase,
            phase=target,
            reason=reason,
            step=self.step,
            observation_id=(
                observation.observation_id
                if observation is not None else ""),
            waiting=waiting,
            gap=gap,
        )
        if target is LoopPhase.WAITING:
            self.resume_phase = self.phase
        self.phase = target
        self.transitions.append(transition)
        await self.emit(LoopPhaseChanged(
            phase=target.value,
            previous_phase=transition.previous_phase.value,
            reason=transition.reason,
            step=transition.step,
            transition=transition.as_record(),
        ))
        return transition

    async def set_step(self, step: int) -> None:
        self.step = max(0, int(step))

    async def observe(
        self,
        observation: Observation,
        reason: str = "observation recorded",
    ) -> None:
        await self.record(observation)
        await self.transition(
            LoopPhase.OBSERVE,
            reason,
            observation=observation,
        )

    async def record(self, observation: Observation) -> None:
        if any(
            item.observation_id == observation.observation_id
            for item in self.observations
        ):
            return
        self.observations.append(observation)
        await self.emit(ObservationRecorded(observation.as_record()))

    async def begin_verification(self, reason: str) -> int:
        self.verification_attempts += 1
        await self.transition(
            LoopPhase.VERIFY,
            reason,
            step=self.step,
        )
        return self.verification_attempts

    async def wait(
        self,
        condition: WaitCondition,
    ) -> None:
        await self.transition(
            LoopPhase.WAITING,
            condition.reason
            or f"waiting for {WaitKind(condition.kind).value}",
            waiting=condition,
        )

    async def resume(self, reason: str = "wait condition resolved") -> None:
        await self.transition(
            self.resume_phase,
            reason,
            step=self.step,
        )

    async def fail(
        self,
        reason: LoopTerminationReason | str,
        error: str,
        *,
        metadata: Optional[dict[str, object]] = None,
    ) -> AgentLoopTerminated:
        termination = LoopTermination(
            reason=reason,
            error=error,
            turn_id=self.turn_id,
            phase=LoopPhase.FAILED,
            metadata=dict(metadata or {}),
        )
        self.termination = termination
        if self.phase is not LoopPhase.FAILED:
            await self.transition(
                LoopPhase.FAILED,
                error,
            )
        return AgentLoopTerminated(termination)

    async def complete(self, final_text: str) -> LoopTermination:
        await self.transition(
            LoopPhase.COMPLETED,
            "verification passed",
        )
        termination = LoopTermination(
            reason=LoopTerminationReason.COMPLETED,
            final_text=final_text,
            turn_id=self.turn_id,
            phase=LoopPhase.COMPLETED,
        )
        self.termination = termination
        return termination

    def as_record(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "step": self.step,
            "verification_attempts": self.verification_attempts,
            "transition_count": len(self.transitions),
            "observation_count": len(self.observations),
            "termination": (
                self.termination.as_record()
                if self.termination is not None else None),
        }


__all__ = [
    "AgentLoopStateMachine",
    "AgentLoopTerminated",
    "LoopTransitionError",
]
