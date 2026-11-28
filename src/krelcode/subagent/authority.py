"""Build narrowed authority envelopes for delegated agents."""

from __future__ import annotations

from typing import Sequence

from ..contracts.agent_authority import AuthorityEnvelope
from ..contracts.delegation import DelegationContract
from ..contracts.tool import Tool


def paths_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    for first in left:
        first_norm = first.rstrip("/")
        for second in right:
            second_norm = second.rstrip("/")
            if (
                first_norm == second_norm
                or first_norm.startswith(second_norm + "/")
                or second_norm.startswith(first_norm + "/")
            ):
                return True
    return False


def build_subagent_authority(
    contract: DelegationContract,
    *,
    tools: Sequence[Tool],
    mcp_servers: Sequence[str],
    network_allowed: bool,
) -> AuthorityEnvelope:
    systems = list(contract.scope.systems)
    if not systems:
        systems.append("workspace")
        if any(
            tool.name in {
                "run_command", "job_start", "job_output", "job_kill",
            }
            for tool in tools
        ):
            systems.append("shell")
        if mcp_servers:
            systems.append("mcp")
    return AuthorityEnvelope(
        allowed_systems=tuple(systems),
        allowed_tools=tuple(tool.name for tool in tools),
        allowed_mcp_servers=tuple(mcp_servers),
        network_allowed=network_allowed,
        write_scope=",".join(contract.scope.write_paths) or "workspace",
        allowed_read_paths=contract.scope.read_paths,
        allowed_write_paths=contract.scope.write_paths,
        denied_paths=contract.scope.deny_paths,
    )


__all__ = ["build_subagent_authority", "paths_overlap"]
