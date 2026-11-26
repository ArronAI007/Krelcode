"""Tool execution runtimes with one normalized result contract."""

from __future__ import annotations

from typing import Mapping, Optional

from ...contracts.json import JsonValueError
from ...contracts.tool import (
    Tool,
    ToolExecutionRuntime,
    ToolResult,
)
from ...contracts.tool_execution import (
    ToolExecutionResult,
    ToolIntent,
    ToolResolverPort,
)
from ...core.messages import INVALID_ARGUMENTS_KEY
from ..schema import validate_tool_arguments


class _CallableToolResolver:
    def __init__(self, resolver: object) -> None:
        self._resolver = resolver

    def get_tool(self, name: str) -> Optional[Tool]:
        getter = getattr(self._resolver, "get", None)
        return getter(name) if callable(getter) else None


class _BaseToolRuntime:
    runtime = ToolExecutionRuntime.LOCAL

    def __init__(self, resolver: ToolResolverPort) -> None:
        self.resolver = resolver

    def supports(self, intent: ToolIntent) -> bool:
        return intent.runtime is self.runtime

    async def execute(self, intent: ToolIntent) -> ToolExecutionResult:
        if not self.supports(intent):
            return self._error(
                intent,
                f"工具 {intent.call.name} 被路由到不匹配的执行运行时",
                error_kind="runtime_mismatch",
            )
        if intent.call.has_invalid_arguments:
            return self._error(
                intent,
                "工具参数不是合法 JSON: "
                f"{intent.call.arguments[INVALID_ARGUMENTS_KEY]!r}",
                error_kind="invalid_arguments_json",
            )
        try:
            arguments = intent.arguments
        except JsonValueError as exc:
            return self._error(
                intent,
                f"工具参数不是合法 JSON: {exc}",
                error_kind="invalid_arguments_json",
            )

        tool = self.resolver.get_tool(intent.call.name)
        if tool is None:
            return self._error(
                intent,
                f"执行运行时未找到工具: {intent.call.name}",
                error_kind="tool_not_found",
            )
        validation_errors = validate_tool_arguments(
            intent.parameters, arguments)
        if validation_errors:
            rendered = [error.render() for error in validation_errors[:20]]
            if len(validation_errors) > 20:
                rendered.append(
                    f"…另有 {len(validation_errors) - 20} 个错误")
            return self._error(
                intent,
                f"工具参数校验失败: {intent.call.name}\n- "
                + "\n- ".join(rendered),
                error_kind="schema_validation",
            )

        try:
            raw = await tool.execute(arguments, intent.context)
        except Exception as exc:
            return self._error(
                intent,
                f"工具执行出错: {type(exc).__name__}: {exc}",
                error_kind="runtime_exception",
                exception_type=type(exc).__name__,
            )
        if not isinstance(raw, ToolResult):
            return self._error(
                intent,
                f"工具返回了无效结果类型: {type(raw).__name__}",
                error_kind="invalid_result",
            )
        return ToolExecutionResult.from_tool_result(
            raw,
            runtime=self.runtime,
            diagnostics=self._diagnostics(intent),
        )

    def _diagnostics(
        self,
        intent: ToolIntent,
        **extra: object,
    ) -> Mapping[str, object]:
        return {
            "source": intent.descriptor.source.value,
            "runtime": self.runtime.value,
            **extra,
        }

    def _error(
        self,
        intent: ToolIntent,
        content: str,
        *,
        error_kind: str,
        **extra: object,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            content=content,
            is_error=True,
            runtime=self.runtime,
            diagnostics=self._diagnostics(
                intent,
                error_kind=error_kind,
                **extra,
            ),
        )


class LocalToolRuntime(_BaseToolRuntime):
    """Execute in-process tools without a remote protocol boundary."""

    runtime = ToolExecutionRuntime.LOCAL


class MCPRuntime(_BaseToolRuntime):
    """Execute MCP-adapter tools while keeping wire details below the agent."""

    runtime = ToolExecutionRuntime.MCP

    def _diagnostics(
        self,
        intent: ToolIntent,
        **extra: object,
    ) -> Mapping[str, object]:
        return {
            **super()._diagnostics(intent, **extra),
            "mcp_server": intent.descriptor.mcp_server or "",
        }


class SandboxRuntime(_BaseToolRuntime):
    """Execute process-capable local tools through Sandbox-aware contexts."""

    runtime = ToolExecutionRuntime.SANDBOX

    def _diagnostics(
        self,
        intent: ToolIntent,
        **extra: object,
    ) -> Mapping[str, object]:
        sandbox = intent.context.sandbox
        return {
            **super()._diagnostics(intent, **extra),
            "sandbox_enabled": intent.context.sandbox_enabled,
            "sandbox_backend": (
                str(getattr(sandbox, "backend", "none"))
                if sandbox is not None else "none"
            ),
        }


class RemoteRuntime(_BaseToolRuntime):
    """Execute tools whose implementation lives behind a remote service."""

    runtime = ToolExecutionRuntime.REMOTE


def resolver_from_registry(registry: object) -> ToolResolverPort:
    return _CallableToolResolver(registry)


__all__ = [
    "LocalToolRuntime",
    "MCPRuntime",
    "RemoteRuntime",
    "SandboxRuntime",
    "resolver_from_registry",
]
