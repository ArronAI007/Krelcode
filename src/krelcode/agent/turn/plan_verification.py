"""Turn plan control state into acceptance checks."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from ...contracts.acceptance import (
    AcceptanceCheck,
    AcceptanceEvidence,
    AcceptanceResult,
    AcceptanceStatus,
)
from ...contracts.plan import (
    PlanApprovalStatus,
    PlanControl,
    PlanControlMode,
    PlanNodeStatus,
)


def apply_plan_completion(
    result: AcceptanceResult,
    plan_control: Optional[PlanControl],
) -> AcceptanceResult:
    if plan_control is None or not plan_control.nodes:
        return result
    checks: list[AcceptanceCheck] = []
    evidence: list[AcceptanceEvidence] = []
    if (
        PlanControlMode(plan_control.mode) is PlanControlMode.PLAN_MODE
        and PlanApprovalStatus(plan_control.approval)
        is not PlanApprovalStatus.APPROVED
    ):
        checks.append(AcceptanceCheck(
            criterion="plan:approval",
            passed=False,
            reason="Plan Mode 尚未获得执行批准",
        ))
    for node in plan_control.nodes:
        status = PlanNodeStatus(node.status)
        if status is PlanNodeStatus.COMPLETED:
            passed = (
                not node.acceptance_checks
                or (bool(node.evidence_refs) and bool(result.evidence))
            )
            reason = (
                "" if passed
                else f"已完成节点缺少验收证据: {node.id}"
            )
        elif status is PlanNodeStatus.CANCELLED:
            passed = True
            reason = ""
        else:
            passed = False
            reason = f"Plan 节点尚未完成: {node.id} ({status.value})"
        check = AcceptanceCheck(
            criterion=f"plan:{node.id}",
            passed=passed,
            reason=reason,
        )
        checks.append(check)
        evidence.append(AcceptanceEvidence(
            kind="plan-node",
            source="agent.plan",
            passed=passed,
            summary=f"{node.id}: {status.value}",
            sequence=0,
            scope=tuple(node.evidence_refs),
            fresh=True,
            metadata={
                "node_id": node.id,
                "status": status.value,
                "acceptance_checks": list(node.acceptance_checks),
                "evidence_refs": list(node.evidence_refs),
                "plan_revision": plan_control.revision.revision_id,
            },
        ))
    failed = tuple(item for item in checks if not item.passed)
    acceptance_status = (
        AcceptanceStatus.FAILED
        if failed and result.status is AcceptanceStatus.PASSED
        else result.status
    )
    reason = (
        result.reason
        if result.status is not AcceptanceStatus.PASSED
        else "; ".join(item.reason for item in failed)
    )
    return replace(
        result,
        status=acceptance_status,
        checks=(*result.checks, *checks),
        evidence=(*result.evidence, *evidence),
        reason=reason,
        metadata={
            **dict(result.metadata),
            "plan_revision": plan_control.revision.revision_id,
            "plan_approval": PlanApprovalStatus(plan_control.approval).value,
        },
    )


__all__ = ["apply_plan_completion"]
