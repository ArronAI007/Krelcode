"""Managed enterprise policy.

The policy is deliberately a small, provider-neutral value object.  It is
loaded from the ``managed`` configuration layer (never from user/project/local
configuration in the normal CLI path) and is enforced at two boundaries:

* startup: options that would weaken the organization's security ceiling are
  rejected, and required SSO/audit prerequisites are checked;
* tool execution: enterprise deny lists, command/path restrictions, provider
  and tool allow lists are checked before the ordinary approval policy.

Keeping this module independent from the REPL, EventBus and concrete tools
makes it usable by the main agent, headless mode, SDK/stream-json and
subagents without developing a second permission implementation.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..tools.base import PermissionMode


class EnterprisePolicyError(ValueError):
    """A managed policy is malformed or cannot be enforced safely."""


class PolicyViolation(EnterprisePolicyError):
    """A requested operation is forbidden by managed policy."""


_PERMISSION_RANK = {
    PermissionMode.READ_ONLY: 0,
    PermissionMode.PLAN: 1,
    PermissionMode.DEFAULT: 2,
    PermissionMode.AUTO_APPROVE: 3,
}
_PERMISSION_BY_RANK = {value: key for key, value in _PERMISSION_RANK.items()}
_PERMISSION_ALIASES = {
    "readonly": "read-only",
    "read_only": "read-only",
    "read-only": "read-only",
    "plan": "plan",
    "default": "default",
    "auto": "auto-approve",
    "autoapprove": "auto-approve",
    "auto_approve": "auto-approve",
    "auto-approve": "auto-approve",
}


def _permission_mode(value: Any, *, field_name: str) -> PermissionMode:
    if isinstance(value, PermissionMode):
        return value
    normalized = str(value or "").strip().lower()
    normalized = _PERMISSION_ALIASES.get(normalized, normalized)
    try:
        return PermissionMode(normalized)
    except ValueError as exc:
        raise EnterprisePolicyError(
            f"enterprise.{field_name} 不是有效权限模式: {value!r}") from exc


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise EnterprisePolicyError(f"enterprise 布尔值无效: {value!r}")


def _as_strings(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise EnterprisePolicyError(f"enterprise.{field_name} 必须是字符串数组")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return tuple(dict.fromkeys(result))


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise EnterprisePolicyError(f"enterprise.{field_name} 必须是 TOML table")
    return value


@dataclass(frozen=True)
class EnterprisePolicy:
    """Normalized, immutable organization policy.

    ``enabled=False`` is the backward-compatible default.  Once enabled, all
    fields are still explicit; absent security flags default to fail-closed
    where a weaker option could otherwise silently win.
    """

    enabled: bool = False
    organization: str = ""
    policy_version: str = ""
    require_sso: bool = False
    max_permission_mode: PermissionMode = PermissionMode.DEFAULT
    allow_auto_approve: bool = False
    allow_no_sandbox: bool = False
    allow_telemetry: bool = True
    disable_plugins: bool = False
    allowed_providers: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disabled_tools: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] = ()
    source: str = ""
    raw_audit: Mapping[str, Any] = field(default_factory=dict, repr=False)
    raw_sso: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_mapping(
        cls, value: Optional[Mapping[str, Any]], *, source: str = "",
    ) -> "EnterprisePolicy":
        raw = _mapping(value, field_name="")
        if not raw:
            return cls(source=source)
        enabled = _as_bool(raw.get("enabled"), bool(raw))
        if not enabled:
            return cls(enabled=False, organization=str(raw.get("organization", "") or ""),
                       policy_version=str(raw.get("policy_version", "") or ""),
                       source=source)
        max_mode_raw = raw.get("max_permission_mode", "default")
        max_mode = _permission_mode(max_mode_raw, field_name="max_permission_mode")
        allow_auto = _as_bool(raw.get("allow_auto_approve"), False)
        # max_permission_mode=auto-approve is not a license to ignore the
        # separate auto-approve switch.
        if max_mode is PermissionMode.AUTO_APPROVE and not allow_auto:
            max_mode = PermissionMode.DEFAULT
        audit = _mapping(raw.get("audit"), field_name="audit")
        sso = _mapping(raw.get("sso"), field_name="sso")
        policy = cls(
            enabled=True,
            organization=str(raw.get("organization", "") or "").strip(),
            policy_version=str(raw.get("policy_version", "") or "").strip(),
            require_sso=_as_bool(raw.get("require_sso"), False),
            max_permission_mode=max_mode,
            allow_auto_approve=allow_auto,
            allow_no_sandbox=_as_bool(raw.get("allow_no_sandbox"), False),
            allow_telemetry=_as_bool(raw.get("allow_telemetry"), True),
            disable_plugins=_as_bool(raw.get("disable_plugins"), False),
            allowed_providers=tuple(_as_strings(
                raw.get("allowed_providers"), field_name="allowed_providers")),
            allowed_tools=tuple(_as_strings(
                raw.get("allowed_tools"), field_name="allowed_tools")),
            disabled_tools=tuple(_as_strings(
                raw.get("disabled_tools") or raw.get("denied_tools"),
                field_name="disabled_tools")),
            denied_paths=tuple(_as_strings(
                raw.get("denied_paths"), field_name="denied_paths")),
            denied_commands=tuple(_as_strings(
                raw.get("denied_commands"), field_name="denied_commands")),
            source=source,
            raw_audit=dict(audit),
            raw_sso=dict(sso),
        )
        policy._validate()
        return policy

    @classmethod
    def from_config(
        cls, value: Optional[Mapping[str, Any]], *, source: str = "",
    ) -> "EnterprisePolicy":
        """Compatibility alias for embedders using the older terminology."""
        return cls.from_mapping(value, source=source)

    def _validate(self) -> None:
        if not self.organization:
            raise EnterprisePolicyError(
                "enterprise.enabled=true 时必须配置 organization")
        if self.require_sso:
            issuer = str(self.raw_sso.get("issuer", "") or "").strip()
            client_id = str(self.raw_sso.get("client_id", "") or "").strip()
            if not issuer or not client_id:
                raise EnterprisePolicyError(
                    "enterprise.require_sso=true 时必须配置 sso.issuer 和 sso.client_id")
        audit_enabled = _as_bool(self.raw_audit.get("enabled"), False)
        audit_required = _as_bool(self.raw_audit.get("required"), False)
        if audit_required and not audit_enabled:
            raise EnterprisePolicyError(
                "enterprise.audit.required=true 时必须同时启用 audit.enabled")
        mode = str(self.raw_audit.get("mode", "hash-chain") or "hash-chain").lower()
        if audit_enabled and mode not in {"off", "hash-chain", "hmac"}:
            raise EnterprisePolicyError(
                "enterprise.audit.mode 必须是 off/hash-chain/hmac")
        if audit_enabled and mode == "hmac":
            env_name = str(self.raw_audit.get("hmac_key_env", "") or "").strip()
            if not env_name:
                raise EnterprisePolicyError(
                    "enterprise.audit.mode=hmac 时必须配置 hmac_key_env")

    @property
    def audit(self) -> Mapping[str, Any]:
        return self.raw_audit

    @property
    def sso(self) -> Mapping[str, Any]:
        return self.raw_sso

    def _tool_allowed(self, name: str) -> bool:
        if self.allowed_tools and not any(
                fnmatch.fnmatchcase(name, pattern)
                for pattern in self.allowed_tools):
            return False
        return not any(
            fnmatch.fnmatchcase(name, pattern)
            for pattern in self.disabled_tools)

    def deny_tool_reason(self, tool_call: Any, workspace_root: Path) -> Optional[str]:
        """Return a human-readable managed denial, or ``None``.

        This is intentionally synchronous and side-effect free.  It is safe to
        call from an approval policy, a tool pool precheck, or tests.
        """
        if not self.enabled:
            return None
        name = str(getattr(tool_call, "name", "") or "")
        if not self._tool_allowed(name):
            return f"企业策略禁止工具 {name}"
        arguments = getattr(tool_call, "arguments", {}) or {}
        raw_path = arguments.get("path", arguments.get("file_path")) \
            if hasattr(arguments, "get") else None
        if raw_path not in (None, ""):
            try:
                root = Path(workspace_root).expanduser().resolve()
                candidate = Path(str(raw_path)).expanduser()
                if not candidate.is_absolute():
                    candidate = root / candidate
                relative = candidate.resolve(strict=False).relative_to(root).as_posix()
            except (OSError, ValueError):
                relative = str(raw_path).replace(os.sep, "/")
            if any(fnmatch.fnmatchcase(relative, pattern)
                   or fnmatch.fnmatchcase(Path(relative).name, pattern)
                   for pattern in self.denied_paths):
                return f"企业策略禁止路径 {relative}"
        command = arguments.get("command") if hasattr(arguments, "get") else None
        if command and any(
                fnmatch.fnmatchcase(str(command), pattern)
                or fnmatch.fnmatchcase(str(command).split(None, 1)[0], pattern)
                for pattern in self.denied_commands):
            return "企业策略禁止该命令"
        return None

    def clamp_permission_mode(self, mode: PermissionMode | str) -> PermissionMode:
        if not self.enabled:
            return PermissionMode(mode)
        requested = _permission_mode(mode, field_name="permission_mode")
        if requested is PermissionMode.AUTO_APPROVE and not self.allow_auto_approve:
            requested = PermissionMode.DEFAULT
        if _PERMISSION_RANK[requested] > _PERMISSION_RANK[self.max_permission_mode]:
            return self.max_permission_mode
        return requested

    def validate_cli(self, args: Any) -> None:
        """Reject explicit switches that try to weaken managed policy."""
        if not self.enabled:
            return
        if getattr(args, "no_sandbox", False) and not self.allow_no_sandbox:
            raise PolicyViolation(
                "企业策略禁止 --no-sandbox；请联系管理员修改 managed policy")
        if getattr(args, "auto_approve", False) and not self.allow_auto_approve:
            raise PolicyViolation(
                "企业策略禁止 --auto-approve；请联系管理员修改 managed policy")
        requested = getattr(args, "permission_mode", None)
        if requested:
            mode = _permission_mode(requested, field_name="permission_mode")
            if _PERMISSION_RANK[mode] > _PERMISSION_RANK[self.max_permission_mode]:
                raise PolicyViolation(
                    "企业策略不允许权限模式 "
                    f"{mode.value}（上限 {self.max_permission_mode.value}）")

    def validate_provider(self, provider: str) -> None:
        if self.enabled and self.allowed_providers and provider not in self.allowed_providers:
            raise PolicyViolation(
                f"企业策略不允许 Provider {provider}；允许: "
                + ", ".join(self.allowed_providers))

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "organization": self.organization,
            "policy_version": self.policy_version,
            "require_sso": self.require_sso,
            "max_permission_mode": self.max_permission_mode.value,
            "allow_auto_approve": self.allow_auto_approve,
            "allow_no_sandbox": self.allow_no_sandbox,
            "allow_telemetry": self.allow_telemetry,
            "source": self.source,
        }


def effective_permission_mode(
    requested: PermissionMode | str,
    policy: Optional[EnterprisePolicy],
) -> PermissionMode:
    """Apply the managed ceiling to a requested mode."""
    mode = PermissionMode(requested)
    return policy.clamp_permission_mode(mode) if policy is not None else mode


def _managed_layer_data(
    config_bundle: Any,
) -> Optional[Mapping[str, Any]]:
    if config_bundle is None:
        return None
    for layer in getattr(config_bundle, "layers", ()) or ():
        if getattr(layer, "scope", "") == "managed" and getattr(layer, "exists", False):
            return getattr(layer, "data", None) or {}
    return None


def load_enterprise_policy(
    config: Optional[Mapping[str, Any]] = None,
    *,
    config_bundle: Any = None,
    source: str = "",
) -> EnterprisePolicy:
    """Load policy only from a managed layer when provenance is available.

    ``config`` is a compatibility fallback for embedders that construct policy
    directly in tests.  The CLI passes ``config_bundle`` and therefore never
    treats project/user configuration as enterprise policy.
    """
    managed = _managed_layer_data(config_bundle)
    if managed is None:
        raw = (config or {}).get("enterprise") if config is not None else None
        return EnterprisePolicy.from_mapping(raw, source=source or "explicit")
    return EnterprisePolicy.from_mapping(
        managed.get("enterprise") if isinstance(managed, Mapping) else None,
        source=source or "managed",
    )
