"""Delegation contract construction, budget narrowing and result validation."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..contracts.application import (
    ApplicationRequest,
    TaskBudget,
    TaskShape,
    TriggerType,
)
from ..contracts.delegation import (
    DelegationAcceptance,
    DelegationCapabilities,
    DelegationContract,
    DelegationEvidence,
    DelegationFailurePolicy,
    DelegationKind,
    DelegationOutputContract,
    DelegationParentContext,
    DelegationResult,
    DelegationStatus,
    WorkspaceVersion,
)
from ..tools.schema import validate_tool_arguments
from .models import AgentHandle


def build_delegation_contract(
    *,
    task: str,
    raw: Mapping[str, object] | None = None,
    parent: Optional[DelegationParentContext] = None,
    application_request: Optional[ApplicationRequest] = None,
) -> DelegationContract:
    """Build a contract from structured input, with legacy task compatibility."""
    parent_context = parent or DelegationParentContext()
    if raw:
        return DelegationContract.from_mapping(
            raw,
            parent=parent_context,
            task_fallback=task,
        )
    acceptance = (
        DelegationAcceptance(
            criteria=application_request.acceptance.criteria,
            required_evidence=application_request.acceptance.required_evidence,
        )
        if application_request is not None else DelegationAcceptance()
    )
    budget = (
        application_request.budget
        if application_request is not None else TaskBudget()
    )
    return DelegationContract(
        objective=task,
        parent=parent_context,
        budget=budget,
        acceptance=acceptance,
        task_text=task,
        metadata={"legacy_task_compat": True},
    )


def narrow_task_budget(
    parent: TaskBudget,
    requested: TaskBudget,
) -> TaskBudget:
    """Ensure delegated work cannot exceed the parent's residual limits."""
    return TaskBudget(
        max_steps=_min_int(parent.max_steps, requested.max_steps),
        max_tokens=_min_int(parent.max_tokens, requested.max_tokens),
        timeout_seconds=_min_optional(
            parent.timeout_seconds, requested.timeout_seconds),
        max_cost_usd=_min_optional(
            parent.max_cost_usd, requested.max_cost_usd),
        max_tool_calls=_min_int(
            parent.max_tool_calls, requested.max_tool_calls),
        max_subagents=_min_int(
            parent.max_subagents, requested.max_subagents),
        max_high_risk_actions=_min_int(
            parent.max_high_risk_actions, requested.max_high_risk_actions),
        soft_limit_ratio=min(
            parent.soft_limit_ratio, requested.soft_limit_ratio),
        ask_user_at_soft_limit=(
            parent.ask_user_at_soft_limit
            or requested.ask_user_at_soft_limit
        ),
        allow_budget_extension=(
            parent.allow_budget_extension
            and requested.allow_budget_extension
        ),
        budget_extension_ratio=min(
            parent.budget_extension_ratio,
            requested.budget_extension_ratio,
        ),
    )


def prepare_delegation_runtime(
    contract: DelegationContract,
    *,
    parent: DelegationParentContext,
    tools: Sequence[object],
    mcp_servers: Sequence[str],
    permission_mode: str,
    definition_name: str,
    model: str,
    application_request: ApplicationRequest,
) -> tuple[DelegationContract, ApplicationRequest]:
    requested_budget = contract.budget
    if not _budget_has_limits(requested_budget):
        requested_budget = default_child_budget(parent.budget)
    effective_budget = narrow_task_budget(parent.budget, requested_budget)
    effective_contract = replace(
        contract,
        capabilities=DelegationCapabilities(
            definition=definition_name,
            model=model,
            allowed_tools=tuple(
                str(getattr(tool, "name", "")) for tool in tools),
            mcp_servers=tuple(mcp_servers),
            permission_mode=permission_mode,
            skills=contract.capabilities.skills,
        ),
        budget=effective_budget,
        contract_hash="",
        delegation_id="",
    )
    prepared_request = replace(
        application_request,
        trigger=TriggerType.SUBAGENT,
        goal=effective_contract.task,
        task_shape=TaskShape.MULTI_AGENT,
        acceptance=effective_contract.acceptance.to_acceptance_contract(),
        budget=effective_budget,
        metadata={
            **dict(application_request.metadata),
            "delegation_id": effective_contract.delegation_id,
            "delegation_contract_hash": effective_contract.contract_hash,
            "parent_task_id": parent.parent_task_id,
            "parent_turn_id": parent.parent_turn_id,
            "parent_session_id": parent.parent_session_id,
        },
    )
    return effective_contract, prepared_request


