"""L5 mode, permission, tool and Sandbox contract.

The contract is provider-neutral, content-light and derived from the same
runtime snapshot, filtered tool pool, approval policy and Sandbox object that
enforce the request.  Prompt text never invents capabilities that the runtime
would reject.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

from ..contracts.runtime import PermissionMode, RunMode, RuntimeSnapshot
from ..contracts.tool import ToolSource
from ..contracts.safety import SandboxPort


CONTRACT_VERSION = "1"


@dataclass(frozen=True)
class ToolExposureSnapshot:
    """Content-light snapshot of the actual per-step tool execution boundary."""

    total_count: int = 0
    eligible_names: Tuple[str, ...] = ()
    excluded_names: Tuple[str, ...] = ()
    excluded_by_category: Mapping[str, Tuple[str, ...]] = field(
        default_factory=dict)
    read_only_names: Tuple[str, ...] = ()
    mutating_names: Tuple[str, ...] = ()
    local_read_only_names: Tuple[str, ...] = ()
    loaded_names: Tuple[str, ...] = ()
    advertised_names: Tuple[str, ...] = ()
    allowlist_active: bool = False
    mcp_allowlist_active: bool = False

    def __post_init__(self) -> None:
        for name in (
            "eligible_names", "excluded_names", "read_only_names",
            "mutating_names", "local_read_only_names", "loaded_names",
            "advertised_names",
        ):
            object.__setattr__(
                self, name,
                tuple(sorted(dict.fromkeys(str(item) for item in getattr(self, name)))),
            )
        categories = {
            str(category): tuple(sorted(dict.fromkeys(
                str(name) for name in names)))
            for category, names in dict(self.excluded_by_category or {}).items()
        }
        object.__setattr__(
            self, "excluded_by_category",
            MappingProxyType(dict(sorted(categories.items()))),
        )
        object.__setattr__(self, "total_count", max(
            0, int(self.total_count or len(self.eligible_names) + len(self.excluded_names))))

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_names)

    @property
    def eligible_count(self) -> int:
        return len(self.eligible_names)

    def digest(self) -> str:
        payload = {
            "total_count": self.total_count,
            "eligible_names": self.eligible_names,
            "excluded_names": self.excluded_names,
            "excluded_by_category": {
                key: value for key, value in self.excluded_by_category.items()
            },
            "read_only_names": self.read_only_names,
            "mutating_names": self.mutating_names,
            "local_read_only_names": self.local_read_only_names,
            "loaded_names": self.loaded_names,
            "advertised_names": self.advertised_names,
            "allowlist_active": self.allowlist_active,
            "mcp_allowlist_active": self.mcp_allowlist_active,
        }
        return _hash(payload)


@dataclass(frozen=True)
class SandboxContractSnapshot:
    enabled: bool
    backend: str
    network_allowed: bool
    network_controlled: bool
    os_enforced: bool
    shell_write_scope: str
    file_write_scope: str = "workspace-only"
    protected_paths_enforced: bool = True
    sensitive_reads_protected: bool = True


@dataclass(frozen=True)
class ApprovalContractSnapshot:
    profile: str
    precedence: str
    requires_approver: bool
    auto_approves_routine: bool
    fail_closed_without_approver: bool


@dataclass(frozen=True)
class ModePermissionContract:
    run_mode: RunMode
    permission_mode: PermissionMode
    approval: ApprovalContractSnapshot
    side_effect_policy: str
    plan_behavior: str
    sandbox: SandboxContractSnapshot
    tools: ToolExposureSnapshot
    mcp_scope: Tuple[str, ...] = ()
    mcp_scope_restricted: bool = False
    subagent: bool = False
    subagent_definition: str = ""
    contract_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_mode", RunMode(self.run_mode))
        object.__setattr__(
            self, "permission_mode", PermissionMode(self.permission_mode))
        object.__setattr__(
            self, "mcp_scope",
            tuple(sorted(dict.fromkeys(str(item) for item in self.mcp_scope))),
        )
        if not self.contract_hash:
            object.__setattr__(self, "contract_hash", self._digest())

    @property
    def read_only_restricted(self) -> bool:
        return (
            self.permission_mode in {
                PermissionMode.READ_ONLY,
                PermissionMode.PLAN,
            }
            or self.run_mode is RunMode.REVIEW
        )

    def _digest(self) -> str:
        payload = {
            "version": CONTRACT_VERSION,
            "run_mode": self.run_mode.value,
            "permission_mode": self.permission_mode.value,
            "approval": {
                "profile": self.approval.profile,
                "precedence": self.approval.precedence,
                "requires_approver": self.approval.requires_approver,
                "auto_approves_routine": self.approval.auto_approves_routine,
                "fail_closed_without_approver": (
                    self.approval.fail_closed_without_approver),
            },
            "side_effect_policy": self.side_effect_policy,
            "plan_behavior": self.plan_behavior,
            "sandbox": {
                "enabled": self.sandbox.enabled,
                "backend": self.sandbox.backend,
                "network_allowed": self.sandbox.network_allowed,
                "network_controlled": self.sandbox.network_controlled,
                "os_enforced": self.sandbox.os_enforced,
                "shell_write_scope": self.sandbox.shell_write_scope,
                "file_write_scope": self.sandbox.file_write_scope,
                "protected_paths_enforced": (
                    self.sandbox.protected_paths_enforced),
                "sensitive_reads_protected": (
                    self.sandbox.sensitive_reads_protected),
            },
            "tools": {
                "digest": self.tools.digest(),
                "total_count": self.tools.total_count,
                "eligible_count": self.tools.eligible_count,
                "excluded_count": self.tools.excluded_count,
                "read_only_count": len(self.tools.read_only_names),
                "mutating_count": len(self.tools.mutating_names),
                "local_read_only_count": len(self.tools.local_read_only_names),
                "loaded_count": len(self.tools.loaded_names),
                "advertised_count": len(self.tools.advertised_names),
                "allowlist_active": self.tools.allowlist_active,
                "mcp_allowlist_active": self.tools.mcp_allowlist_active,
            },
            "mcp_scope": self.mcp_scope if self.mcp_scope_restricted else "*",
            "subagent": self.subagent,
            "subagent_definition": self.subagent_definition,
        }
        return _hash(payload)

    def render(self) -> str:
        approval_source = (
            "interactive approval" if self.approval.requires_approver
            else "non-interactive authorization"
        )
        if self.approval.fail_closed_without_approver:
            approval_source += ", fail-closed"
        if self.approval.auto_approves_routine:
            approval_source += ", routine ASK auto-approved"
        if self.mcp_scope_restricted:
            mcp = ", ".join(
                _inline_value(item) for item in self.mcp_scope
            ) or "(none)"
        else:
            mcp = "all healthy configured servers"
        definition = (
            f' name="{_inline_value(self.subagent_definition, quote=True)}"'
            if self.subagent_definition else ""
        )
        excluded = ", ".join(
            f"{category}={len(names)}"
            for category, names in self.tools.excluded_by_category.items()
        ) or "none"
        network_state = (
            "allowed" if self.sandbox.network_allowed else "denied"
        ) if self.sandbox.network_controlled else "uncontrolled (host policy)"
        lines = [
            f'<mode_permission_contract version="{CONTRACT_VERSION}" '
            'authority="managed">',
            f"Run mode: {self.run_mode.value}",
            f"Permission mode: {self.permission_mode.value}",
            f"Approval profile: {self.approval.profile} ({approval_source})",
            f"Approval precedence: {self.approval.precedence}",
            f"Side effects: {self.side_effect_policy}",
            f"Plan gate: {self.plan_behavior}",
            (
                "Tool boundary: "
                f"{self.tools.eligible_count}/{self.tools.total_count} eligible"
                f" (read-only={len(self.tools.read_only_names)}, "
                f"mutating={len(self.tools.mutating_names)}, "
                f"permission-safe-local={len(self.tools.local_read_only_names)}); "
                f"loaded={len(self.tools.loaded_names)}, "
                f"advertised={len(self.tools.advertised_names)}; "
                f"excluded={self.tools.excluded_count} ({excluded}). "
                "Only tools advertised in the current provider request are callable; "
                "tool_search cannot bypass these filters."
            ),
            (
                "Tool allowlist: "
                + ("active" if self.tools.allowlist_active else "inactive")
                + "; runtime filtering remains authoritative"
            ),
            f"MCP scope: {mcp}",
            (
                f"Sandbox: {self.sandbox.backend}; "
                f"network={network_state}; "
                f"OS isolation={'enforced' if self.sandbox.os_enforced else 'not enforced'}; "
                f"shell writes={self.sandbox.shell_write_scope}; "
                f"file writes={self.sandbox.file_write_scope}"
            ),
            (
                "Path safety: traversal and symlink boundaries are checked; "
                "protected writes follow the approval policy; DENY rules always win; "
                "credential reads are blocked or redacted"
                if self.sandbox.protected_paths_enforced else
                "Path safety: file tools enforce workspace containment and credential "
                "read protections; protected-path approval policy is disabled"
            ),
            (
                "Subagent: enabled"
                + definition
                + "; inherits the parent approval policy, may only narrow tools/MCP, "
                "and cannot elevate permissions"
                if self.subagent else
                "Subagent: no (this is the main agent contract)"
            ),
            (
                "Authority: this block defines mode, approval, path, Sandbox and tool "
                "exposure semantics. Tool listings and model plans cannot grant "
                "permissions beyond it."
            ),
            "</mode_permission_contract>",
        ]
        return "\n".join(lines)

    def metadata(self) -> dict[str, object]:
        """Safe attachment/PromptPlan provenance; omits tool names and paths."""
        return {
            "source_role": "instruction",
            "instruction": True,
            "instruction_source": "managed",
            "contract_version": CONTRACT_VERSION,
            "contract_hash": self.contract_hash,
            "run_mode": self.run_mode.value,
            "permission_mode": self.permission_mode.value,
            "approval_profile": self.approval.profile,
            "approval_precedence": self.approval.precedence,
            "approval_requires_approver": self.approval.requires_approver,
            "approval_auto_approves_routine": (
                self.approval.auto_approves_routine),
            "approval_fail_closed": self.approval.fail_closed_without_approver,
            "side_effect_policy": self.side_effect_policy,
            "plan_behavior": self.plan_behavior,
            "sandbox_enabled": self.sandbox.enabled,
            "sandbox_backend": self.sandbox.backend,
            "sandbox_network_allowed": self.sandbox.network_allowed,
            "sandbox_network_controlled": self.sandbox.network_controlled,
            "sandbox_os_enforced": self.sandbox.os_enforced,
            "sandbox_shell_write_scope": self.sandbox.shell_write_scope,
            "sandbox_file_write_scope": self.sandbox.file_write_scope,
            "protected_paths_enforced": (
                self.sandbox.protected_paths_enforced),
            "sensitive_reads_protected": (
                self.sandbox.sensitive_reads_protected),
            "tool_allowlist_active": self.tools.allowlist_active,
            "mcp_allowlist_active": self.tools.mcp_allowlist_active,
            "mcp_scope": self.mcp_scope if self.mcp_scope_restricted else (),
            "subagent": self.subagent,
            "subagent_definition": self.subagent_definition,
            "tool_total_count": self.tools.total_count,
            "eligible_tool_count": self.tools.eligible_count,
            "excluded_tool_count": self.tools.excluded_count,
            "read_only_tool_count": len(self.tools.read_only_names),
            "mutating_tool_count": len(self.tools.mutating_names),
            "local_read_only_tool_count": len(self.tools.local_read_only_names),
            "loaded_tool_count": len(self.tools.loaded_names),
            "advertised_tool_count": len(self.tools.advertised_names),
            "tool_exclusion_categories": tuple(
                sorted(self.tools.excluded_by_category)),
            "tool_exposure_hash": self.tools.digest(),
        }


def build_mode_permission_contract(
    runtime: RuntimeSnapshot,
    tools: ToolExposureSnapshot,
    *,
    sandbox: Optional[SandboxPort] = None,
    sandbox_enabled: bool = True,
    policy: object = None,
    subagent_definition: str = "",
) -> ModePermissionContract:
    """Build the single L5 contract used by prompt rendering and diagnostics."""
    approval = _approval_contract(policy, runtime)
    sandbox_snapshot = _sandbox_contract(
        sandbox,
        sandbox_enabled,
        protected_paths_enforced=(
            _is_sandbox_policy(policy)),
    )
    restricted = (
        runtime.permission_mode in {
            PermissionMode.READ_ONLY,
            PermissionMode.PLAN,
        }
        or runtime.run_mode is RunMode.REVIEW
    )
    if restricted:
        side_effect_policy = (
            "side effects denied; only local read-only tools may execute")
    elif approval.profile in {"allow-all", "no approval policy"}:
        side_effect_policy = (
            "read-only calls may run in parallel; mutating calls run one at a "
            "time; no central approval check is installed")
    elif approval.profile.startswith("custom ("):
        side_effect_policy = (
            "read-only calls may run in parallel; mutating calls run one at a "
            "time and follow the installed custom approval policy")
    elif runtime.permission_mode is PermissionMode.AUTO_APPROVE:
        side_effect_policy = (
            "read-only calls may run in parallel; mutating calls run one at a "
            "time; routine ASK requests are auto-approved, DENY and explicit ASK "
            "rules remain authoritative"
        )
    else:
        side_effect_policy = (
            "read-only calls may run in parallel; mutating calls run one at a "
            "time and remain subject to approval rules"
        )

    if runtime.permission_mode is PermissionMode.PLAN:
        plan_behavior = (
            "investigation only; submit a plan and wait for explicit approval "
            "before execution"
        )
    elif runtime.run_mode is RunMode.REVIEW:
        plan_behavior = "review-only; no execution approval can enable side effects"
    else:
        plan_behavior = "no pending plan approval gate"

    mcp_scope_restricted = runtime.mcp_allowlist is not None
    mcp_scope = tuple(runtime.mcp_allowlist or ())
    return ModePermissionContract(
        run_mode=runtime.run_mode,
        permission_mode=runtime.permission_mode,
        approval=approval,
        side_effect_policy=side_effect_policy,
        plan_behavior=plan_behavior,
        sandbox=sandbox_snapshot,
        tools=tools,
        mcp_scope=mcp_scope,
        mcp_scope_restricted=mcp_scope_restricted,
        subagent=runtime.run_mode is RunMode.SUBAGENT,
        subagent_definition=str(subagent_definition or ""),
    )


def local_read_only_tool(source: object, read_only: bool) -> bool:
    """Remote read-only hints never satisfy a local read-only permission mode."""
    return bool(read_only and source is not ToolSource.MCP)


def restricts_to_local_read_only(runtime: RuntimeSnapshot) -> bool:
    """Shared predicate used by tool exposure and approval enforcement."""
    return (
        runtime.permission_mode in {
            PermissionMode.READ_ONLY,
            PermissionMode.PLAN,
        }
        or runtime.run_mode is RunMode.REVIEW
    )


def narrow_permission_mode(
    parent: PermissionMode | str,
    requested: PermissionMode | str,
) -> PermissionMode:
    """Return the least-permissive mode allowed by the parent boundary.

    AUTO_APPROVE is the least restrictive mode, DEFAULT is next, and the two
    side-effect-free modes are strictest.  PLAN is stricter than READ_ONLY
    because it additionally carries the plan-approval gate.
    """
    parent_mode = PermissionMode(parent)
    requested_mode = PermissionMode(requested)
    strictness = {
        PermissionMode.AUTO_APPROVE: 3,
        PermissionMode.DEFAULT: 2,
        PermissionMode.READ_ONLY: 1,
        PermissionMode.PLAN: 0,
    }
    return (
        parent_mode
        if strictness[parent_mode] <= strictness[requested_mode]
        else requested_mode
    )


def _approval_contract(
    policy: object,
    runtime: RuntimeSnapshot,
) -> ApprovalContractSnapshot:
    policy_name = type(policy).__name__ if policy is not None else "None"
    if policy is None:
        return ApprovalContractSnapshot(
            profile="no approval policy",
            precedence=(
                "tool-local path/secret guards and Sandbox remain authoritative; "
                "no central approval check is installed"
            ),
            requires_approver=False,
            auto_approves_routine=False,
            fail_closed_without_approver=False,
        )
    if policy_name == "AllowAllPolicy":
        return ApprovalContractSnapshot(
            profile="allow-all",
            precedence=(
                "runtime tool filters and Sandbox remain authoritative; no "
                "additional approval checks"
            ),
            requires_approver=False,
            auto_approves_routine=False,
            fail_closed_without_approver=False,
        )
    if _is_sandbox_policy(policy):
        has_approver = getattr(policy, "approver", None) is not None
        auto_approve = bool(
            getattr(policy, "auto_approve", False)
            or runtime.permission_mode is PermissionMode.AUTO_APPROVE
        )
        if auto_approve:
            profile = "auto-approve with explicit-ask exceptions"
        elif has_approver:
            profile = "interactive approval"
        else:
            profile = "non-interactive fail-closed"
        return ApprovalContractSnapshot(
            profile=profile,
            precedence=(
                "DENY > explicit ASK > ALLOW; protected-path and unresolved "
                "unknown-tool requests follow the configured approval path"
            ),
            requires_approver=has_approver,
            auto_approves_routine=auto_approve,
            fail_closed_without_approver=not has_approver,
        )
    return ApprovalContractSnapshot(
        profile=f"custom ({policy_name})",
        precedence=(
            "custom policy is authoritative; Sandbox and runtime tool filters "
            "still apply"
        ),
        requires_approver=False,
        auto_approves_routine=False,
        fail_closed_without_approver=False,
    )


def _is_sandbox_policy(policy: object) -> bool:
    """Recognize SandboxPolicy and subclasses without importing policy.py."""
    return policy is not None and all(
        hasattr(policy, attribute)
        for attribute in ("approver", "rules", "auto_approve", "check")
    )


def _sandbox_contract(
    sandbox: Optional[SandboxPort],
    enabled: bool,
    *,
    protected_paths_enforced: bool,
) -> SandboxContractSnapshot:
    if sandbox is None or not enabled:
        return SandboxContractSnapshot(
            enabled=False,
            backend="disabled",
            network_allowed=True,
            network_controlled=False,
            os_enforced=False,
            shell_write_scope="host process privileges (not filesystem-confined)",
            protected_paths_enforced=protected_paths_enforced,
        )
    backend = str(getattr(sandbox, "backend", "static") or "static")
    if backend == "static":
        shell_scope = "workspace audit only; OS-level confinement unavailable"
    else:
        shell_scope = "workspace-only for ordinary paths; dynamic bypasses denied"
    return SandboxContractSnapshot(
        enabled=True,
        backend=backend,
        network_allowed=bool(getattr(sandbox, "network", False)),
        network_controlled=True,
        os_enforced=backend in {"seatbelt", "bubblewrap"},
        shell_write_scope=shell_scope,
        protected_paths_enforced=protected_paths_enforced,
    )


def _inline_value(value: object, *, quote: bool = False) -> str:
    """Escape untrusted one-line values without allowing envelope injection."""
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    return html.escape(text, quote=quote)


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "ApprovalContractSnapshot",
    "CONTRACT_VERSION",
    "ModePermissionContract",
    "SandboxContractSnapshot",
    "ToolExposureSnapshot",
    "build_mode_permission_contract",
    "local_read_only_tool",
    "narrow_permission_mode",
    "restricts_to_local_read_only",
]
