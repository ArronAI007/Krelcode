"""State-machine callbacks shared by AgentLoop and tool execution."""

from __future__ import annotations

from typing import Optional, Protocol

from krelcode.contracts.loop import (
    LoopPhase,
    Observation,
    VerificationGap,
    WaitCondition,
    WaitKind,
)
from krelcode.core.events import (
    AgentEvent,
    QueryPhaseChanged,
)
from krelcode.core.messages import Message, ToolCall
from .loop_state import AgentLoopStateMachine


class LoopControlHost(Protocol):
    @property
    def loop_state(self) -> Optional[AgentLoopStateMachine]: ...

    @loop_state.setter
    def loop_state(self, value: Optional[AgentLoopStateMachine]) -> None: ...

    @property
    def observations(self) -> list[Observation]: ...

    @property
    def last_observation(self) -> Optional[Observation]: ...

    @last_observation.setter
    def last_observation(self, value: Optional[Observation]) -> None: ...

    _current_turn_id: str

    @property
    def history(self) -> list[Message]: ...

    async def _emit(self, event: AgentEvent) -> None: ...


async def bind_loop_state(
    host: LoopControlHost,
    state: AgentLoopStateMachine,
) -> None:
    host.loop_state = state


async def before_tool_execution(
    host: LoopControlHost,
    tool_call: ToolCall,
) -> None:
    state = host.loop_state
    if state is None:
        return
    if state.phase in {LoopPhase.MODEL, LoopPhase.OBSERVE}:
        await state.transition(
            LoopPhase.ACT,
            f"executing {tool_call.name}",
            step=state.step,
        )
        await host._emit(QueryPhaseChanged(
            "act", f"执行工具 {tool_call.name}"))


async def record_observation(
    host: LoopControlHost,
    observation: Observation,
) -> None:
    if not any(
        item.observation_id == observation.observation_id
        for item in host.observations
    ):
        host.observations.append(observation)
    host.last_observation = observation
    state = host.loop_state
    if state is None:
        return
    if state.phase is LoopPhase.ACT:
        await state.observe(observation)
        await host._emit(QueryPhaseChanged(
            "observe", observation.summary))
    else:
        await state.record(observation)


async def enter_waiting(
    host: LoopControlHost,
    condition: WaitCondition,
) -> None:
    state = host.loop_state
    if state is None:
        return
    await state.wait(condition)
    await host._emit(QueryPhaseChanged(
        "waiting",
        condition.reason or WaitKind(condition.kind).value,
    ))


async def resume_waiting(
    host: LoopControlHost,
    request_id: str,
) -> None:
    state = host.loop_state
    if state is None or state.phase is not LoopPhase.WAITING:
        return
    await state.resume(f"wait condition resolved: {request_id}")
    await host._emit(QueryPhaseChanged(
        state.phase.value, "wait condition resolved"))


async def record_verification_gaps(
    host: LoopControlHost,
    gaps: tuple[VerificationGap, ...],
    attempt: int,
    *,
    working_context: object,
) -> None:
    lines = [
        "[Verification Gap]",
        "The previous completion claim did not pass acceptance.",
    ]
    upsert = getattr(working_context, "upsert", None)
    for index, gap in enumerate(gaps, 1):
        observation = Observation(
            observation_id=(
                f"obs-verification-{host._current_turn_id}-{attempt}-{index}"),
            turn_id=host._current_turn_id,
            source="system",
            kind="verification.gap",
            status="error",
            summary=gap.message,
            data=gap.as_record(),
            trust="trusted",
        )
        await record_observation(host, observation)
        if callable(upsert):
            upsert(
                "verification_gap",
                gap.message,
                id=f"verification:{attempt}:{index}",
                source="verification",
                metadata={
                    "criterion": gap.criterion,
                    "code": gap.code,
                    "observation_id": observation.observation_id,
                },
            )
        lines.append(
            f"- {gap.criterion}: {gap.message}"
            + (f" Next: {gap.suggested_action}"
               if gap.suggested_action else "")
        )
    host.history.append(Message.user("\n".join(lines)))


__all__ = [
    "LoopControlHost",
    "before_tool_execution",
    "bind_loop_state",
    "enter_waiting",
    "record_observation",
    "record_verification_gaps",
    "resume_waiting",
]
