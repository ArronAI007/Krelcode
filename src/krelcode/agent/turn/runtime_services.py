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
    from krelcode.contracts.governance import ControlPlanePort
    from krelcode.contracts.tool_execution import ExecutionRuntimePort
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


class RuntimeServiceBinderPort(Protocol):
    @property
    def redactor(self) -> "SecretRedactor": ...

    def add_secret(self, secret: Optional[str]) -> None: ...

    def add_secrets(self, secrets: Sequence[str]) -> None: ...


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

    def set_identity(self, provider: str, model: str) -> None: ...

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
    control_plane: "ControlPlanePort"
    execution_runtime: "ExecutionRuntimePort"
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
    "RuntimeServiceBinderPort",
    "OutputStyleEntryServicePort",
    "OutputStyleServicePort",
    "PromptRuntimeServices",
    "SessionRecorderPort",
    "SessionRuntimeServices",
    "SkillServicePort",
    "TelemetryConfigServicePort",
    "TelemetryPreferencesServicePort",
    "TelemetryServicePort",
    "WebOptionsServicePort",
    "WebRuntimeServicePort",
]
