"""Provider-neutral tool execution contracts.

The agent turn layer creates a ``ToolIntent`` and delegates it through an
``ExecutionRuntimePort``.  Concrete runtimes own tool resolution, schema
validation and invocation, then return the same ``ToolExecutionResult`` shape
regardless of whether execution happened locally, through MCP, inside a
Sandbox, or through a remote agent service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Tuple

from ..core.messages import ToolCall, ToolResultBlock
from .json import JsonObject, ensure_json_object
from .tool import (
    Tool,
    ToolContext,
    ToolDescriptor,
    ToolExecutionRuntime,
    ToolResult,
)


@dataclass(frozen=True)
class ToolIntent:
    """One normalized tool call ready for an execution runtime."""

    call: ToolCall
    descriptor: ToolDescriptor
    parameters: Mapping[str, object]
    context: ToolContext

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )

    @property
    def runtime(self) -> ToolExecutionRuntime:
        return self.descriptor.resolved_execution_runtime

    @property
    def arguments(self) -> JsonObject:
        return ensure_json_object(
            self.call.arguments, path=f"ToolIntent[{self.call.name}].arguments")


@dataclass(frozen=True)
class ToolExecutionResult:
    """Stable result returned by every execution runtime."""

    content: str
    is_error: bool = False
    data: Mapping[str, object] = field(default_factory=dict)
    blocks: Tuple[ToolResultBlock, ...] = ()
    runtime: ToolExecutionRuntime = ToolExecutionRuntime.LOCAL
    diagnostics: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", str(self.content))
        object.__setattr__(self, "is_error", bool(self.is_error))
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "runtime", ToolExecutionRuntime(self.runtime))
        object.__setattr__(
            self, "data", MappingProxyType(dict(self.data)))
        object.__setattr__(
            self, "diagnostics", MappingProxyType(dict(self.diagnostics)))

    def to_tool_result(self) -> ToolResult:
        """Adapt to the model/history compatibility DTO."""
        return ToolResult(
            content=self.content,
            is_error=self.is_error,
            data=ensure_json_object(
                dict(self.data), path="ToolExecutionResult.data"),
            blocks=self.blocks,
        )

    @classmethod
    def from_tool_result(
        cls,
        result: ToolResult,
        *,
        runtime: ToolExecutionRuntime,
        diagnostics: Optional[Mapping[str, object]] = None,
    ) -> "ToolExecutionResult":
        return cls(
            content=result.content,
            is_error=result.is_error,
            data=dict(result.data),
            blocks=tuple(result.blocks),
            runtime=runtime,
            diagnostics=diagnostics or {},
        )


class ToolResolverPort(Protocol):
    """Resolve an opaque tool instance without exposing registry internals."""

    def get_tool(self, name: str) -> Optional[Tool]: ...


class ExecutionRuntimePort(Protocol):
    """Execute a normalized intent and return a normalized result."""

    def supports(self, intent: ToolIntent) -> bool: ...

    async def execute(self, intent: ToolIntent) -> ToolExecutionResult: ...


__all__ = [
    "ExecutionRuntimePort",
    "ToolExecutionResult",
    "ToolIntent",
    "ToolResolverPort",
]
