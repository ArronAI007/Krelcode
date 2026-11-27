"""Adapters from the semantic AgentLoop to runtime-facing ports."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence, cast

from krelcode.contracts.acceptance import AcceptanceEvaluatorPort
from krelcode.contracts.application import ApplicationRequest
from krelcode.contracts.governance import ControlPlanePort
from krelcode.contracts.runtime import (
    HarnessTurnControlPort,
    RuntimeResourcePort,
    RuntimeResourceRegistryPort,
    RuntimeSnapshot,
)
from krelcode.core.events import EventBus
from krelcode.core.messages import ImageRef, SteeringInput
from krelcode.agent.turn.loop import AgentLoop


class AgentLoopTurnAdapter(HarnessTurnControlPort):
    """Expose only the semantic controls required by AgentRuntime."""

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @property
    def loop(self) -> AgentLoop:
        """Legacy access for session command adapters during migration."""
        return self._loop

    @property
    def bus(self) -> EventBus:
        return self._loop.services.core.bus

    @property
    def workspace_root(self) -> Path:
        return self._loop.services.core.tool_context.workspace_root

    @property
    def tool_names(self) -> Sequence[str]:
        return tuple(self._loop.services.core.registry.names())

    @property
    def shell_jobs(self) -> object | None:
        return getattr(self._loop.services.core.tool_context, "shell_jobs", None)

    def bind_session(self, session_id: Optional[str]) -> None:
        runtime = replace(
            self._loop.services.core.tool_context.runtime,
            session_id=session_id,
        )
        self._loop.services.core.tool_context.runtime = runtime

    def set_acceptance_evaluator(
        self,
        evaluator: object | None,
    ) -> None:
        self._loop.acceptance_evaluator = cast(
            Optional[AcceptanceEvaluatorPort], evaluator)

    def set_control_plane(self, control_plane: object | None) -> None:
        resolved = cast(Optional[ControlPlanePort], control_plane)
        self._loop.services.core.control_plane = resolved
        self._loop.dependencies.control_plane = resolved

    def set_runtime_snapshot_provider(
        self,
        provider: Optional[Callable[[RuntimeSnapshot], RuntimeSnapshot]],
    ) -> None:
        self._loop.runtime_snapshot_provider = provider

    async def run_turn(
        self,
        user_input: str,
        images: Optional[list[ImageRef]] = None,
        *,
        turn_id: Optional[str] = None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[Sequence[str]] = None,
        application_request: Optional[ApplicationRequest] = None,
    ) -> str:
        return await self._loop.run_turn(
            user_input,
            images=images,
            turn_id=turn_id,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids,
            application_request=application_request,
        )

    def submit_steering(self, item: SteeringInput | str) -> None:
        self._loop.steering_queue.put_nowait(item)

    def drain_steering(self) -> list[SteeringInput | str]:
        pending: list[SteeringInput | str] = []
        while not self._loop.steering_queue.empty():
            try:
                pending.append(self._loop.steering_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return pending

    def abort_current_turn(self, reason: str = "用户中止") -> None:
        self._loop.abort_current_turn(reason)

    async def close_runtime_resources(self) -> None:
        compact_scheduler = self._loop.compact_scheduler
        self._loop.compact_scheduler = None
        if compact_scheduler is not None:
            await compact_scheduler.close()
        memory_dream_scheduler = self._loop.memory_dream_scheduler
        self._loop.memory_dream_scheduler = None
        if memory_dream_scheduler is not None:
            await memory_dream_scheduler.close()
        lsp = self._loop.services.extensions.lsp
        if lsp is not None:
            self._loop.services.extensions.lsp = None
            await lsp.close()


class InlineRuntimeResourceRegistry(RuntimeResourceRegistryPort):
    """Compatibility registry for directly constructed AgentRuntime values."""

    def __init__(self) -> None:
        self._resources: list[RuntimeResourcePort] = []
        self._names: set[str] = set()
        self._closed = False
        self.last_errors: tuple[str, ...] = ()

    def register(self, resource: RuntimeResourcePort) -> None:
        if self._closed:
            raise RuntimeError("RuntimeResourceRegistry 已关闭")
        if resource.resource_name in self._names:
            return
        self._names.add(resource.resource_name)
        self._resources.append(resource)

    async def close_all(self) -> tuple[str, ...]:
        if self._closed:
            return self.last_errors
        self._closed = True
        errors: list[str] = []
        for resource in reversed(self._resources):
            try:
                await resource.close()
            except Exception as exc:
                errors.append(
                    f"{resource.resource_name}: {type(exc).__name__}: {exc}")
        self.last_errors = tuple(errors)
        return self.last_errors


class AsyncCloseResource(RuntimeResourcePort):
    """Adapt a concrete async close method to RuntimeResourcePort."""

    def __init__(
        self,
        name: str,
        closer: Callable[[], Awaitable[None]],
    ) -> None:
        self._name = name
        self._closer = closer
        self._closed = False

    @property
    def resource_name(self) -> str:
        return self._name

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._closer()


def as_turn_control(
    host: AgentLoop | HarnessTurnControlPort,
) -> HarnessTurnControlPort:
    if isinstance(host, AgentLoop):
        return AgentLoopTurnAdapter(host)
    return host


__all__ = [
    "AgentLoopTurnAdapter",
    "AsyncCloseResource",
    "InlineRuntimeResourceRegistry",
    "as_turn_control",
]
