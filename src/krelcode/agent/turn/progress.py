"""Detect stalled execution and turn it into an explicit recovery decision."""

from __future__ import annotations

from collections import Counter
from typing import Mapping, Optional, Protocol, Sequence

from ...contracts.loop import (
    Observation,
    ObservationSource,
    ObservationStatus,
)
from ...contracts.plan import PlanControl
from ...contracts.recovery import (
    FailureClass,
    RecoveryDecision,
    RecoveryStrategy,
    Repairability,
)
from ...core.messages import Message


class RecoveryHostPort(Protocol):
    history: list[Message]
    _current_turn_id: str

    async def _record_observation(self, observation: Observation) -> None: ...


class ProgressMonitor:
    """Track repeated actions, stale plans and absence of new task facts."""

    def __init__(
        self,
        *,
        window: int = 8,
        repeated_error_threshold: int = 3,
        no_fact_threshold: int = 4,
        stagnant_plan_threshold: int = 4,
        max_replans: int = 1,
    ) -> None:
        self.window = max(2, int(window))
        self.repeated_error_threshold = max(
            2, int(repeated_error_threshold))
        self.no_fact_threshold = max(2, int(no_fact_threshold))
        self.stagnant_plan_threshold = max(
            2, int(stagnant_plan_threshold))
        self.max_replans = max(0, int(max_replans))
        self._seen_facts: set[str] = set()
        self._replans = 0
        self._empty_fact_steps = 0
        self._plan_signature: tuple[object, ...] = ()
        self._plan_unchanged_steps = 0

    def inspect(
        self,
        *,
        step: int,
        observations: Sequence[Observation],
        plan_control: Optional[PlanControl],
        abort_reason: str = "",
    ) -> Optional[RecoveryDecision]:
        recent = tuple(observations[-self.window:])
        facts = _new_fact_digests(recent, self._seen_facts)
        if facts:
            self._empty_fact_steps = 0
        else:
            self._empty_fact_steps += 1

        plan_signature = _plan_signature(plan_control)
        if plan_signature != self._plan_signature:
            self._plan_signature = plan_signature
            self._plan_unchanged_steps = 0
        elif plan_signature:
            self._plan_unchanged_steps += 1

        repeated = _repeated_error_signature(
            recent, self.repeated_error_threshold)
        stagnant_plan = (
            bool(plan_signature)
            and self._plan_unchanged_steps >= self.stagnant_plan_threshold
            and self._empty_fact_steps >= self.no_fact_threshold
        )
        no_facts = self._empty_fact_steps >= self.no_fact_threshold
        if not abort_reason and not repeated and not no_facts and not stagnant_plan:
            return None

        if repeated:
            failure = FailureClass.REPEATED_EXPLORATION
            reason = (
                "repeated failing action observed "
                f"{self.repeated_error_threshold} times: {repeated}"
            )
        elif stagnant_plan:
            failure = FailureClass.NO_PROGRESS
            reason = (
                "plan and workspace facts have not changed for "
                f"{self.stagnant_plan_threshold} steps"
            )
        else:
            failure = FailureClass.NO_PROGRESS
            reason = (
                "no new task fact was observed for "
                f"{self.no_fact_threshold} steps"
            )
        self._replans += 1
        allowed = self._replans <= self.max_replans
        return RecoveryDecision(
            failure_class=failure,
            strategy=(
                RecoveryStrategy.REPLAN
                if allowed else RecoveryStrategy.ESCALATE
            ),
            repairability=(
                Repairability.REPLAN
                if allowed else Repairability.USER_ACTION
            ),
            reason=abort_reason or reason,
            retryable=allowed,
            requires_replan=allowed,
            requires_user=not allowed,
            attempt=self._replans,
            evidence={
                "step": max(0, int(step)),
                "empty_fact_steps": self._empty_fact_steps,
                "plan_unchanged_steps": self._plan_unchanged_steps,
                "plan_revision": (
                    plan_control.revision.revision_id
                    if plan_control is not None else ""),
                "repeated_signature": repeated,
                "observation_ids": [item.observation_id for item in recent],
            },
        )

    def reset_after_replan(self) -> None:
        self._empty_fact_steps = 0
        self._plan_unchanged_steps = 0


async def record_recovery_decision(
    host: RecoveryHostPort,
    decision: RecoveryDecision,
) -> Observation:
    record = decision.to_dict()
    observation = Observation(
        observation_id=(
            f"obs-recovery-{host._current_turn_id}-{decision.attempt}"),
        turn_id=host._current_turn_id,
        source=ObservationSource.SYSTEM,
        kind="recovery.decision",
        status=ObservationStatus.ERROR,
        summary=decision.reason,
        data=record,
        trust="trusted",
    )
    await host._record_observation(observation)
    host.history.append(Message.user(
        "[Recovery Decision]\n"
        f"- failure: {record['failure_class']}\n"
        f"- strategy: {record['strategy']}\n"
        f"- reason: {decision.reason}\n"
        "Revise the plan from this evidence. Do not repeat the same action "
        "without explaining what changed. Prefer a smaller verifiable step."
    ))
    return observation


def _new_fact_digests(
    observations: Sequence[Observation],
    seen: set[str],
) -> tuple[str, ...]:
    discovered: list[str] = []
    for item in observations:
        if ObservationStatus(item.status) is ObservationStatus.ERROR:
            continue
        if item.kind in {"recovery.decision", "verification.gap"}:
            continue
        digest = _observation_digest(item)
        if digest in seen:
            continue
        seen.add(digest)
        discovered.append(digest)
    return tuple(discovered)


def _observation_digest(observation: Observation) -> str:
    data = observation.data
    return ":".join((
        observation.kind,
        str(data.get("content_hash") or ""),
        str(data.get("result_data") or ""),
        observation.summary,
    ))


def _repeated_error_signature(
    observations: Sequence[Observation],
    threshold: int,
) -> str:
    signatures = [
        _error_signature(item)
        for item in observations
        if ObservationStatus(item.status) is ObservationStatus.ERROR
    ]
    if not signatures:
        return ""
    signature, count = Counter(signatures).most_common(1)[0]
    return signature if count >= threshold else ""


def _error_signature(observation: Observation) -> str:
    data = observation.data
    diagnostics = data.get("diagnostics")
    error_kind = (
        str(diagnostics.get("error_kind") or "")
        if isinstance(diagnostics, Mapping) else ""
    )
    return ":".join((
        observation.kind,
        error_kind,
        str(data.get("content_hash") or ""),
        observation.summary,
    ))


def _plan_signature(
    plan_control: Optional[PlanControl],
) -> tuple[object, ...]:
    if plan_control is None or not plan_control.nodes:
        return ()
    return (
        plan_control.revision.revision_id,
        tuple(item.id for item in plan_control.nodes),
        tuple(str(item.status) for item in plan_control.nodes),
        plan_control.completed_ids,
    )


__all__ = [
    "ProgressMonitor",
    "RecoveryHostPort",
    "record_recovery_decision",
]
