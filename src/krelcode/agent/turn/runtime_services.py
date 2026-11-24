"""Typed service container owned by one long-lived ``AgentLoop``.

Runtime *state* changes with every turn (history, prompt plans, queue handles).
Runtime *services* are stable resources with a lifecycle longer than a turn
(provider, tools, stores, registries, MCP and session services).  Keeping the
two together behind a dynamic dictionary made dependencies impossible to
discover with the type checker.

This module introduces the explicit service boundary used by new code.  Legacy
``loop.<service>`` attributes are routed here by :class:`RuntimeStateHost` while
the remaining call sites migrate incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol, Sequence

from .runtime_ports import (
    CompactCheckpointStorePort,
    ContextAssemblerPort,
    PromptAssemblerPort,
    ReminderCatalogPort,
    ToolMicrocompactorPort,
    WorkingContextPort,
)

if TYPE_CHECKING:
    from krelcode.agent.context.compression import ContextManager
    from krelcode.agent.context.context_checkpoint import (
        ContextCheckpointCoordinator,
        ContextCheckpointStore,
    )
    from krelcode.agent.prompt.fragments import PromptFragmentRegistry
    from krelcode.agent.prompt.prompt_surface import PromptSurfaceRegistry
    from krelcode.agent.rules import RuleLoader
    from krelcode.core.events import EventBus
    from krelcode.core.hooks import HookPort
    from krelcode.core.interactions import UserInputPort
    from krelcode.core.redact import SecretRedactor
    from krelcode.contracts.service_ports import EnterprisePolicyPort, PrincipalPort
    from krelcode.contracts.tool import ToolContext
    from krelcode.llm.base import LLMProvider
    from krelcode.safety.policy import ApprovalPolicy
    from krelcode.tools.registry import ToolRegistry


class MemoryServicePort(Protocol):
    """Methods consumed from the session-scoped memory service.

    ``memory`` is intentionally described structurally because ``agent`` must
    not import the concrete ``session`` implementation.
    """

    redactor: Optional["SecretRedactor"]

    def bind_session(self, session_id: str) -> None: ...

    def copy_session(self, source: str, target: str) -> None: ...

    def search(self, query: str, *, limit: int = 8) -> object: ...


class SessionRecorderPort(Protocol):
    def path(self) -> object: ...


class SkillServicePort(Protocol):
    def prompt_index(self) -> str: ...

    def apply_to_prompt(self, prompt: str) -> str: ...


class PluginServicePort(Protocol):
    def status_lines(self) -> list[str]: ...

    def reload(self) -> object: ...


class AgentDefinitionServicePort(Protocol):
    """Marker protocol for the subagent definition registry."""


class MCPServicePort(Protocol):
    def status(self) -> list[object]: ...

    async def close_all(self) -> None: ...


class CommandServicePort(Protocol):
    def names(self) -> tuple[str, ...]: ...


@dataclass(slots=True)
class CoreRuntimeServices:
    """Provider, tools and turn-independent safety primitives."""

    llm: "LLMProvider"
    registry: "ToolRegistry"
    tool_context: "ToolContext"
    bus: "EventBus"
    policy: Optional["ApprovalPolicy"] = None
    redactor: Optional["SecretRedactor"] = None
    hook_port: Optional["HookPort"] = None
    user_input_port: Optional["UserInputPort"] = None


@dataclass(slots=True)
class ContextRuntimeServices:
    """Stable context assembly, persistence and rule-loading services."""

    manager: Optional["ContextManager"]
    assembler: ContextAssemblerPort
    working_context: WorkingContextPort
    tool_microcompactor: ToolMicrocompactorPort
    reminder_catalog: ReminderCatalogPort
    compact_checkpoint: Optional[CompactCheckpointStorePort] = None
    checkpoint: Optional["ContextCheckpointStore"] = None
    checkpoint_coordinator: Optional["ContextCheckpointCoordinator"] = None
    rule_loader: Optional["RuleLoader"] = None
    directory_rule_source: object | None = None


@dataclass(slots=True)
class PromptRuntimeServices:
    """Prompt assembly and discovery services."""

    assembler: PromptAssemblerPort
    surface_registry: "PromptSurfaceRegistry"
    registry: Optional["PromptFragmentRegistry"] = None


@dataclass(slots=True)
class ExtensionRuntimeServices:
    """Installed extension and platform adapters with long-lived state."""

    skills: Optional[SkillServicePort] = None
    plugins: Optional[PluginServicePort] = None
    commands: Optional[CommandServicePort] = None
    agents: Optional[AgentDefinitionServicePort] = None
    mcp: Optional[MCPServicePort] = None
    custom_commands: object | None = None
    output_styles: object | None = None
    lsp: object | None = None
    web: object | None = None
    browser: object | None = None
    auto_memory: object | None = None
    telemetry: object | None = None
    auditor: object | None = None
    extension_registry: object | None = None
    prompt_release_pipeline: object | None = None
    prompt_release_approval_service: object | None = None
    runtime_service_binder: object | None = None
    runtime_services: object | None = None


@dataclass(slots=True)
class SessionRuntimeServices:
    """Session persistence and authorization services."""

    memory: Optional[MemoryServicePort] = None
    recorder: Optional[SessionRecorderPort] = None
    principal: Optional["PrincipalPort"] = None
    enterprise_policy: Optional["EnterprisePolicyPort"] = None


@dataclass(slots=True)
class AgentServices:
    """Typed, concern-grouped services shared by one agent lifecycle."""

    core: CoreRuntimeServices
    context: ContextRuntimeServices
    prompt: PromptRuntimeServices
    extensions: ExtensionRuntimeServices
    session: SessionRuntimeServices

    def has_field(self, name: str) -> bool:
        return name in _SERVICE_FIELDS

    def get_field(self, name: str, default: Any = None) -> Any:
        location = _SERVICE_FIELDS.get(name)
        if location is None:
            return default
        group_name, field_name = location
        group = getattr(self, group_name)
        return getattr(group, field_name, default)

    def set_field(self, name: str, value: Any) -> None:
        location = _SERVICE_FIELDS.get(name)
        if location is None:
            raise AttributeError(name)
        group_name, field_name = location
        group = getattr(self, group_name)
        setattr(group, field_name, value)

    def update(self, **values: Any) -> None:
        for name, value in values.items():
            self.set_field(name, value)


# Legacy attribute name -> (service group, typed field).  This is the only
# allowed translation table for the compatibility view; new production code
# must use ``loop.services.<group>.<field>`` directly.
_SERVICE_FIELDS: dict[str, tuple[str, str]] = {
    "llm": ("core", "llm"),
    "registry": ("core", "registry"),
    "tool_context": ("core", "tool_context"),
    "bus": ("core", "bus"),
    "policy": ("core", "policy"),
    "redactor": ("core", "redactor"),
    "hook_port": ("core", "hook_port"),
    "user_input_port": ("core", "user_input_port"),
    "context_manager": ("context", "manager"),
    "context_assembler": ("context", "assembler"),
    "working_context_store": ("context", "working_context"),
    "tool_microcompactor": ("context", "tool_microcompactor"),
    "reminder_catalog": ("context", "reminder_catalog"),
    "compact_checkpoint_store": ("context", "compact_checkpoint"),
    "context_checkpoint_store": ("context", "checkpoint"),
    "context_checkpoint_coordinator": ("context", "checkpoint_coordinator"),
    "rule_loader": ("context", "rule_loader"),
    "directory_rule_source": ("context", "directory_rule_source"),
    "prompt_assembler": ("prompt", "assembler"),
    "prompt_registry": ("prompt", "registry"),
    "prompt_surface_registry": ("prompt", "surface_registry"),
    "skill_manager": ("extensions", "skills"),
    "plugin_manager": ("extensions", "plugins"),
    "command_registry": ("extensions", "commands"),
    "agent_definitions": ("extensions", "agents"),
    "mcp_supervisor": ("extensions", "mcp"),
    "custom_command_manager": ("extensions", "custom_commands"),
    "output_style_manager": ("extensions", "output_styles"),
    "lsp_manager": ("extensions", "lsp"),
    "web_runtime": ("extensions", "web"),
    "browser_runtime": ("extensions", "browser"),
    "auto_memory": ("extensions", "auto_memory"),
    "telemetry": ("extensions", "telemetry"),
    "auditor": ("extensions", "auditor"),
    "extension_registry": ("extensions", "extension_registry"),
    "prompt_release_pipeline": ("extensions", "prompt_release_pipeline"),
    "prompt_release_approval_service": (
        "extensions", "prompt_release_approval_service"),
    "runtime_service_binder": ("extensions", "runtime_service_binder"),
    "runtime_services": ("extensions", "runtime_services"),
    "memory_store": ("session", "memory"),
    "session_recorder": ("session", "recorder"),
    "principal": ("session", "principal"),
    "enterprise_policy": ("session", "enterprise_policy"),
}


SERVICE_FIELD_NAMES = frozenset(_SERVICE_FIELDS)


__all__ = [
    "AgentDefinitionServicePort",
    "AgentServices",
    "CommandServicePort",
    "ContextRuntimeServices",
    "CoreRuntimeServices",
    "ExtensionRuntimeServices",
    "MCPServicePort",
    "MemoryServicePort",
    "PluginServicePort",
    "PromptRuntimeServices",
    "SERVICE_FIELD_NAMES",
    "SessionRecorderPort",
    "SessionRuntimeServices",
    "SkillServicePort",
]
