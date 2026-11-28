"""Authority and approval envelopes for an Agent Contract."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Sequence
from urllib.parse import urlparse


class ApprovalDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ApprovalRule:
    id: str
    decision: ApprovalDecision | str
    action_pattern: str = "*"
    resource_pattern: str = "*"
    system: str = ""
    reason: str = ""
    approver: str = "contract"

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id or "").strip())
        if not self.id:
            raise ValueError("ApprovalRule.id 不能为空")
        object.__setattr__(
            self, "decision", ApprovalDecision(self.decision))
        for name in (
            "action_pattern", "resource_pattern", "system", "reason",
            "approver",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "*").strip())

    def matches(
        self,
        *,
        tool_name: str,
        resource: str,
        system: str,
    ) -> bool:
        return (
            (not self.system or self.system == "*" or self.system == system)
            and fnmatch.fnmatchcase(tool_name, self.action_pattern)
            and fnmatch.fnmatchcase(resource, self.resource_pattern)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "decision": ApprovalDecision(self.decision).value,
            "action_pattern": self.action_pattern,
            "resource_pattern": self.resource_pattern,
            "system": self.system,
            "reason": self.reason,
            "approver": self.approver,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ApprovalRule":
        return cls(
            id=str(raw.get("id") or ""),
            decision=str(
                raw.get("decision") or ApprovalDecision.ALLOW.value),
            action_pattern=str(raw.get("action_pattern") or "*"),
            resource_pattern=str(raw.get("resource_pattern") or "*"),
            system=str(raw.get("system") or ""),
            reason=str(raw.get("reason") or ""),
            approver=str(raw.get("approver") or "contract"),
        )


@dataclass(frozen=True)
class AuthorityDecision:
    decision: ApprovalDecision
    reason: str = ""
    rule_id: str = ""
    system: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision", ApprovalDecision(self.decision))


@dataclass(frozen=True)
class AuthorityEnvelope:
    allowed_systems: tuple[str, ...] = ("workspace",)
    denied_systems: tuple[str, ...] = ()
    allowed_tools: Optional[tuple[str, ...]] = None
    denied_tools: tuple[str, ...] = ()
    allowed_mcp_servers: Optional[tuple[str, ...]] = None
    denied_mcp_servers: tuple[str, ...] = ()
    network_allowed: bool = False
    allowed_domains: tuple[str, ...] = ()
    write_scope: str = "workspace"
    denied_paths: tuple[str, ...] = ()
    approval_rules: tuple[ApprovalRule, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "allowed_systems", "denied_systems", "denied_tools",
            "allowed_mcp_servers", "denied_mcp_servers",
            "allowed_domains", "denied_paths",
        ):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                None if value is None else _string_tuple(value),
            )
        if self.allowed_tools is not None:
            object.__setattr__(
                self, "allowed_tools", _string_tuple(self.allowed_tools))
        object.__setattr__(
            self, "network_allowed", bool(self.network_allowed))
        object.__setattr__(
            self, "write_scope", str(self.write_scope or "workspace"))
        object.__setattr__(
            self, "approval_rules", tuple(self.approval_rules))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def evaluate_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        mcp_server: str = "",
        tool_source: str = "",
    ) -> AuthorityDecision:
        system = tool_system(tool_name, mcp_server, tool_source)
        resource = tool_resource(tool_name, arguments, mcp_server)
        if system in self.denied_systems:
            return AuthorityDecision(
                ApprovalDecision.DENY,
                f"系统被契约禁止: {system}",
                system=system,
            )
        if self.allowed_systems and system not in self.allowed_systems:
            return AuthorityDecision(
                ApprovalDecision.DENY,
                f"系统不在契约允许范围: {system}",
                system=system,
            )
        if any(
            fnmatch.fnmatchcase(tool_name, pattern)
            for pattern in self.denied_tools
        ):
            return AuthorityDecision(
                ApprovalDecision.DENY,
                f"工具被契约禁止: {tool_name}",
                system=system,
            )
        if self.allowed_tools is not None and not any(
            fnmatch.fnmatchcase(tool_name, pattern)
            for pattern in self.allowed_tools
        ):
            return AuthorityDecision(
                ApprovalDecision.DENY,
                f"工具不在契约允许列表: {tool_name}",
                system=system,
            )
        if mcp_server:
            if mcp_server in self.denied_mcp_servers:
                return AuthorityDecision(
                    ApprovalDecision.DENY,
                    f"MCP server 被契约禁止: {mcp_server}",
                    system=system,
                )
            if (
                self.allowed_mcp_servers is not None
                and mcp_server not in self.allowed_mcp_servers
            ):
                return AuthorityDecision(
                    ApprovalDecision.DENY,
                    f"MCP server 不在契约允许列表: {mcp_server}",
                    system=system,
                )
        if system == "network" and not self.network_allowed:
            return AuthorityDecision(
                ApprovalDecision.DENY,
                "契约禁止网络访问",
                system=system,
            )
        if system == "network" and self.allowed_domains:
            host = _network_host(arguments)
            if not host or not any(
                fnmatch.fnmatchcase(host, pattern)
                for pattern in self.allowed_domains
            ):
                return AuthorityDecision(
                    ApprovalDecision.DENY,
                    f"网络目标不在契约允许域名: {host or '(unknown)'}",
                    system=system,
                )
        path = str(arguments.get("path") or arguments.get("file_path") or "")
        if path and any(
            fnmatch.fnmatchcase(path, pattern)
            for pattern in self.denied_paths
        ):
            return AuthorityDecision(
                ApprovalDecision.DENY,
                f"路径被契约禁止: {path}",
                system=system,
            )
        matched_rules = [
            rule for rule in self.approval_rules
            if rule.matches(
                tool_name=tool_name,
                resource=resource,
                system=system,
            )
        ]
        for rule in matched_rules:
            if ApprovalDecision(rule.decision) is ApprovalDecision.DENY:
                return AuthorityDecision(
                    ApprovalDecision.DENY,
                    rule.reason or f"审批规则拒绝: {rule.id}",
                    rule.id,
                    system,
                )
        for rule in matched_rules:
            if (
                ApprovalDecision(rule.decision)
                is ApprovalDecision.REQUIRE_APPROVAL
            ):
                return AuthorityDecision(
                    ApprovalDecision.REQUIRE_APPROVAL,
                    rule.reason or f"审批规则要求人工批准: {rule.id}",
                    rule.id,
                    system,
                )
        return AuthorityDecision(ApprovalDecision.ALLOW, system=system)

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_systems": list(self.allowed_systems),
            "denied_systems": list(self.denied_systems),
            "allowed_tools": (
                list(self.allowed_tools)
                if self.allowed_tools is not None else None),
            "denied_tools": list(self.denied_tools),
            "allowed_mcp_servers": (
                list(self.allowed_mcp_servers)
                if self.allowed_mcp_servers is not None else None),
            "denied_mcp_servers": list(self.denied_mcp_servers),
            "network_allowed": self.network_allowed,
            "allowed_domains": list(self.allowed_domains),
            "write_scope": self.write_scope,
            "denied_paths": list(self.denied_paths),
            "approval_rules": [
                rule.to_dict() for rule in self.approval_rules
            ],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "AuthorityEnvelope":
        rules_raw = raw.get("approval_rules")
        rules = tuple(
            ApprovalRule.from_dict(item)
            for item in (rules_raw if isinstance(rules_raw, list) else ())
            if isinstance(item, Mapping)
        )
        allowed_tools = raw.get("allowed_tools")
        allowed_mcp = raw.get("allowed_mcp_servers")
        return cls(
            allowed_systems=_string_tuple(
                raw.get("allowed_systems") or ("workspace",)),
            denied_systems=_string_tuple(raw.get("denied_systems")),
            allowed_tools=(
                _string_tuple(allowed_tools)
                if isinstance(allowed_tools, list) else None),
            denied_tools=_string_tuple(raw.get("denied_tools")),
            allowed_mcp_servers=(
                _string_tuple(allowed_mcp)
                if isinstance(allowed_mcp, list) else None),
            denied_mcp_servers=_string_tuple(
                raw.get("denied_mcp_servers")),
            network_allowed=bool(raw.get("network_allowed", False)),
            allowed_domains=_string_tuple(raw.get("allowed_domains")),
            write_scope=str(raw.get("write_scope") or "workspace"),
            denied_paths=_string_tuple(raw.get("denied_paths")),
            approval_rules=rules,
            metadata=_mapping(raw.get("metadata")),
        )


def tool_system(tool_name: str, mcp_server: str, tool_source: str) -> str:
    if mcp_server or tool_source == "mcp" or tool_name.startswith("mcp__"):
        return "mcp"
    if tool_name in {
        "run_command", "run_skill_script", "job_start", "job_output",
        "job_kill",
    }:
        return "shell"
    if tool_name in {"web_fetch", "web_search"} or tool_source == "web":
        return "network"
    return "workspace"


def tool_resource(
    tool_name: str,
    arguments: Mapping[str, object],
    mcp_server: str,
) -> str:
    for key in ("path", "file_path", "url", "command", "uri"):
        value = arguments.get(key)
        if value not in (None, ""):
            return f"{mcp_server}:{value}" if mcp_server else str(value)
    return f"{mcp_server}:{tool_name}" if mcp_server else tool_name


def _network_host(arguments: Mapping[str, object]) -> str:
    raw = str(arguments.get("url") or "").strip()
    if not raw:
        query = str(arguments.get("query") or "").strip()
        if query.startswith(("http://", "https://")):
            raw = query
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return str(parsed.hostname or "").lower()


def _mapping(raw: object) -> dict[str, object]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[object] = (value,)
    elif isinstance(value, Sequence):
        values = value
    else:
        values = ()
    return tuple(
        text for item in values
        if (text := str(item).strip())
    )


__all__ = [
    "ApprovalDecision",
    "ApprovalRule",
    "AuthorityDecision",
    "AuthorityEnvelope",
    "tool_resource",
    "tool_system",
]
