"""Provider-neutral runtime and permission values.

These values are shared by the agent loop, tool catalogue, approval policy,
subagents and prompt assembly.  They deliberately depend only on the standard
library so no adapter layer has to import another adapter to describe runtime
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional


class RunMode(str, Enum):
    """当前进程发起查询的运行形态；工具池和权限据此做差异化暴露。"""

    REPL = "repl"
    PRINT = "print"
    JSON = "json"
    SDK = "sdk"
    REVIEW = "review"
    SUBAGENT = "subagent"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    READ_ONLY = "read-only"
    AUTO_APPROVE = "auto-approve"
    PLAN = "plan"


@dataclass(frozen=True)
class RuntimeSnapshot:
    """一轮查询的不可变运行时快照（AppState 的窄化替代）。"""

    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    run_mode: RunMode = RunMode.REPL
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    features: FrozenSet[str] = frozenset()
    disabled_tools: FrozenSet[str] = frozenset()
    alive_mcp_servers: FrozenSet[str] = frozenset()
    mcp_health_known: bool = False
    tool_allowlist: Optional[FrozenSet[str]] = None
    mcp_allowlist: Optional[FrozenSet[str]] = None
    # Organization-level allow/deny patterns are separate from a Skill's
    # temporary allowlist. The tool pool applies them before advertising a
    # schema, so hiding a tool and denying its execution cannot drift apart.
    managed_tool_allowlist: Optional[FrozenSet[str]] = None
    managed_tool_denylist: Optional[FrozenSet[str]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_mode", RunMode(self.run_mode))
        object.__setattr__(
            self, "permission_mode", PermissionMode(self.permission_mode))
        object.__setattr__(self, "features", frozenset(self.features or ()))
        object.__setattr__(
            self, "disabled_tools", frozenset(self.disabled_tools or ()))
        object.__setattr__(
            self, "alive_mcp_servers",
            frozenset(self.alive_mcp_servers or ()))
        if self.tool_allowlist is not None:
            object.__setattr__(
                self, "tool_allowlist", frozenset(self.tool_allowlist))
        if self.mcp_allowlist is not None:
            object.__setattr__(
                self, "mcp_allowlist", frozenset(self.mcp_allowlist))
        if self.managed_tool_allowlist is not None:
            object.__setattr__(
                self, "managed_tool_allowlist",
                frozenset(self.managed_tool_allowlist))
        if self.managed_tool_denylist is not None:
            object.__setattr__(
                self, "managed_tool_denylist",
                frozenset(self.managed_tool_denylist))


__all__ = ["PermissionMode", "RunMode", "RuntimeSnapshot"]