def render_delegation_task(contract: DelegationContract) -> str:
    """Render the machine contract into the child's self-contained task."""
    if contract.metadata.get("legacy_task_compat"):
        return contract.task
    known = contract.known_context
    lines = [
        "# Delegated task",
        f"Objective: {contract.objective}",
    ]
    sections = (
        ("Confirmed facts", known.facts),
        ("Assumptions", known.assumptions),
        ("Decisions", known.decisions),
        ("Completed work", known.completed_work),
        ("Blockers", known.blockers),
        ("Already attempted", known.attempted),
        ("Constraints", contract.constraints),
        ("Allowed read paths", contract.scope.read_paths),
        ("Allowed write paths", contract.scope.write_paths),
        ("Denied paths", contract.scope.deny_paths),
        ("Allowed systems", contract.scope.systems),
        ("Acceptance criteria", contract.acceptance.criteria),
        ("Required evidence", contract.acceptance.required_evidence),
    )
    for title, values in sections:
        if values:
            lines.append("")
            lines.append(title + ":")
            lines.extend(f"- {value}" for value in values)
    lines.extend((
        "",
        "Output contract:",
        json.dumps(
            contract.output.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "",
        f"Failure policy: {DelegationFailurePolicy(contract.failure_policy).value}",
        f"Relation kind: {DelegationKind(contract.relation_kind).value}",
    ))
    if contract.handoff_state:
        lines.extend((
            "",
            "Handoff state:",
            json.dumps(
                dict(contract.handoff_state),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        ))
    return "\n".join(lines)


def default_child_budget(parent: TaskBudget) -> TaskBudget:
    """Allocate one bounded share when the caller omitted child limits."""
    slots = max(1, parent.max_subagents or 1)
    return TaskBudget(
        max_steps=_share_int(parent.max_steps, slots),
        max_tokens=_share_int(parent.max_tokens, slots),
        timeout_seconds=_share_float(parent.timeout_seconds, slots),
        max_cost_usd=_share_float(parent.max_cost_usd, slots),
        max_tool_calls=_share_int(parent.max_tool_calls, slots),
        max_subagents=None,
        max_high_risk_actions=_share_int(
            parent.max_high_risk_actions, slots),
        soft_limit_ratio=parent.soft_limit_ratio,
        ask_user_at_soft_limit=parent.ask_user_at_soft_limit,
        allow_budget_extension=parent.allow_budget_extension,
        budget_extension_ratio=parent.budget_extension_ratio,
    )


def validate_delegation_result(
    handle: AgentHandle,
    *,
    current_workspace: WorkspaceVersion,
) -> DelegationResult:
    """Convert a child report into a validated, versioned Observation source."""
    contract = handle.delegation_contract
    if contract is None:
        contract = build_delegation_contract(task=handle.task)
    result_text = str(handle.result or "")
    structured: dict[str, object] = {}
    errors: list[str] = []
    if handle.status == "completed":
        if len(result_text) > contract.output.max_chars:
            errors.append(
                f"result_exceeds_max_chars:{len(result_text)}>"
                f"{contract.output.max_chars}")
        elif contract.output.format == "json":
            try:
                parsed = json.loads(result_text)
            except json.JSONDecodeError as exc:
                errors.append(f"invalid_json:{exc.msg}")
            else:
                if not isinstance(parsed, dict):
                    errors.append("json_output_must_be_object")
                else:
                    structured = parsed
                    if contract.output.schema:
                        for item in validate_tool_arguments(
                            contract.output.schema, parsed):
                            errors.append(
                                f"output_schema:{item.render()}")
                    missing = [
                        field for field in contract.output.required_fields
                        if field not in parsed
                    ]
                    if missing:
                        errors.append(
                            "missing_output_fields:" + ",".join(missing))
        else:
            structured = {"text": result_text}
    elif handle.status not in {"failed", "cancelled", "killed"}:
        errors.append(f"non_terminal_status:{handle.status}")
    else:
        errors.append(f"child_status:{handle.status}")

    evidence = _evidence_for_handle(handle)
    if contract.output.evidence_required and not evidence:
        errors.append("evidence_required")
    if contract.acceptance.required_evidence and not evidence:
        errors.append("acceptance_evidence_missing")
    age = max(0.0, time.time() - max(
        (item.observed_at for item in evidence), default=time.time()))
    if age > contract.acceptance.max_evidence_age_seconds:
        errors.append(f"evidence_stale:{age:.1f}s")

    baseline = handle.base_workspace_version
    result_workspace = (
        handle.result_workspace_version
        if handle.result_workspace_version.available
        else current_workspace
    )
    if contract.acceptance.require_workspace_match:
        if not baseline.available or not result_workspace.available:
            errors.append("workspace_version_missing")
        elif (
            DelegationKind(contract.relation_kind) is DelegationKind.HANDOFF
            and not current_workspace.matches(result_workspace)
        ):
            errors.append("handoff_workspace_mismatch")
        elif (
            DelegationKind(contract.relation_kind) is DelegationKind.DELEGATION
            and not current_workspace.matches(result_workspace)
            and not current_workspace.matches(baseline)
        ):
            errors.append("workspace_changed_during_delegation")

    accepted = not errors and handle.status == "completed"
    status = (
        DelegationStatus.ACCEPTED
        if accepted else DelegationStatus.REJECTED
    )
    result_workspace = replace(
        result_workspace,
        metadata={
            **dict(result_workspace.metadata),
            "base_workspace_digest": baseline.workspace_digest,
            "current_workspace_digest": current_workspace.workspace_digest,
        },
    )
    return DelegationResult(
        delegation_id=contract.delegation_id,
        agent_id=handle.id,
        status=status,
        summary=result_text or str(handle.error or ""),
        structured_output=structured,
        evidence=evidence,
        workspace_version=result_workspace,
        produced_at=time.time(),
        contract_hash=contract.contract_hash,
        accepted=accepted,
        validation_errors=tuple(errors),
        usage=dict(handle.budget_usage),
        metadata={
            "child_status": handle.status,
            "relation_kind": DelegationKind(contract.relation_kind).value,
            "sidechain_path": str(handle.sidechain_path or ""),
            "branch": handle.branch or "",
        },
    )


def failure_policy_payload(handle: AgentHandle) -> dict[str, object]:
    contract = handle.delegation_contract
    if contract is None:
        return {}
    return {
        "policy": DelegationFailurePolicy(
            contract.failure_policy).value,
        "max_retries": contract.retry_policy.max_retries,
        "allow_reassign": contract.retry_policy.allow_reassign,
        "reassign_definition": contract.retry_policy.reassign_definition,
        "retry_count": handle.retry_count,
    }


def _evidence_for_handle(handle: AgentHandle) -> tuple[DelegationEvidence, ...]:
    observed_at = float(handle.finished_at or time.time())
    workspace = handle.result_workspace_version
    evidence: list[DelegationEvidence] = []
    if handle.sidechain_path is not None:
        evidence.append(DelegationEvidence(
            id=f"sidechain:{handle.id}",
            source="sidechain",
            observed_at=observed_at,
            workspace_version=workspace,
            metadata={"path": str(handle.sidechain_path)},
        ))
    if handle.context_checkpoint_id:
        evidence.append(DelegationEvidence(
            id=f"context-checkpoint:{handle.context_checkpoint_id}",
            source="context_checkpoint",
            observed_at=observed_at,
            workspace_version=workspace,
            metadata={"checkpoint_id": handle.context_checkpoint_id},
        ))
    feed = getattr(handle, "feed", None)
    latest_sequence = int(
        getattr(handle, "latest_sequence", 0)
        or (feed.latest_sequence if feed is not None else 0)
    )
    if latest_sequence:
        evidence.append(DelegationEvidence(
            id=f"agent-feed:{handle.id}:{latest_sequence}",
            source="agent_feed",
            observed_at=observed_at,
            workspace_version=workspace,
            metadata={"latest_sequence": latest_sequence},
        ))
    return tuple(evidence)


def _min_optional(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _min_int(left: Optional[int], right: Optional[int]) -> Optional[int]:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _share_int(value: Optional[int], slots: int) -> Optional[int]:
    return None if value is None else max(1, int(value) // slots)


def _share_float(value: Optional[float], slots: int) -> Optional[float]:
    return None if value is None else max(0.001, float(value) / slots)


def _budget_has_limits(budget: TaskBudget) -> bool:
    return any(
        getattr(budget, name, None) is not None
        for name in (
            "max_steps", "max_tokens", "timeout_seconds", "max_cost_usd",
            "max_tool_calls", "max_subagents", "max_high_risk_actions",
        )
    )


__all__ = [
    "build_delegation_contract",
    "default_child_budget",
    "failure_policy_payload",
    "narrow_task_budget",
    "prepare_delegation_runtime",
    "render_delegation_task",
    "validate_delegation_result",
]
