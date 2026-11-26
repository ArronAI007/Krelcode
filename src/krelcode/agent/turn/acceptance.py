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
from ...core.messages import Message


class DefaultAcceptanceEvaluator:
    """Evaluate required evidence without asking another model."""

    def evaluate(
        self,
        request: ApplicationRequest,
        final_response: str,
        evidence: Sequence[AcceptanceEvidence],
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
                reason="" if passed else f"缺少验收证据: {requirement}",
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
            },
        )


def acceptance_evidence_from_history(
    history: Iterable[Message],
) -> tuple[AcceptanceEvidence, ...]:
    evidence: list[AcceptanceEvidence] = []
    for message in history:
        if message.role != "tool":
            continue
        content = message.content or ""
        name = message.tool_name or "tool"
        evidence.append(AcceptanceEvidence(
            kind="tool-result",
            source=name,
            passed=not bool(message.tool_is_error),
            summary=f"{len(content)} chars",
            metadata={
                "tool_call_id": message.tool_call_id or "",
                "tool_name": name,
            },
        ))
        if name in {"run_command", "job_output"} and not message.tool_is_error:
            evidence.append(AcceptanceEvidence(
                kind="test-result",
                source=name,
                passed=True,
                summary=content[:240],
                metadata={"tool_call_id": message.tool_call_id or ""},
            ))
    return tuple(evidence)


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
        return evidence.get("tool-result", AcceptanceEvidence(
            "", "", False)).passed
    if requirement in {"test", "tests", "test-result", "test_result"}:
        return evidence.get("test-result", AcceptanceEvidence(
            "", "", False)).passed
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
        return evidence.get("test-result", AcceptanceEvidence(
            "", "", False)).passed
    if any(term in normalized for term in ("tool", "工具", "证据")):
        return bool(evidence)
    return True


__all__ = [
    "DefaultAcceptanceEvaluator",
    "acceptance_evidence_from_history",
]
