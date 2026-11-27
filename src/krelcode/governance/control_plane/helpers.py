"""Helpers that issue a GovernanceDecision before admission."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ...contracts.change_admission import ChangeCandidate, ChangeType
from ...contracts.governance import (
    AuditCorrelation,
    GovernanceDecision,
    GovernanceRequest,
    PluginTrustContext,
    SandboxProfile,
)
from ...contracts.runtime import RuntimeSnapshot
from ...contracts.tool import ToolContext
from ...safety.control_plane import DefaultControlPlane


def governance_for_change(
    candidate: ChangeCandidate,
    *,
    tool_context: Optional[ToolContext] = None,
    request_id: str = "",
) -> GovernanceDecision:
    context = tool_context
    runtime = context.runtime if context is not None else RuntimeSnapshot()
    plugin_trust = (
        PluginTrustContext(
            name=str(candidate.metadata.get("plugin_name") or ""),
            required=candidate.change_type is ChangeType.PLUGIN,
            allowed=bool(candidate.metadata.get("plugin_trusted", True)),
        )
        if candidate.change_type is ChangeType.PLUGIN
        else PluginTrustContext()
    )
    sandbox = context.sandbox if context is not None else None
    request = GovernanceRequest(
        identity=context.principal if context is not None else None,
        enterprise_policy=context.enterprise_policy if context is not None else None,
        runtime=runtime,
        action=candidate.change_type.value,
        resource=candidate.target,
        workspace_root=(
            context.workspace_root if context is not None else Path(".")
        ),
        sandbox_profile=SandboxProfile(
            backend=str(getattr(sandbox, "backend", "none")),
            enabled=bool(context.sandbox_enabled) if context is not None else True,
            network=bool(getattr(sandbox, "network", False)),
        ),
        plugin_trust=plugin_trust,
        audit=AuditCorrelation(
            correlation_id=request_id or candidate.candidate_id,
            request_id=request_id or candidate.candidate_id,
        ),
        metadata={"candidate_id": candidate.candidate_id},
    )
    return DefaultControlPlane().evaluate_sync(request)
