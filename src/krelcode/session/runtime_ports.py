"""Explicit runtime ports consumed by the session orchestration layer.

The composition root may build concrete MCP supervisors, checkpoint managers,
hook runners and interaction brokers, but the session runner must not depend on
those implementations.  This module is the single boundary for that bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from ..core.hooks import HookPort
from ..core.interactions import UserInputPort
from ..core.messages import ToolCall
from .commands.ports import CheckpointManagerPort, MCPSupervisorPort


class CheckpointGroupPort(Protocol):
    @property
    def seq(self) -> int: ...


class RestoreTurnsResultPort(Protocol):
    @property
    def groups(self) -> int: ...

    @property
    def restored(self) -> Sequence[str]: ...

    @property
    def deleted(self) -> Sequence[str]: ...


class CheckpointPort(CheckpointManagerPort, Protocol):
    """File checkpoints needed by session commands and conversation rewind."""

    def groups_for_turn(
        self,
        turn_id: str,
        *,
        session_id: str = "",
    ) -> Sequence[CheckpointGroupPort]: ...

    def restore_turns(
        self,
        turn_ids: Sequence[str],
        *,
        session_id: str = "",
    ) -> RestoreTurnsResultPort: ...


class ApprovalBrokerPort(Protocol):
    """Approval rendezvous shared by REPL and stream-json transports."""

    @property
    def pending(self) -> object | None: ...

    async def request(self, tool_call: ToolCall, reason: str) -> str: ...

    def expect(self, request_id: str) -> None: ...

    async def next_request(self) -> object | None: ...

    def resolve(self, raw: str) -> Optional[str]: ...

    def resolve_for(self, request_id: str, raw: str) -> Optional[str]: ...

    def cancel_all(self) -> None: ...

    def close(self) -> None: ...


class UserInputResolutionPort(Protocol):
    @property
    def accepted(self) -> bool: ...

    @property
    def value(self) -> Optional[str]: ...

    @property
    def reason(self) -> str: ...


class UserInputBrokerPort(UserInputPort, Protocol):
    """User-input rendezvous shared by REPL and stream-json transports."""

    @property
    def pending(self) -> object | None: ...

    async def next_request(self) -> object | None: ...

    def resolve(
        self,
        raw: str,
        *,
        request_id: str = "",
    ) -> UserInputResolutionPort: ...

    def cancel_request(self, request_id: str) -> bool: ...

    def cancel_all(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SessionRuntimePorts:
    """Long-lived ports injected into one session run."""

    supervisor: Optional[MCPSupervisorPort] = None
    checkpoints: Optional[CheckpointPort] = None
    hooks: Optional[HookPort] = None
    approval: Optional[ApprovalBrokerPort] = None
    user_input: Optional[UserInputBrokerPort] = None


__all__ = [
    "ApprovalBrokerPort",
    "CheckpointGroupPort",
    "CheckpointPort",
    "RestoreTurnsResultPort",
    "SessionRuntimePorts",
    "UserInputBrokerPort",
    "UserInputResolutionPort",
]
