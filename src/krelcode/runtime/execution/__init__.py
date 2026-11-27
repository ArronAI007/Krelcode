"""Canonical runtime execution facade."""

from krelcode.tools.runtime import (
    LocalToolRuntime,
    MCPRuntime,
    RemoteRuntime,
    SandboxRuntime,
)
from krelcode.tools.runtime.router import ExecutionRuntimeRouter
from .task_runtime import LocalRuntimeTaskHandle, LocalTaskRuntime

__all__ = [
    "ExecutionRuntimeRouter",
    "LocalRuntimeTaskHandle",
    "LocalTaskRuntime",
    "LocalToolRuntime",
    "MCPRuntime",
    "RemoteRuntime",
    "SandboxRuntime",
]
