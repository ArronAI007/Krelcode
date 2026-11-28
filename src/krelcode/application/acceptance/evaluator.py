"""Deterministic acceptance evaluation over the completed turn."""

from __future__ import annotations

import hashlib
from typing import Iterable, Sequence

from ...contracts.acceptance import (
    AcceptanceCheck,
    AcceptanceEvidence,
    AcceptanceResult,
    AcceptanceStatus,
)
from ...contracts.application import ApplicationRequest
from ...contracts.agent import AgentContract, DeliverableKind


class DefaultAcceptanceEvaluator:
    """Evaluate required evidence without asking another model."""

    def evaluate(
        self,
        request: ApplicationRequest,
        final_response: str,
        evidence: Sequence[AcceptanceEvidence],
        contract: AgentContract | None = None,
    ) -> AcceptanceResult:
        response = str(final_response or "").strip()
        evidence_items = tuple(evidence)
        response_check = AcceptanceCheck(
            criterion="non_empty_final_response",
            passed=bool(response),
            reason="" if response else "最终回答为空",
            evidence=(AcceptanceEvidence(
                kind="model_response",
                source="agent.final",
                passed=bool(response),
                summary=f"{len(response)} chars",
            ),),
        )
        evidence_by_kind = {
            item.kind: item for item in evidence_items
        }
        required_checks = []
        needs_review = False
        for requirement in request.acceptance.required_evidence:
            normalized = requirement.strip().lower()
            passed = _required_evidence_passed(
                normalized, response=bool(response),
                evidence=evidence_by_kind)
            if normalized not in {
                "model-or-tool-result", "model-response", "model_result",
                "tool-result", "tool_result", "test", "tests",
                "test-result", "test_result",
            }:
                needs_review = True
            required_checks.append(AcceptanceCheck(
                criterion=f"evidence:{requirement}",
                passed=passed,
                reason="" if passed else _evidence_failure_reason(
                    requirement, evidence_by_kind),
                evidence=tuple(evidence_by_kind.values()),
            ))
        criterion_checks = tuple(
            AcceptanceCheck(
                criterion=criterion,
                passed=_criterion_passed(
                    criterion, response=bool(response),
                    evidence=evidence_by_kind),
                reason="" if _criterion_passed(
                    criterion, response=bool(response),
                    evidence=evidence_by_kind)
                else f"验收条件未满足: {criterion}",
            )
            for criterion in request.acceptance.criteria
        )
        checks = (response_check, *required_checks, *criterion_checks)
        if contract is not None:
            deliverable_checks = tuple(
                _deliverable_check(requirement, response, evidence_items)
                for requirement in contract.deliverables.requirements
            )
            checks = (*checks, *deliverable_checks)
        passed = all(check.passed for check in checks)
        if passed:
            status = AcceptanceStatus.PASSED
            reason = ""
        elif needs_review and request.acceptance.allow_manual_review:
            status = AcceptanceStatus.NEEDS_REVIEW
            reason = "部分验收证据需要人工确认"
        else:
            status = AcceptanceStatus.FAILED
            reason = "; ".join(
                check.reason for check in checks
                if not check.passed and check.reason)
        return AcceptanceResult(
            status=status,
            request_goal=request.goal,
            contract=request.acceptance,
            checks=checks,
            evidence=evidence_items,
            reason=reason,
            final_response_hash=hashlib.sha256(
                response.encode("utf-8")).hexdigest(),
            metadata={
                "trigger": request.trigger.value,
                "task_shape": request.task_shape.value,
                "contract_id": (
                    contract.contract_id if contract is not None else ""),
                "contract_hash": (
                    contract.contract_hash if contract is not None else ""),
            },
        )


def _required_evidence_passed(
    requirement: str,
    *,
    response: bool,
    evidence: dict[str, AcceptanceEvidence],
) -> bool:
    if requirement in {"model-or-tool-result", "model_result"}:
        return response or any(item.passed for item in evidence.values())
    if requirement in {"model-response", "model_response"}:
        return response
    if requirement in {"tool-result", "tool_result"}:
        item = evidence.get("tool-result")
        return bool(item is not None and item.passed and item.fresh)
    if requirement in {"test", "tests", "test-result", "test_result"}:
        item = evidence.get("test-result")
        return bool(item is not None and item.passed and item.fresh)
    return False


def _criterion_passed(
    criterion: str,
    *,
    response: bool,
    evidence: dict[str, AcceptanceEvidence],
) -> bool:
    normalized = criterion.casefold()
    if not response:
        return False
    if any(term in normalized for term in ("test", "测试", "验证")):
        item = evidence.get("test-result")
        return bool(item is not None and item.passed and item.fresh)
    if any(term in normalized for term in ("tool", "工具", "证据")):
        return bool(evidence)
    return True


def _evidence_failure_reason(
    requirement: str,
    evidence: dict[str, AcceptanceEvidence],
) -> str:
    normalized = requirement.strip().lower()
    kind = (
        "test-result"
        if normalized in {"test", "tests", "test-result", "test_result"}
        else "tool-result"
    )
    item = evidence.get(kind)
    if item is not None and item.passed and not item.fresh:
        return item.freshness_reason or f"验收证据已过期: {requirement}"
    return f"缺少验收证据: {requirement}"


def _deliverable_check(
    requirement: object,
    response: str,
    evidence: Sequence[AcceptanceEvidence],
) -> AcceptanceCheck:
    deliverable_id = str(getattr(requirement, "id", "") or "")
    kind = DeliverableKind(getattr(requirement, "kind", "response"))
    matched = tuple(
        item for item in evidence
        if item.deliverable_id == deliverable_id
    )
    if not matched and not bool(getattr(requirement, "required", True)):
        return AcceptanceCheck(
            criterion=f"deliverable:{deliverable_id}",
            passed=True,
            reason="可选交付物未提供",
        )
    if kind is DeliverableKind.RESPONSE:
        passed = bool(response.strip()) and any(
            item.passed and item.fresh for item in matched)
    else:
        passed = any(item.passed and item.fresh for item in matched)
        if bool(getattr(requirement, "artifact_required", False)):
            passed = passed and any(
                item.passed
                and item.fresh
                and item.artifact is not None
                and bool(item.digest)
                for item in matched
            )
    return AcceptanceCheck(
        criterion=f"deliverable:{deliverable_id}",
        passed=passed,
        reason="" if passed else f"交付物未完成: {deliverable_id}",
        evidence=matched,
    )


__all__ = [
    "DefaultAcceptanceEvaluator",
]
