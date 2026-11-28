"""Contract governance preflight and fail-closed acceptance gate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence

from krelcode.contracts.acceptance import (
    AcceptanceEvaluatorPort,
    AcceptanceResult,
    AcceptanceStatus,
)
from krelcode.contracts.agent import AgentContract
from krelcode.contracts.application import ApplicationRequest
from krelcode.contracts.artifact import ArtifactStorePort
from krelcode.contracts.governance import (
    AuditCorrelation,
    ControlPlanePort,
    GovernanceRequest,
    SandboxProfile,
    SandboxResourceLimits,
)
from krelcode.contracts.tool import ToolContext
from krelcode.core.events import (
    AcceptanceEvaluated,
    AgentEvent,
    SecurityDecisionRecorded,
)
from krelcode.core.messages import ImageRef, Message
from .acceptance import (
    AcceptanceGateError,
    AcceptanceReviewRequired,
    acceptance_evidence_from_history,
    deliverable_evidence_from_contract,
)

EmitEvent = Callable[[AgentEvent], Awaitable[None]]


class AgentContractDenied(RuntimeError):
    """The frozen Agent Contract failed governance preflight."""


async def preflight_agent_contract(
    tool_context: ToolContext,
    contract: AgentContract,
    control_plane: Optional[ControlPlanePort],
    emit: EmitEvent,
) -> None:
    if control_plane is None:
        return
    runtime = tool_context.runtime
    if contract.authority.allowed_tools is not None:
        runtime = replace(
            runtime,
            tool_allowlist=frozenset(contract.authority.allowed_tools),
        )
    if contract.authority.allowed_mcp_servers is not None:
        runtime = replace(
            runtime,
            mcp_allowlist=frozenset(contract.authority.allowed_mcp_servers),
            alive_mcp_servers=frozenset(
                contract.authority.allowed_mcp_servers),
            mcp_health_known=True,
        )
    request = GovernanceRequest(
        identity=tool_context.principal,
        enterprise_policy=tool_context.enterprise_policy,
        runtime=runtime,
        action="agent.contract.admit",
        resource=contract.contract_id,
        workspace_root=tool_context.workspace_root,
        sandbox_profile=_sandbox_profile(contract),
        authority=contract.authority,
        audit=AuditCorrelation(
            correlation_id=contract.contract_id,
            session_id=runtime.session_id or "",
            turn_id=runtime.turn_id or "",
            request_id=contract.contract_id,
            trace_id=contract.contract_hash[:32],
        ),
        metadata={
            "contract_hash": contract.contract_hash,
            "deliverable_count": len(contract.deliverables.requirements),
            "acceptance_enforced": contract.enforce_acceptance,
        },
    )
    decision = await control_plane.evaluate(request)
    await emit(SecurityDecisionRecorded(
        contract.contract_id,
        decision.security_decision.to_dict(),
    ))
    if not decision.allowed:
        raise AgentContractDenied(
            "AgentContract 未通过治理预检: "
            + (decision.reason or "denied"))


async def admit_agent_contract(
    tool_context: ToolContext,
    contract: AgentContract,
    control_plane: Optional[ControlPlanePort],
    emit: EmitEvent,
    *,
    text: str,
    images: Sequence[ImageRef] = (),
) -> None:
    validate_agent_contract_inputs(contract, text, images)
    await preflight_agent_contract(
        tool_context, contract, control_plane, emit)


def validate_agent_contract_inputs(
    contract: AgentContract,
    text: str,
    images: Sequence[ImageRef] = (),
) -> None:
    errors = contract.validate_inputs(text, images)
    if errors:
        raise ValueError(
            "AgentContract 输入校验失败: " + "; ".join(errors))


async def evaluate_contract_acceptance(
    *,
    contract: Optional[AgentContract],
    request: Optional[ApplicationRequest],
    evaluator: Optional[AcceptanceEvaluatorPort],
    final_response: str,
    history: Sequence[Message],
    workspace_root: Path,
    artifact_store: Optional[ArtifactStorePort],
    emit: EmitEvent,
) -> Optional[AcceptanceResult]:
    if request is None or evaluator is None:
        return None
    evidence = list(acceptance_evidence_from_history(history))
    if contract is not None:
        evidence.extend(deliverable_evidence_from_contract(
            contract,
            workspace_root,
            final_response,
            artifact_store=artifact_store,
        ))
    result = evaluator.evaluate(
        request,
        final_response,
        tuple(evidence),
        contract=contract,
    )
    await emit(AcceptanceEvaluated(result.as_dict()))
    return result


def enforce_contract_acceptance(
    contract: Optional[AgentContract],
    result: Optional[AcceptanceResult],
) -> None:
    if (
        contract is not None
        and contract.enforce_acceptance
        and result is not None
        and result.status is not AcceptanceStatus.PASSED
    ):
        if result.status is AcceptanceStatus.NEEDS_REVIEW:
            raise AcceptanceReviewRequired(result)
        raise AcceptanceGateError(result)


def _sandbox_profile(contract: AgentContract) -> SandboxProfile:
    sandbox = contract.sandbox
    return SandboxProfile(
        name=sandbox.profile,
        backend=sandbox.backend,
        enabled=sandbox.enabled,
        network=sandbox.network,
        permission_profile=sandbox.permission_profile,
        write_scope=sandbox.write_scope,
        resource_limits=SandboxResourceLimits(
            cpu_seconds=sandbox.cpu_seconds,
            memory_mb=sandbox.memory_mb,
            storage_mb=sandbox.storage_mb,
            process_limit=sandbox.process_limit,
        ),
    )


__all__ = [
    "AgentContractDenied",
    "admit_agent_contract",
    "enforce_contract_acceptance",
    "evaluate_contract_acceptance",
    "preflight_agent_contract",
    "validate_agent_contract_inputs",
]
