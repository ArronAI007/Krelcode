"""Conservative runtime admission policy for subagent delegation value."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.delegation import (
    DelegationContract,
    DelegationFailurePolicy,
    DelegationKind,
)


@dataclass(frozen=True)
class DelegationValueAssessment:
    allowed: bool
    reasons: tuple[str, ...] = ()


def assess_delegation_value(
    contract: DelegationContract,
) -> DelegationValueAssessment:
    """Reject trivial structured delegation without isolation/specialization."""
    if contract.metadata.get("legacy_task_compat"):
        return DelegationValueAssessment(True, ("legacy_task_compat",))
    benefits = [
        reason for reason, present in (
            ("context_isolation", bool(
                contract.scope.read_paths
                or contract.scope.write_paths
                or contract.known_context.facts
            )),
            ("capability_specialization", (
                contract.capabilities.definition != "general-purpose"
            )),
            ("parallel_candidate", bool(
                contract.metadata.get("parallelizable"))),
            ("bounded_output", bool(
                contract.output.schema
                or contract.output.required_fields
            )),
            ("failure_governance", (
                DelegationFailurePolicy(contract.failure_policy)
                is not DelegationFailurePolicy.BEST_EFFORT
                or contract.retry_policy.max_retries > 0
            )),
            ("handoff", (
                DelegationKind(contract.relation_kind)
                is DelegationKind.HANDOFF
            )),
        ) if present
    ]
    if len(contract.objective) < 24 and not benefits:
        return DelegationValueAssessment(
            False,
            (
                "delegation_value_unclear",
                "trivial_or_highly_shared_task_should_run_locally",
            ),
        )
    return DelegationValueAssessment(True, tuple(benefits))


__all__ = ["DelegationValueAssessment", "assess_delegation_value"]
