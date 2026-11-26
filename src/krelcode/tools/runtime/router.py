"""Deterministic route from a normalized ToolIntent to one runtime."""

from __future__ import annotations

from typing import Mapping, Optional

from ...contracts.tool import ToolExecutionRuntime
from ...contracts.tool_execution import (
    ExecutionRuntimePort,
    ToolExecutionResult,
    ToolIntent,
    ToolResolverPort,
)
from .execution import (
    LocalToolRuntime,
    MCPRuntime,
    RemoteRuntime,
    SandboxRuntime,
)


class ExecutionRuntimeRouter:
    """Route intents without exposing concrete tool storage to the agent."""

    def __init__(
        self,
        resolver: ToolResolverPort,
        *,
        runtimes: Optional[Mapping[
            ToolExecutionRuntime, ExecutionRuntimePort]] = None,
    ) -> None:
        self._runtimes = dict(runtimes or {
            ToolExecutionRuntime.LOCAL: LocalToolRuntime(resolver),
            ToolExecutionRuntime.MCP: MCPRuntime(resolver),
            ToolExecutionRuntime.SANDBOX: SandboxRuntime(resolver),
            ToolExecutionRuntime.REMOTE: RemoteRuntime(resolver),
        })

    def supports(self, intent: ToolIntent) -> bool:
        runtime = self._runtimes.get(intent.runtime)
        return runtime is not None and runtime.supports(intent)

    async def execute(self, intent: ToolIntent) -> ToolExecutionResult:
        runtime = self._runtimes.get(intent.runtime)
        if runtime is None:
            return ToolExecutionResult(
                content=(
                    f"工具执行运行时不可用: {intent.runtime.value}"
                    f"（工具: {intent.call.name}）"
                ),
                is_error=True,
                runtime=intent.runtime,
                diagnostics={
                    "source": intent.descriptor.source.value,
                    "runtime": intent.runtime.value,
                    "error_kind": "runtime_unavailable",
                },
            )
        return await runtime.execute(intent)


__all__ = ["ExecutionRuntimeRouter"]
