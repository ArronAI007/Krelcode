"""Apply risk-selected external verification levels to acceptance."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Optional

from ...contracts.acceptance import AcceptanceResult, AcceptanceStatus
from ...contracts.application import ApplicationRequest
from ...contracts.verification import (
    VerificationLevel,
    VerificationPolicy,
    VerificationRequest,
    VerifierPort,
)


async def apply_verification_policy(
    *,
    result: AcceptanceResult,
    request: ApplicationRequest,
    turn_id: str,
    final_response: str,
    ports: Optional[
        Mapping[VerificationLevel | str, VerifierPort]
    ] = None,
) -> AcceptanceResult:
    policy = VerificationPolicy.for_request(request)
    resolved_ports = {
        VerificationLevel(key): value
        for key, value in (ports or {}).items()
    }
    checks = list(result.checks)
    evidence = list(result.evidence)
    reasons: list[str] = []
    verifier_results: list[dict[str, object]] = []
    status = result.status
    external_levels = (
        VerificationLevel.INDEPENDENT_MODEL,
        VerificationLevel.HUMAN,
    )
    for level in external_levels:
        if level not in policy.required_levels:
            continue
        verifier = resolved_ports.get(level)
        if verifier is None:
            status = (
                AcceptanceStatus.NEEDS_REVIEW
                if status is AcceptanceStatus.PASSED else status
            )
            reasons.append(f"{level.value} verifier is not configured")
            continue
        verification = await verifier.verify(VerificationRequest(
            turn_id=turn_id,
            level=level,
            goal=request.goal,
            final_response=final_response,
            checks=result.checks,
            evidence=result.evidence,
            policy=policy,
            metadata={
                "contract_goal": request.goal,
                "acceptance_status": result.status.value,
            },
        ))
        checks.extend(verification.checks)
        evidence.extend(verification.evidence)
        verifier_results.append({
            "level": VerificationLevel(verification.level).value,
            "verifier_id": verification.verifier_id,
            "status": AcceptanceStatus(verification.status).value,
            "reason": verification.reason,
        })
        if verification.status is AcceptanceStatus.FAILED:
            status = AcceptanceStatus.FAILED
        elif (
            verification.status is AcceptanceStatus.NEEDS_REVIEW
            and status is AcceptanceStatus.PASSED
        ):
            status = AcceptanceStatus.NEEDS_REVIEW
        if verification.reason:
            reasons.append(verification.reason)
    metadata = {
        **dict(result.metadata),
        "verification_policy": policy.to_dict(),
        "verifier_chain": [
            item.value for item in policy.required_levels
        ],
        "configured_verifiers": [
            item.value for item in resolved_ports
        ],
        "verifier_results": verifier_results,
    }
    return replace(
        result,
        status=status,
        checks=tuple(checks),
        evidence=tuple(evidence),
        reason=(
            result.reason
            if result.status is not AcceptanceStatus.PASSED
            else "; ".join(reasons)
        ),
        metadata=metadata,
    )


__all__ = ["apply_verification_policy"]
