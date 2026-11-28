"""Stable subagent state and manager-port contracts.

Tool adapters depend on this module rather than the concrete manager so the
manager can construct its tool surface without creating a module cycle.
"""

from __future__ import annotations

from typing import Any, Protocol, Tuple

STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_KILLED = "killed"

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_KILLED,
})


class SubagentManagerPort(Protocol):
    """Operations required by the parent-facing subagent tools."""

    async def spawn(self, *args: Any, **kwargs: Any) -> Any: ...
    async def wait(self, agent_id: str, *, timeout: float = 60.0) -> Any: ...
    async def wait_many(self, agent_ids: list[str], *, timeout: float = 60.0) -> list[Any]: ...
    async def send_input(
        self, agent_id: str, message: str, *, interrupt: bool = False,
    ) -> Tuple[Any, str]: ...
    def feed(self, agent_id: str, *, after_sequence: int = 0, limit: int = 100) -> Any: ...
    async def pause(self, agent_id: str) -> Any: ...
    async def resume(self, agent_id: str) -> Any: ...
    async def kill(self, agent_id: str) -> Any: ...
    async def retry(self, agent_id: str, *, definition: str = "") -> Any: ...
    async def record_delegation_result(
        self, handle: Any, result: Any) -> None: ...
    def get(self, agent_id: str) -> Any: ...
    def definition_lines(self) -> list[str]: ...
    def status_lines(self) -> list[str]: ...
