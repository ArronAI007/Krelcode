"""Core-owned validation and commit of middleware state patches."""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Mapping, Protocol

from ...contracts.loop import Observation
from ...contracts.middleware import (
    MiddlewarePipelineResult,
    PatchOperation,
    PatchScope,
    StatePatch,
)
from ...contracts.plan import PlanControl
from ...contracts.tool import ToolContext


class MiddlewareCommitHost(Protocol):
    runtime_context: list[str]
    extension_metadata: dict[str, object]
    _turn_tool_context: ToolContext

    def add_runtime_context(
        self,
        contents: Iterable[str],
        source: str = "hook",
    ) -> int: ...

    async def _record_observation(self, observation: Observation) -> None: ...


class MiddlewareHostMixin:
    runtime_context: list[str]
    extension_metadata: dict[str, object]
    middleware_evaluations: list[dict[str, object]]
    _turn_tool_context: ToolContext

    def add_runtime_context(
        self,
        contents: Iterable[str],
        source: str = "hook",
    ) -> int:
        raise NotImplementedError

    async def _record_observation(self, observation: Observation) -> None:
        raise NotImplementedError

    async def _apply_middleware_result(
        self,
        result: MiddlewarePipelineResult,
    ) -> tuple[StatePatch, ...]:
        applied = await apply_middleware_result(self, result)
        self.middleware_evaluations.append(result.to_dict())
        return tuple(applied)


async def apply_middleware_result(
    host: MiddlewareCommitHost,
    result: MiddlewarePipelineResult,
) -> tuple[StatePatch, ...]:
    applied: list[StatePatch] = []
    for patch in result.patches:
        scope = PatchScope(patch.scope)
        operation = PatchOperation(patch.operation)
        if scope is PatchScope.RUNTIME_CONTEXT:
            if operation is PatchOperation.APPEND:
                values = (
                    patch.value
                    if isinstance(patch.value, (list, tuple))
                    else (patch.value,)
                )
                host.add_runtime_context(
                    (str(value) for value in values),
                    source="middleware",
                )
            elif operation is PatchOperation.REMOVE:
                host.runtime_context = [
                    item for item in host.runtime_context
                    if item != str(patch.value or "")
                ]
            else:
                raise ValueError(
                    "runtime_context Patch 只支持 append/remove")
        elif scope is PatchScope.STATE_METADATA:
            if operation in {
                PatchOperation.SET, PatchOperation.APPEND,
            }:
                host.extension_metadata[patch.path] = patch.value
            elif operation is PatchOperation.REMOVE:
                host.extension_metadata.pop(patch.path, None)
        elif scope is PatchScope.OBSERVATION:
            if not isinstance(patch.value, Observation):
                raise ValueError("observation Patch 必须携带 Observation")
            await host._record_observation(patch.value)
        elif scope is PatchScope.PLAN:
            if operation is not PatchOperation.SET or not isinstance(
                patch.value, Mapping
            ):
                raise ValueError("plan Patch 只支持 set mapping")
            host._turn_tool_context = replace(
                host._turn_tool_context,
                plan_control=PlanControl.from_mapping(patch.value),
            )
        applied.append(patch)
    return tuple(applied)


__all__ = [
    "MiddlewareCommitHost",
    "MiddlewareHostMixin",
    "apply_middleware_result",
]
