"""Provider-neutral governance evaluator.

This module combines identity, enterprise policy, permission mode, tool
allowlists, Sandbox profile, plugin trust, Prompt release identity and artifact
versions into one deterministic decision. Enforcement remains in each owning
execution boundary.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from dataclasses import replace
from typing import Optional

from ..core.messages import ToolCall
from ..contracts.governance import (
    ArtifactIdentity,
    AuditCorrelation,
    GovernanceDecision,
    GovernanceRequest,
    PolicyVersion,
    SecurityDecision,
    SecurityDecisionValue,
)
from ..contracts.runtime import PermissionMode


class DefaultControlPlane:
    """Evaluate every governance input without executing any side effect."""

    async def evaluate(self, request: GovernanceRequest) -> GovernanceDecision:
        return self.evaluate_sync(request)

    def evaluate_sync(self, request: GovernanceRequest) -> GovernanceDecision:
        policy = request.enterprise_policy
        denials: list[str] = []
        constraints: list[str] = []
        checked: list[str] = [
            "identity",
            "enterprise_policy",
            "permission_mode",
            "tool_allowlist",
            "sandbox_profile",
            "plugin_trust",
            "prompt_release",
            "artifact_version",
        ]

        identity = request.identity
        subject = str(getattr(identity, "subject", "") or "")
        if _policy_bool(policy, "require_sso") and identity is None:
            denials.append("企业策略要求 SSO 身份，但当前请求没有已验证身份")
        if identity is not None:
            expires_at = _float(getattr(identity, "expires_at", 0.0))
            if expires_at > 0 and expires_at <= time.time():
                denials.append("身份凭据已过期")

        requested_mode = request.runtime.permission_mode
        effective_mode = requested_mode
        clamp = getattr(policy, "clamp_permission_mode", None)
        if callable(clamp):
            try:
                effective_mode = PermissionMode(clamp(requested_mode))
            except (TypeError, ValueError):
                effective_mode = requested_mode
                denials.append("企业策略返回了无效权限模式")
        if effective_mode is not requested_mode:
            constraints.append(
                f"权限模式从 {requested_mode.value} 收窄为 "
                f"{effective_mode.value}")

        allowlist = _effective_tool_allowlist(request)
        if request.tool_name:
            if allowlist is not None and not any(
                fnmatch.fnmatchcase(request.tool_name, pattern)
                for pattern in allowlist
            ):
                denials.append(f"工具 allowlist 不允许 {request.tool_name}")
            managed_deny = request.runtime.managed_tool_denylist or frozenset()
            if any(
                fnmatch.fnmatchcase(request.tool_name, pattern)
                for pattern in managed_deny
            ):
                denials.append(f"企业工具 denylist 禁止 {request.tool_name}")
            deny_reason = getattr(policy, "deny_tool_reason", None)
            if callable(deny_reason):
                reason = deny_reason(
                    ToolCall(
                        id=request.audit.request_id or "governance",
                        name=request.tool_name,
                        arguments=dict(request.tool_arguments),
                    ),
                    request.workspace_root,
                )
                if reason:
                    denials.append(str(reason))

        sandbox = request.sandbox_profile
        if (
            not sandbox.enabled
            and _policy_enabled(policy)
            and not _policy_bool(policy, "allow_no_sandbox")
        ):
            denials.append("企业策略禁止关闭 Sandbox")

        plugin = request.plugin_trust
        if plugin.name and _policy_bool(policy, "disable_plugins"):
            denials.append("企业策略已禁用插件")
        if plugin.required and (
            not plugin.allowed
            or plugin.trust_status in {"invalid", "untrusted", "revoked"}
        ):
            denials.append(
                f"插件信任校验未通过: {plugin.name or '(unknown)'}")

        prompt = request.prompt_release
        if prompt.required:
            if not prompt.approved:
                denials.append("Prompt release 尚未获批")
            if not prompt.release_id or not prompt.release_hash:
                denials.append("Prompt release 缺少 id/hash")
            if (
                prompt.expected_release_id
                and prompt.release_id != prompt.expected_release_id
            ):
                denials.append(
                    "Prompt release id 与治理要求不一致: "
                    f"{prompt.release_id} != {prompt.expected_release_id}")
            if (
                prompt.expected_release_hash
                and prompt.release_hash != prompt.expected_release_hash
            ):
                denials.append("Prompt release hash 与治理要求不一致")

        for artifact in request.artifacts:
            _evaluate_artifact(artifact, denials)

        audit = _normalized_audit(request.audit)
        policy_version = _policy_version(policy, request.policy_version)
        denied_reason = "; ".join(dict.fromkeys(denials))
        security_decision = SecurityDecision(
            policy_id=policy_version.label,
            policy_version=policy_version.version,
            resource=request.resource or request.tool_name,
            action=request.action,
            decision=(
                SecurityDecisionValue.DENY
                if denials else SecurityDecisionValue.ALLOW
            ),
            reason=denied_reason,
            sandbox_profile=sandbox,
            trace_id=audit.trace_id or audit.correlation_id,
            evidence={
                "checked": tuple(checked),
                "policy_digest": policy_version.digest,
            },
        )
        return GovernanceDecision(
            allowed=not denials,
            reason=denied_reason,
            policy_version=policy_version,
            identity_subject=subject,
            permission_mode=effective_mode,
            tool_allowlist=allowlist,
            sandbox_profile=sandbox,
            plugin_trust=plugin,
            prompt_release=prompt,
            artifacts=request.artifacts,
            audit=audit,
            constraints=tuple(constraints),
            evidence={
                "checked": tuple(checked),
                "enterprise_policy": _policy_evidence(policy),
                "request_id": audit.request_id,
                "resource": request.resource,
                "action": request.action,
            },
            security_decision=security_decision,
        )


ControlPlane = DefaultControlPlane


def _effective_tool_allowlist(
    request: GovernanceRequest,
) -> Optional[frozenset[str]]:
    runtime = request.runtime
    transient = runtime.tool_allowlist
    managed = runtime.managed_tool_allowlist
    if transient is None:
        return frozenset(managed) if managed is not None else None
    if managed is None:
        return frozenset(transient)
    return frozenset(
        name for name in transient
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in managed)
    )


def _evaluate_artifact(
    artifact: ArtifactIdentity,
    denials: list[str],
) -> None:
    label = artifact.id or f"{artifact.kind}:(unknown)"
    if artifact.required and (
        not artifact.kind or not artifact.id or not artifact.version
    ):
        denials.append(f"Artifact 缺少 kind/id/version: {label}")
    if artifact.required and not artifact.digest:
        denials.append(f"Artifact 缺少内容摘要: {label}")
    if artifact.expected_digest and (
        artifact.digest != artifact.expected_digest
    ):
        denials.append(f"Artifact 版本摘要不匹配: {label}")


def _normalized_audit(value: AuditCorrelation) -> AuditCorrelation:
    correlation_id = value.correlation_id
    if not correlation_id:
        parts = (
            value.session_id,
            value.turn_id,
            value.request_id or value.trace_id,
        )
        correlation_id = ":".join(part for part in parts if part)
    return replace(value, correlation_id=correlation_id)


def _policy_version(
    policy: object | None,
    explicit: PolicyVersion,
) -> PolicyVersion:
    metadata = _policy_evidence(policy)
    digest = explicit.digest
    if not digest:
        encoded = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
    return PolicyVersion(
        organization=(
            explicit.organization
            or str(getattr(policy, "organization", "") or "")),
        version=(
            explicit.version
            or str(getattr(policy, "policy_version", "") or "0")),
        source=(
            explicit.source
            if explicit.source != "local"
            else str(getattr(policy, "source", "") or "local")),
        digest=digest,
    )


def _policy_evidence(policy: object | None) -> dict[str, object]:
    if policy is None:
        return {"enabled": False, "source": "local"}
    audit_metadata = getattr(policy, "audit_metadata", None)
    if callable(audit_metadata):
        try:
            value = audit_metadata()
        except Exception:
            value = {}
        if isinstance(value, dict):
            return dict(value)
    return {
        "enabled": _policy_enabled(policy),
        "organization": str(getattr(policy, "organization", "") or ""),
        "version": str(getattr(policy, "policy_version", "") or ""),
        "source": str(getattr(policy, "source", "") or ""),
    }


def _policy_enabled(policy: object | None) -> bool:
    return bool(getattr(policy, "enabled", False))


def _policy_bool(policy: object | None, name: str) -> bool:
    return bool(getattr(policy, name, False)) if policy is not None else False


def _float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError, OverflowError):
        return 0.0


__all__ = ["ControlPlane", "DefaultControlPlane"]
