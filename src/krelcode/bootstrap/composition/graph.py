"""Explicit runtime composition graph for a bootstrapped application.

The graph separates process-facing runtime services from hot-reloadable
extension surfaces. It contains runtime-ready objects and narrow ports only;
construction belongs to ``bootstrap.application_stages``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol, Tuple

from ...agent.turn.loop import AgentLoop
from ...agent.turn.runtime import AgentRuntime
from ...agent.usage import UsageTracker
from ...core.events import EventBus
from ...core.hooks import HookPort
from ...core.redact import SecretRedactor
from ...extensions import ExtensionRecord, ExtensionRegistry
from ...plugins import PluginManager, PluginRecord
from ...session.commands.custom import CustomCommandManager
from ...session.commands.ports import (
    MCPSupervisorPort,
    ModelSwitchPort,
    SkillReloadPort,
)
from ...session.input import InputProcessor
from ...session.runtime_ports import (
    ApprovalBrokerPort,
    CheckpointPort,
    UserInputBrokerPort,
)
from ...skills import SkillManager
from ...subagent.definition import AgentDefinitionManager


class TelemetryRuntimePort(Protocol):
    """Lifecycle surface required by the application host."""

    def attach(self, bus: EventBus) -> None: ...

    async def close(self) -> None: ...


class AuditorPort(Protocol):
    """Lifecycle surface required to detach an enterprise auditor."""

    def detach(self) -> None: ...


class ObservabilityRuntimePort(Protocol):
    """Lifecycle surface for the isolated local event journal."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...


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
    telemetry_runtime: TelemetryRuntimePort
    auditor: Optional[AuditorPort]
    loop: AgentLoop
    observability: Optional[ObservabilityRuntimePort] = None


@dataclass(frozen=True)
class ExtensionRuntime:
    """Hot-reloadable extension surfaces exposed to command/tool consumers."""

    plugin_manager: PluginManager
    skill_manager: SkillManager
    agent_definitions: AgentDefinitionManager
    extension_registry: ExtensionRegistry
    custom_commands: CustomCommandManager
    refresh: Callable[[], Tuple[ExtensionRecord, ...]]
    reload_skills: SkillReloadPort
    reload_plugins: Callable[[], Awaitable[Tuple[PluginRecord, ...]]]
    reload_custom_commands: Callable[[], CustomCommandManager]


@dataclass(frozen=True)
class RuntimeGraph:
    """The complete runtime object graph owned by one application instance."""

    core: CoreRuntime
    extensions: ExtensionRuntime


__all__ = [
    "AuditorPort",
    "CoreRuntime",
    "ExtensionRuntime",
    "ObservabilityRuntimePort",
    "RuntimeGraph",
    "TelemetryRuntimePort",
]
