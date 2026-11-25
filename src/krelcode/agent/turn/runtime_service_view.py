"""Explicit mutable state owned by one ``AgentLoop`` instance.

The loop historically exposed a large number of ad-hoc attributes.  That made
it easy for a command, a subagent or a composition helper to mutate runtime
state without any visible ownership boundary.  This module gives those values
stable names and groups them by concern while keeping the old attribute access
as a compatibility view during the migration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from itertools import count
from typing import (
    TYPE_CHECKING,
    Callable,
    Iterator,
    Optional,
    Protocol,
    Sequence,
    cast,
)

from ...core.hooks import HookPort
from .runtime_ports import (
    CompactCheckpointResultPort,
    CompactCheckpointStorePort,
    ContextAssemblerPort,
    ContextManagerPort,
    PromptAssemblerPort,
    ReminderCatalogPort,
    ToolMicrocompactorPort,
    WorkingContextPort,
)
from .runtime_services import (
    AgentDefinitionServicePort,
    RuntimeServiceBinderPort,
    AgentServices,
    BrowserRuntimeServicePort,
    CommandServicePort,
    ExtensionRefresh,
    ExtensionRegistryServicePort,
    LspServicePort,
    MCPServicePort,
    MemoryServicePort,
    OutputStyleServicePort,
    PluginServicePort,
    SessionRecorderPort,
    SkillServicePort,
    TelemetryServicePort,
    WebRuntimeServicePort,
)

if TYPE_CHECKING:
    from krelcode.agent.context.attachments import (
        ContextAttachmentDiff,
        ContextAttachmentStore,
    )
    from krelcode.agent.context.context_cache_ledger import ContextCacheLedger
    from krelcode.agent.context.context_checkpoint_models import (
        ContextRestoreResult,
        FileIntegrity,
        WorkspaceFingerprint,
    )
    from krelcode.agent.context.context_plan import ContextPlanSnapshot
    from krelcode.agent.context.compression import ContextManager
    from krelcode.agent.context.context_checkpoint import (
        ContextCheckpointCoordinator,
        ContextCheckpointStore,
    )
    from krelcode.agent.context.context_source import ContextAssembly
    from krelcode.agent.context.tool_microcompact import MicrocompactReport
    from krelcode.agent.context.workspace import ContextOptions, WorkspaceContext
    from krelcode.agent.context.working_context import WorkingContextStore
    from krelcode.agent.prompt.fragments import (
        PromptFragmentRegistry,
        PromptSnapshot,
    )
    from krelcode.agent.prompt.prompt_models import PromptPlan, ProviderMessages
    from krelcode.agent.prompt.prompt_surface import (
        PromptSurfaceDescriptor,
        PromptSurfaceRegistry,
    )
    from krelcode.agent.prompts import ModelPromptProfile, PromptProfileResolution
    from krelcode.agent.rules import RuleLoader
    from krelcode.agent.turn.dependencies import AgentDependencies
    from krelcode.agent.turn.intent import TurnIntent
    from krelcode.agent.turn.reminders import ReminderSelection
    from krelcode.agent.turn.tool_pool import TurnToolPool
    from krelcode.agent.turn.workflows import TaskWorkflowSelection
    from krelcode.core.context_usage import ContextUsageSnapshot
    from krelcode.core.events import EventBus
    from krelcode.core.interactions import UserInputPort
    from krelcode.core.messages import Message, SteeringInput
    from krelcode.core.redact import SecretRedactor
    from krelcode.contracts.service_ports import (
        EnterprisePolicyPort,
        PrincipalPort,
    )
    from krelcode.contracts.tool import RuntimeSnapshot, ToolContext, TurnAbortSignal
    from krelcode.llm.base import LLMProvider
    from krelcode.safety.contract import ModePermissionContract
    from krelcode.safety.policy import ApprovalPolicy
    from krelcode.tools.registry import ToolRegistry


from .runtime_services import AgentServices


class RuntimeServiceView:
    """Typed compatibility properties for stable runtime services."""

    services: AgentServices
    dependencies: AgentDependencies

    @property
    def llm(self) -> LLMProvider:
        return self.services.core.llm

    @llm.setter
    def llm(self, value: LLMProvider) -> None:
        self.services.core.llm = value
        self.dependencies.llm = value

    @property
    def registry(self) -> ToolRegistry:
        return self.services.core.registry

    @registry.setter
    def registry(self, value: ToolRegistry) -> None:
        self.services.core.registry = value
        self.dependencies.registry = value

    @property
    def tool_context(self) -> ToolContext:
        return self.services.core.tool_context

    @tool_context.setter
    def tool_context(self, value: ToolContext) -> None:
        self.services.core.tool_context = value
        self.dependencies.tool_context = value

    @property
    def bus(self) -> EventBus:
        return self.services.core.bus

    @bus.setter
    def bus(self, value: EventBus) -> None:
        self.services.core.bus = value
        self.dependencies.bus = value

    @property
    def policy(self) -> Optional[ApprovalPolicy]:
        return self.services.core.policy

    @policy.setter
    def policy(self, value: Optional[ApprovalPolicy]) -> None:
        self.services.core.policy = value
        self.dependencies.policy = value

    @property
    def redactor(self) -> Optional[SecretRedactor]:
        return self.services.core.redactor

    @redactor.setter
    def redactor(self, value: Optional[SecretRedactor]) -> None:
        self.services.core.redactor = value
        self.dependencies.redactor = value

    @property
    def hook_port(self) -> Optional[HookPort]:
        return self.services.core.hook_port

    @hook_port.setter
    def hook_port(self, value: Optional[HookPort]) -> None:
        self.services.core.hook_port = value
        self.dependencies.hook_port = value

    @property
    def user_input_port(self) -> Optional[UserInputPort]:
        return self.services.core.user_input_port

    @user_input_port.setter
    def user_input_port(self, value: Optional[UserInputPort]) -> None:
        self.services.core.user_input_port = value
        self.dependencies.user_input_port = value

    @property
    def runtime_snapshot_provider(
        self,
    ) -> Optional[Callable[[RuntimeSnapshot], RuntimeSnapshot]]:
        return self.services.core.runtime_snapshot_provider

    @runtime_snapshot_provider.setter
    def runtime_snapshot_provider(
        self, value: Optional[Callable[[RuntimeSnapshot], RuntimeSnapshot]]
    ) -> None:
        self.services.core.runtime_snapshot_provider = value
        self.dependencies.runtime_snapshot_provider = value

    @property
    def context_manager(self) -> Optional[ContextManager]:
        return self.services.context.manager

    @context_manager.setter
    def context_manager(self, value: Optional[ContextManager]) -> None:
        self.services.context.manager = value
        self.dependencies.context_manager = cast(ContextManagerPort, value)

    @property
    def context_assembler(self) -> ContextAssemblerPort:
        return self.services.context.assembler

    @context_assembler.setter
    def context_assembler(self, value: ContextAssemblerPort) -> None:
        self.services.context.assembler = value
        self.dependencies.context_assembler = value

    @property
    def working_context_store(self) -> WorkingContextPort:
        return self.services.context.working_context

    @working_context_store.setter
    def working_context_store(self, value: WorkingContextPort) -> None:
        self.services.context.working_context = value
        self.dependencies.working_context_store = value

    @property
    def tool_microcompactor(self) -> ToolMicrocompactorPort:
        return self.services.context.tool_microcompactor

    @tool_microcompactor.setter
    def tool_microcompactor(self, value: ToolMicrocompactorPort) -> None:
        self.services.context.tool_microcompactor = value
        self.dependencies.tool_microcompactor = value

    @property
    def reminder_catalog(self) -> ReminderCatalogPort:
        return self.services.context.reminder_catalog

    @reminder_catalog.setter
    def reminder_catalog(self, value: ReminderCatalogPort) -> None:
        self.services.context.reminder_catalog = value
        self.dependencies.reminder_catalog = value

    @property
    def compact_checkpoint_store(self) -> Optional[CompactCheckpointStorePort]:
        return self.services.context.compact_checkpoint

    @compact_checkpoint_store.setter
    def compact_checkpoint_store(
        self, value: Optional[CompactCheckpointStorePort]
    ) -> None:
        self.services.context.compact_checkpoint = value
        self.dependencies.compact_checkpoint_store = value

    @property
    def context_checkpoint_store(self) -> Optional[ContextCheckpointStore]:
        return self.services.context.checkpoint

    @context_checkpoint_store.setter
    def context_checkpoint_store(self, value: Optional[ContextCheckpointStore]) -> None:
        self.services.context.checkpoint = value

    @property
    def context_checkpoint_coordinator(self) -> Optional[ContextCheckpointCoordinator]:
        return self.services.context.checkpoint_coordinator

    @context_checkpoint_coordinator.setter
    def context_checkpoint_coordinator(
        self, value: Optional[ContextCheckpointCoordinator]
    ) -> None:
        self.services.context.checkpoint_coordinator = value

    @property
    def rule_loader(self) -> Optional[RuleLoader]:
        return self.services.context.rule_loader

    @rule_loader.setter
    def rule_loader(self, value: Optional[RuleLoader]) -> None:
        self.services.context.rule_loader = value

    @property
    def directory_rule_source(self) -> object | None:
        return self.services.context.directory_rule_source

    @directory_rule_source.setter
    def directory_rule_source(self, value: object | None) -> None:
        self.services.context.directory_rule_source = value

    @property
    def prompt_assembler(self) -> PromptAssemblerPort:
        return self.services.prompt.assembler

    @prompt_assembler.setter
    def prompt_assembler(self, value: PromptAssemblerPort) -> None:
        self.services.prompt.assembler = value
        self.dependencies.prompt_assembler = value

    @property
    def prompt_registry(self) -> Optional[PromptFragmentRegistry]:
        return self.services.prompt.registry

    @prompt_registry.setter
    def prompt_registry(self, value: Optional[PromptFragmentRegistry]) -> None:
        self.services.prompt.registry = value

    @property
    def prompt_surface_registry(self) -> PromptSurfaceRegistry:
        return self.services.prompt.surface_registry

    @prompt_surface_registry.setter
    def prompt_surface_registry(self, value: PromptSurfaceRegistry) -> None:
        self.services.prompt.surface_registry = value

    @property
    def skill_manager(self) -> Optional[SkillServicePort]:
        return self.services.extensions.skills

    @skill_manager.setter
    def skill_manager(self, value: Optional[SkillServicePort]) -> None:
        self.services.extensions.skills = value

    @property
    def plugin_manager(self) -> Optional[PluginServicePort]:
        return self.services.extensions.plugins

    @plugin_manager.setter
    def plugin_manager(self, value: Optional[PluginServicePort]) -> None:
        self.services.extensions.plugins = value

    @property
    def command_registry(self) -> Optional[CommandServicePort]:
        return self.services.extensions.commands

    @command_registry.setter
    def command_registry(self, value: Optional[CommandServicePort]) -> None:
        self.services.extensions.commands = value

    @property
    def agent_definitions(self) -> Optional[AgentDefinitionServicePort]:
        return self.services.extensions.agents

    @agent_definitions.setter
    def agent_definitions(self, value: Optional[AgentDefinitionServicePort]) -> None:
        self.services.extensions.agents = value

    @property
    def mcp_supervisor(self) -> Optional[MCPServicePort]:
        return self.services.extensions.mcp

    @mcp_supervisor.setter
    def mcp_supervisor(self, value: Optional[MCPServicePort]) -> None:
        self.services.extensions.mcp = value

    @property
    def custom_command_manager(self) -> object | None:
        return self.services.extensions.custom_commands

    @custom_command_manager.setter
    def custom_command_manager(self, value: object | None) -> None:
        self.services.extensions.custom_commands = value

    @property
    def output_style_manager(self) -> Optional[OutputStyleServicePort]:
        return self.services.extensions.output_styles

    @output_style_manager.setter
    def output_style_manager(self, value: Optional[OutputStyleServicePort]) -> None:
        self.services.extensions.output_styles = value

    @property
    def lsp_manager(self) -> Optional[LspServicePort]:
        return self.services.extensions.lsp

    @lsp_manager.setter
    def lsp_manager(self, value: Optional[LspServicePort]) -> None:
        self.services.extensions.lsp = value

    @property
    def web_runtime(self) -> Optional[WebRuntimeServicePort]:
        return self.services.extensions.web

    @web_runtime.setter
    def web_runtime(self, value: Optional[WebRuntimeServicePort]) -> None:
        self.services.extensions.web = value

    @property
    def browser_runtime(self) -> Optional[BrowserRuntimeServicePort]:
        return self.services.extensions.browser

    @browser_runtime.setter
    def browser_runtime(self, value: Optional[BrowserRuntimeServicePort]) -> None:
        self.services.extensions.browser = value

    @property
    def auto_memory(self) -> object | None:
        return self.services.extensions.auto_memory

    @auto_memory.setter
    def auto_memory(self, value: object | None) -> None:
        self.services.extensions.auto_memory = value

    @property
    def telemetry(self) -> Optional[TelemetryServicePort]:
        return self.services.extensions.telemetry

    @telemetry.setter
    def telemetry(self, value: Optional[TelemetryServicePort]) -> None:
        self.services.extensions.telemetry = value

    @property
    def auditor(self) -> object | None:
        return self.services.extensions.auditor

    @auditor.setter
    def auditor(self, value: object | None) -> None:
        self.services.extensions.auditor = value

    @property
    def extension_registry(self) -> Optional[ExtensionRegistryServicePort]:
        return self.services.extensions.extension_registry

    @extension_registry.setter
    def extension_registry(self, value: Optional[ExtensionRegistryServicePort]) -> None:
        self.services.extensions.extension_registry = value

    @property
    def base_skill_paths(self) -> tuple[str, ...]:
        return self.services.extensions.base_skill_paths

    @base_skill_paths.setter
    def base_skill_paths(self, value: tuple[str, ...]) -> None:
        self.services.extensions.base_skill_paths = value

    @property
    def extension_refresh(self) -> Optional[ExtensionRefresh]:
        return self.services.extensions.extension_refresh

    @extension_refresh.setter
    def extension_refresh(self, value: Optional[ExtensionRefresh]) -> None:
        self.services.extensions.extension_refresh = value

    @property
    def plugin_reload(self) -> Optional[ExtensionRefresh]:
        return self.services.extensions.plugin_reload

    @plugin_reload.setter
    def plugin_reload(self, value: Optional[ExtensionRefresh]) -> None:
        self.services.extensions.plugin_reload = value

    @property
    def custom_commands_reload(self) -> Optional[ExtensionRefresh]:
        return self.services.extensions.custom_commands_reload

    @custom_commands_reload.setter
    def custom_commands_reload(self, value: Optional[ExtensionRefresh]) -> None:
        self.services.extensions.custom_commands_reload = value

    @property
    def notebook_options(self) -> object | None:
        return self.services.extensions.notebook_options

    @notebook_options.setter
    def notebook_options(self, value: object | None) -> None:
        self.services.extensions.notebook_options = value

    @property
    def migration_options(self) -> object | None:
        return self.services.extensions.migration_options

    @migration_options.setter
    def migration_options(self, value: object | None) -> None:
        self.services.extensions.migration_options = value

    @property
    def status_line_renderer(self) -> object | None:
        return self.services.extensions.status_line_renderer

    @status_line_renderer.setter
    def status_line_renderer(self, value: object | None) -> None:
        self.services.extensions.status_line_renderer = value

    @property
    def prompt_release_pipeline(self) -> object | None:
        return self.services.extensions.prompt_release_pipeline

    @prompt_release_pipeline.setter
    def prompt_release_pipeline(self, value: object | None) -> None:
        self.services.extensions.prompt_release_pipeline = value

    @property
    def prompt_release_approval_service(self) -> object | None:
        return self.services.extensions.prompt_release_approval_service

    @prompt_release_approval_service.setter
    def prompt_release_approval_service(self, value: object | None) -> None:
        self.services.extensions.prompt_release_approval_service = value

    @property
    def runtime_service_binder(self) -> Optional[RuntimeServiceBinderPort]:
        return cast(
            "Optional[RuntimeServiceBinderPort]",
            self.services.extensions.runtime_service_binder,
        )

    @runtime_service_binder.setter
    def runtime_service_binder(
        self, value: Optional[RuntimeServiceBinderPort]
    ) -> None:
        self.services.extensions.runtime_service_binder = value

    @property
    def runtime_services(self) -> object | None:
        return self.services.extensions.runtime_services

    @runtime_services.setter
    def runtime_services(self, value: object | None) -> None:
        self.services.extensions.runtime_services = value

    @property
    def memory_store(self) -> Optional[MemoryServicePort]:
        return self.services.session.memory

    @memory_store.setter
    def memory_store(self, value: Optional[MemoryServicePort]) -> None:
        self.services.session.memory = value

    @property
    def session_recorder(self) -> Optional[SessionRecorderPort]:
        return self.services.session.recorder

    @session_recorder.setter
    def session_recorder(self, value: Optional[SessionRecorderPort]) -> None:
        self.services.session.recorder = value

    @property
    def principal(self) -> Optional[PrincipalPort]:
        return self.services.session.principal

    @principal.setter
    def principal(self, value: Optional[PrincipalPort]) -> None:
        self.services.session.principal = value

    @property
    def enterprise_policy(self) -> Optional[EnterprisePolicyPort]:
        return self.services.session.enterprise_policy

    @enterprise_policy.setter
    def enterprise_policy(self, value: Optional[EnterprisePolicyPort]) -> None:
        self.services.session.enterprise_policy = value
