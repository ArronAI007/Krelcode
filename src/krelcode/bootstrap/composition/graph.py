"""Explicit runtime composition graph for a bootstrapped application.

The graph separates process-facing core services from hot-reloadable extension
surfaces.  It deliberately contains only runtime-ready objects and callables;
construction remains in ``bootstrap.application``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ...agent.loop import AgentLoop
from ...agent.runtime import AgentRuntime
from ...agent.usage import UsageTracker
from ...core.hooks import HookPort
from ...core.redact import SecretRedactor
from ...session.commands.ports import MCPSupervisorPort, ModelSwitchPort
from ...session.input import InputProcessor
from ...session.runtime_ports import (
    ApprovalBrokerPort,
    CheckpointPort,
    UserInputBrokerPort,
)


@dataclass(frozen=True)
class CoreRuntime:
    """Core services required to run and close one application instance."""

    runtime: AgentRuntime
    supervisor: Optional[MCPSupervisorPort]
    checkpoints: Optional[CheckpointPort]
    usage: UsageTracker
    model_switch: Optional[ModelSwitchPort]
    redactor: SecretRedactor
    input_processor: Optional[InputProcessor]
    approval_broker: Optional[ApprovalBrokerPort]
    input_broker: Optional[UserInputBrokerPort]
    hooks: Optional[HookPort]
    telemetry_runtime: Any
    auditor: Any
    loop: AgentLoop


@dataclass(frozen=True)
class ExtensionRuntime:
    """Hot-reloadable extension surfaces exposed to command/tool consumers."""

    plugin_manager: Any
    skill_manager: Any
    agent_definitions: Any
    extension_registry: Any
    custom_commands: Any
    refresh: Callable[[], Any]
    reload_skills: Callable[[], Any]
    reload_plugins: Callable[[], Awaitable[Any]]
    reload_custom_commands: Callable[[], Any]


@dataclass(frozen=True)
class RuntimeGraph:
    """The complete runtime object graph owned by one application instance."""

    core: CoreRuntime
    extensions: ExtensionRuntime


__all__ = ["CoreRuntime", "ExtensionRuntime", "RuntimeGraph"]
