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
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Collection,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)

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
    from krelcode.contracts.tool import RuntimeSnapshot, ToolContext
    from krelcode.llm.base import LLMProvider
    from krelcode.safety.policy import ApprovalPolicy
    from krelcode.tools.registry import ToolRegistry


class MemoryEntryServicePort(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def text(self) -> str: ...


class MemoryServicePort(Protocol):
    """Methods consumed from the session-scoped memory service.

    ``memory`` is intentionally described structurally because ``agent`` must
    not import the concrete ``session`` implementation.
    """

    redactor: Optional["SecretRedactor"]

    def bind_session(self, session_id: str) -> None: ...

    def copy_session(self, source: str, target: str) -> bool: ...

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
    ) -> Sequence[MemoryEntryServicePort]: ...


class SessionRecorderPort(Protocol):
    @property
    def path(self) -> Path: ...

    @property
    def metadata(self) -> dict[str, object]: ...

    def detach(self) -> None: ...

    def flush_history(self, *, turn_id: str = "") -> None: ...

    def update_metadata(self, **metadata: Any) -> None: ...

    def record_rewind(
        self,
        *,
        target_index: int,
        target_turn_id: str,
        removed_turns: int,
        restored: int = 0,
        deleted: int = 0,
    ) -> None: ...


class SkillServicePort(Protocol):
    def prompt_index(self) -> str: ...

    def apply_to_prompt(self, prompt: str) -> str: ...


class PluginServicePort(Protocol):
    @property
    def mcp_servers(self) -> Mapping[str, object]: ...

    def status_lines(self) -> list[str]: ...

    def reload(self) -> object | Awaitable[object]: ...


class AgentDefinitionServicePort(Protocol):
    """Marker protocol for the subagent definition registry."""


class MCPServicePort(Protocol):
    def status(self) -> list[object]: ...

    async def close_all(self) -> None: ...


class CommandServicePort(Protocol):
    def names(self) -> tuple[str, ...]: ...


class LspServicePort(Protocol):
    def status_lines(self) -> Sequence[str]: ...

    async def close(self) -> None: ...


class WebOptionsServicePort(Protocol):
    enabled: bool
    timeout: float
    max_bytes: int
    allow_private: bool
    allowed_ports: Collection[int]
    search_enabled: bool
    search_provider: str
    search_api_key_env: str


class WebRuntimeServicePort(Protocol):
    options: WebOptionsServicePort


class BrowserOptionsServicePort(Protocol):
    enabled: bool
    max_sessions: int


class BrowserRuntimeServicePort(Protocol):
    options: BrowserOptionsServicePort

    @property
    def backend_name(self) -> str: ...

    @property
    def capabilities(self) -> Collection[str]: ...


class OutputStyleEntryServicePort(Protocol):
    name: str
    content: str
    description: str
    scope: str
    path: Optional[Path]
    source: str
    keep_coding_instructions: bool


class OutputStyleServicePort(Protocol):
    @property
    def styles(self) -> Sequence[OutputStyleEntryServicePort]: ...

    def get(self, name: str) -> Optional[OutputStyleEntryServicePort]: ...

    def reload(self) -> object: ...

    def select(self, name: str) -> Optional[OutputStyleEntryServicePort]: ...

    def apply_to_registry(self, registry: object) -> None: ...

    def status_lines(self) -> Sequence[str]: ...


class ExtensionRegistryServicePort(Protocol):
    def status_lines(
        self,
        *,
        kind: Optional[str] = None,
        source: str = "",
    ) -> Sequence[str]: ...


class TelemetryPreferencesServicePort(Protocol):
    mode: str
    endpoint: str


class TelemetryConfigServicePort(Protocol):
    mode: str


class TelemetryServicePort(Protocol):
    config: TelemetryConfigServicePort

    def status(self) -> Mapping[str, object]: ...

    def enable(
        self,
        mode: str = "local",
        *,
        endpoint: str = "",
        sample_rate: Optional[float] = None,
    ) -> TelemetryPreferencesServicePort: ...

    def disable(self) -> TelemetryPreferencesServicePort: ...

    def summary(self) -> Mapping[str, object]: ...

    def export(self, destination: Path) -> Path: ...

    def clear(self) -> bool: ...

    async def flush(self, *, max_batches: int = 20) -> int: ...


ExtensionRefresh = Callable[[], object | Awaitable[object]]


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
    runtime_snapshot_provider: Optional[
        Callable[["RuntimeSnapshot"], "RuntimeSnapshot"]
    ] = None


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
    output_styles: Optional[OutputStyleServicePort] = None
    lsp: Optional[LspServicePort] = None
    web: Optional[WebRuntimeServicePort] = None
    browser: Optional[BrowserRuntimeServicePort] = None
    auto_memory: object | None = None
    telemetry: Optional[TelemetryServicePort] = None
    auditor: object | None = None
    extension_registry: Optional[ExtensionRegistryServicePort] = None
    base_skill_paths: tuple[str, ...] = ()
    extension_refresh: Optional[ExtensionRefresh] = None
    plugin_reload: Optional[ExtensionRefresh] = None
    custom_commands_reload: Optional[ExtensionRefresh] = None
    notebook_options: object | None = None
    migration_options: object | None = None
    status_line_renderer: object | None = None
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
    "runtime_snapshot_provider": ("core", "runtime_snapshot_provider"),
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
    "base_skill_paths": ("extensions", "base_skill_paths"),
    "extension_refresh": ("extensions", "extension_refresh"),
    "plugin_reload": ("extensions", "plugin_reload"),
    "custom_commands_reload": ("extensions", "custom_commands_reload"),
    "notebook_options": ("extensions", "notebook_options"),
    "migration_options": ("extensions", "migration_options"),
    "status_line_renderer": ("extensions", "status_line_renderer"),
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
    "BrowserOptionsServicePort",
    "BrowserRuntimeServicePort",
    "CommandServicePort",
    "ContextRuntimeServices",
    "CoreRuntimeServices",
    "ExtensionRefresh",
    "ExtensionRegistryServicePort",
    "ExtensionRuntimeServices",
    "LspServicePort",
    "MCPServicePort",
    "MemoryEntryServicePort",
    "MemoryServicePort",
    "PluginServicePort",
    "OutputStyleEntryServicePort",
    "OutputStyleServicePort",
    "PromptRuntimeServices",
    "SERVICE_FIELD_NAMES",
    "SessionRecorderPort",
    "SessionRuntimeServices",
    "SkillServicePort",
    "TelemetryConfigServicePort",
    "TelemetryPreferencesServicePort",
    "TelemetryServicePort",
    "WebOptionsServicePort",
    "WebRuntimeServicePort",
]
