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
    from krelcode.contracts.application import ApplicationRequest
    from krelcode.contracts.tool import RuntimeSnapshot, ToolContext, TurnAbortSignal
    from krelcode.llm.base import LLMProvider
    from krelcode.safety.contract import ModePermissionContract
    from krelcode.safety.policy import ApprovalPolicy
    from krelcode.tools.registry import ToolRegistry


from .runtime_state_models import (
    AgentRuntimeState,
    InstructionLayerPort,
)


class RuntimeStateView:
    """Typed compatibility properties for mutable per-session state."""

    _runtime_state: AgentRuntimeState

    @property
    def history(self) -> list[Message]:
        return self.runtime_state.turn.history

    @history.setter
    def history(self, value: list[Message]) -> None:
        self.runtime_state.turn.history = value

    @property
    def runtime_context(self) -> list[str]:
        return self.runtime_state.turn.runtime_context

    @runtime_context.setter
    def runtime_context(self, value: list[str]) -> None:
        self.runtime_state.turn.runtime_context = value

    @property
    def _token_total(self) -> int:
        return self.runtime_state.turn.token_total

    @_token_total.setter
    def _token_total(self, value: int) -> None:
        self.runtime_state.turn.token_total = value

    @property
    def _failure_counts(self) -> dict[str, int]:
        return self.runtime_state.turn.failure_counts

    @_failure_counts.setter
    def _failure_counts(self, value: dict[str, int]) -> None:
        self.runtime_state.turn.failure_counts = value

    @property
    def _turn_intent(self) -> TurnIntent:
        return self.runtime_state.turn.turn_intent

    @_turn_intent.setter
    def _turn_intent(self, value: TurnIntent) -> None:
        self.runtime_state.turn.turn_intent = value

    @property
    def _turn_workflow(self) -> Optional[TaskWorkflowSelection]:
        return self.runtime_state.turn.turn_workflow

    @_turn_workflow.setter
    def _turn_workflow(self, value: Optional[TaskWorkflowSelection]) -> None:
        self.runtime_state.turn.turn_workflow = value

    @property
    def _active_tool_pool(self) -> Optional[TurnToolPool]:
        return self.runtime_state.turn.active_tool_pool

    @_active_tool_pool.setter
    def _active_tool_pool(self, value: Optional[TurnToolPool]) -> None:
        self.runtime_state.turn.active_tool_pool = value

    @property
    def _turn_tool_context(self) -> ToolContext:
        return cast("ToolContext", self.runtime_state.turn.turn_tool_context)

    @_turn_tool_context.setter
    def _turn_tool_context(self, value: ToolContext) -> None:
        self.runtime_state.turn.turn_tool_context = value

    @property
    def _turn_abort_signal(self) -> TurnAbortSignal:
        return self.runtime_state.turn.turn_abort_signal

    @_turn_abort_signal.setter
    def _turn_abort_signal(self, value: TurnAbortSignal) -> None:
        self.runtime_state.turn.turn_abort_signal = value

    @property
    def _turn_counter(self) -> Iterator[int]:
        return self.runtime_state.turn.turn_counter

    @_turn_counter.setter
    def _turn_counter(self, value: Iterator[int]) -> None:
        self.runtime_state.turn.turn_counter = value

    @property
    def _direct_counter(self) -> Iterator[int]:
        return self.runtime_state.turn.direct_counter

    @_direct_counter.setter
    def _direct_counter(self, value: Iterator[int]) -> None:
        self.runtime_state.turn.direct_counter = value

    @property
    def _current_turn_id(self) -> str:
        return self.runtime_state.turn.current_turn_id

    @_current_turn_id.setter
    def _current_turn_id(self, value: str) -> None:
        self.runtime_state.turn.current_turn_id = value

    @property
    def _input_counter(self) -> Iterator[int]:
        return self.runtime_state.turn.input_counter

    @_input_counter.setter
    def _input_counter(self, value: Iterator[int]) -> None:
        self.runtime_state.turn.input_counter = value

    @property
    def _turn_prepared(self) -> bool:
        return self.runtime_state.turn.turn_prepared

    @_turn_prepared.setter
    def _turn_prepared(self, value: bool) -> None:
        self.runtime_state.turn.turn_prepared = value

    @property
    def _turn_reminder_selection(self) -> Optional[ReminderSelection]:
        return self.runtime_state.turn.turn_reminder_selection

    @_turn_reminder_selection.setter
    def _turn_reminder_selection(self, value: Optional[ReminderSelection]) -> None:
        self.runtime_state.turn.turn_reminder_selection = value

    @property
    def _turn_requested_reminder_ids(self) -> tuple[str, ...]:
        return self.runtime_state.turn.turn_requested_reminder_ids

    @_turn_requested_reminder_ids.setter
    def _turn_requested_reminder_ids(self, value: tuple[str, ...]) -> None:
        self.runtime_state.turn.turn_requested_reminder_ids = value

    @property
    def last_reminder_selection(self) -> Optional[ReminderSelection]:
        return self.runtime_state.turn.last_reminder_selection

    @last_reminder_selection.setter
    def last_reminder_selection(self, value: Optional[ReminderSelection]) -> None:
        self.runtime_state.turn.last_reminder_selection = value

    @property
    def application_request(self) -> Optional[ApplicationRequest]:
        return self.runtime_state.turn.application_request

    @application_request.setter
    def application_request(
        self,
        value: Optional[ApplicationRequest],
    ) -> None:
        self.runtime_state.turn.application_request = value

    @property
    def steering_queue(self) -> asyncio.Queue[SteeringInput | str]:
        return self.runtime_state.turn.steering_queue

    @steering_queue.setter
    def steering_queue(self, value: asyncio.Queue[SteeringInput | str]) -> None:
        self.runtime_state.turn.steering_queue = value

    @property
    def pending_context_refresh(self) -> bool:
        return self.runtime_state.context.pending_context_refresh

    @pending_context_refresh.setter
    def pending_context_refresh(self, value: bool) -> None:
        self.runtime_state.context.pending_context_refresh = value

    @property
    def workspace_context(self) -> Optional[WorkspaceContext]:
        return self.runtime_state.context.workspace_context

    @workspace_context.setter
    def workspace_context(self, value: Optional[WorkspaceContext]) -> None:
        self.runtime_state.context.workspace_context = value

    @property
    def context_options(self) -> Optional[ContextOptions]:
        return self.runtime_state.context.context_options

    @context_options.setter
    def context_options(self, value: Optional[ContextOptions]) -> None:
        self.runtime_state.context.context_options = value

    @property
    def context_engine(self) -> str:
        return self.runtime_state.context.context_engine

    @context_engine.setter
    def context_engine(self, value: str) -> None:
        self.runtime_state.context.context_engine = value

    @property
    def context_enabled(self) -> bool:
        return self.runtime_state.context.context_enabled

    @context_enabled.setter
    def context_enabled(self, value: bool) -> None:
        self.runtime_state.context.context_enabled = value

    @property
    def context_integrity(self) -> Optional[FileIntegrity]:
        return self.runtime_state.context.context_integrity

    @context_integrity.setter
    def context_integrity(self, value: Optional[FileIntegrity]) -> None:
        self.runtime_state.context.context_integrity = value

    @property
    def last_compact_checkpoint(self) -> Optional[CompactCheckpointResultPort]:
        return self.runtime_state.context.last_compact_checkpoint

    @last_compact_checkpoint.setter
    def last_compact_checkpoint(
        self, value: Optional[CompactCheckpointResultPort]
    ) -> None:
        self.runtime_state.context.last_compact_checkpoint = value

    @property
    def last_context_restore(self) -> Optional[ContextRestoreResult]:
        return self.runtime_state.context.last_context_restore

    @last_context_restore.setter
    def last_context_restore(self, value: Optional[ContextRestoreResult]) -> None:
        self.runtime_state.context.last_context_restore = value

    @property
    def last_context_plan(self) -> Optional[ContextPlanSnapshot]:
        return self.runtime_state.context.last_context_plan

    @last_context_plan.setter
    def last_context_plan(self, value: Optional[ContextPlanSnapshot]) -> None:
        self.runtime_state.context.last_context_plan = value

    @property
    def last_context_snapshot(self) -> Optional[ContextUsageSnapshot]:
        return self.runtime_state.context.last_context_snapshot

    @last_context_snapshot.setter
    def last_context_snapshot(self, value: Optional[ContextUsageSnapshot]) -> None:
        self.runtime_state.context.last_context_snapshot = value

    @property
    def last_context_assembly(self) -> Optional[ContextAssembly]:
        return self.runtime_state.context.last_context_assembly

    @last_context_assembly.setter
    def last_context_assembly(self, value: Optional[ContextAssembly]) -> None:
        self.runtime_state.context.last_context_assembly = value

    @property
    def last_context_cache_ledger(self) -> Optional[ContextCacheLedger]:
        return self.runtime_state.context.last_context_cache_ledger

    @last_context_cache_ledger.setter
    def last_context_cache_ledger(self, value: Optional[ContextCacheLedger]) -> None:
        self.runtime_state.context.last_context_cache_ledger = value

    @property
    def previous_context_snapshot(self) -> Optional[ContextUsageSnapshot]:
        return self.runtime_state.context.previous_context_snapshot

    @previous_context_snapshot.setter
    def previous_context_snapshot(self, value: Optional[ContextUsageSnapshot]) -> None:
        self.runtime_state.context.previous_context_snapshot = value

    @property
    def previous_context_cache_ledger(self) -> Optional[ContextCacheLedger]:
        return self.runtime_state.context.previous_context_cache_ledger

    @previous_context_cache_ledger.setter
    def previous_context_cache_ledger(
        self, value: Optional[ContextCacheLedger]
    ) -> None:
        self.runtime_state.context.previous_context_cache_ledger = value

    @property
    def last_mode_permission_contract(self) -> Optional[ModePermissionContract]:
        return self.runtime_state.context.last_mode_permission_contract

    @last_mode_permission_contract.setter
    def last_mode_permission_contract(
        self, value: Optional[ModePermissionContract]
    ) -> None:
        self.runtime_state.context.last_mode_permission_contract = value

    @property
    def context_attachments(self) -> ContextAttachmentStore:
        return self.runtime_state.context.context_attachments

    @context_attachments.setter
    def context_attachments(self, value: ContextAttachmentStore) -> None:
        self.runtime_state.context.context_attachments = value

    @property
    def last_attachment_diff(self) -> ContextAttachmentDiff:
        return self.runtime_state.context.last_attachment_diff

    @last_attachment_diff.setter
    def last_attachment_diff(self, value: ContextAttachmentDiff) -> None:
        self.runtime_state.context.last_attachment_diff = value

    @property
    def last_microcompact_report(self) -> Optional[MicrocompactReport]:
        return self.runtime_state.context.last_microcompact_report

    @last_microcompact_report.setter
    def last_microcompact_report(self, value: Optional[MicrocompactReport]) -> None:
        self.runtime_state.context.last_microcompact_report = value

    @property
    def context_workspace_fingerprint(self) -> Optional[WorkspaceFingerprint]:
        return self.runtime_state.context.context_workspace_fingerprint

    @context_workspace_fingerprint.setter
    def context_workspace_fingerprint(
        self, value: Optional[WorkspaceFingerprint]
    ) -> None:
        self.runtime_state.context.context_workspace_fingerprint = value

    @property
    def restored_workspace_fingerprint(self) -> Optional[WorkspaceFingerprint]:
        return self.runtime_state.context.restored_workspace_fingerprint

    @restored_workspace_fingerprint.setter
    def restored_workspace_fingerprint(
        self, value: Optional[WorkspaceFingerprint]
    ) -> None:
        self.runtime_state.context.restored_workspace_fingerprint = value

    @property
    def restored_context_checkpoint_id(self) -> str:
        return self.runtime_state.context.restored_context_checkpoint_id

    @restored_context_checkpoint_id.setter
    def restored_context_checkpoint_id(self, value: str) -> None:
        self.runtime_state.context.restored_context_checkpoint_id = value

    @property
    def restored_memory_context(self) -> tuple[str, ...]:
        return self.runtime_state.context.restored_memory_context

    @restored_memory_context.setter
    def restored_memory_context(self, value: tuple[str, ...]) -> None:
        self.runtime_state.context.restored_memory_context = value

    @property
    def restored_relevant_memory_ids(self) -> tuple[str, ...]:
        return self.runtime_state.context.restored_relevant_memory_ids

    @restored_relevant_memory_ids.setter
    def restored_relevant_memory_ids(self, value: tuple[str, ...]) -> None:
        self.runtime_state.context.restored_relevant_memory_ids = value

    @property
    def prompt_snapshot(self) -> Optional[PromptSnapshot]:
        return self.runtime_state.prompt.prompt_snapshot

    @prompt_snapshot.setter
    def prompt_snapshot(self, value: Optional[PromptSnapshot]) -> None:
        self.runtime_state.prompt.prompt_snapshot = value

    @property
    def prompt_profile(self) -> Optional[ModelPromptProfile]:
        return self.runtime_state.prompt.prompt_profile

    @prompt_profile.setter
    def prompt_profile(self, value: Optional[ModelPromptProfile]) -> None:
        self.runtime_state.prompt.prompt_profile = value

    @property
    def prompt_profile_resolution(self) -> Optional[PromptProfileResolution]:
        return self.runtime_state.prompt.prompt_profile_resolution

    @prompt_profile_resolution.setter
    def prompt_profile_resolution(
        self, value: Optional[PromptProfileResolution]
    ) -> None:
        self.runtime_state.prompt.prompt_profile_resolution = value

    @property
    def prompt_profile_name(self) -> str:
        return self.runtime_state.prompt.prompt_profile_name

    @prompt_profile_name.setter
    def prompt_profile_name(self, value: str) -> None:
        self.runtime_state.prompt.prompt_profile_name = value

    @property
    def prompt_profile_config(self) -> str:
        return self.runtime_state.prompt.prompt_profile_config

    @prompt_profile_config.setter
    def prompt_profile_config(self, value: str) -> None:
        self.runtime_state.prompt.prompt_profile_config = value

    @property
    def prompt_authoring_enabled(self) -> bool:
        return self.runtime_state.prompt.prompt_authoring_enabled

    @prompt_authoring_enabled.setter
    def prompt_authoring_enabled(self, value: bool) -> None:
        self.runtime_state.prompt.prompt_authoring_enabled = value

    @property
    def auxiliary_prompt_surfaces(self) -> tuple[PromptSurfaceDescriptor, ...]:
        return self.runtime_state.prompt.auxiliary_prompt_surfaces

    @auxiliary_prompt_surfaces.setter
    def auxiliary_prompt_surfaces(
        self, value: tuple[PromptSurfaceDescriptor, ...]
    ) -> None:
        self.runtime_state.prompt.auxiliary_prompt_surfaces = value

    @property
    def last_prompt_plan(self) -> Optional[PromptPlan]:
        return self.runtime_state.prompt.last_prompt_plan

    @last_prompt_plan.setter
    def last_prompt_plan(self, value: Optional[PromptPlan]) -> None:
        self.runtime_state.prompt.last_prompt_plan = value

    @property
    def last_provider_messages(self) -> Optional[ProviderMessages]:
        return self.runtime_state.prompt.last_provider_messages

    @last_provider_messages.setter
    def last_provider_messages(self, value: Optional[ProviderMessages]) -> None:
        self.runtime_state.prompt.last_provider_messages = value

    @property
    def cache_ledger_owner(self) -> str:
        return self.runtime_state.prompt.cache_ledger_owner

    @cache_ledger_owner.setter
    def cache_ledger_owner(self, value: str) -> None:
        self.runtime_state.prompt.cache_ledger_owner = value

    @property
    def parent_policy_instruction_hash(self) -> str:
        return self.runtime_state.prompt.parent_policy_instruction_hash

    @parent_policy_instruction_hash.setter
    def parent_policy_instruction_hash(self, value: str) -> None:
        self.runtime_state.prompt.parent_policy_instruction_hash = value

    @property
    def skill_base_prompt(self) -> str:
        return self.runtime_state.prompt.skill_base_prompt

    @skill_base_prompt.setter
    def skill_base_prompt(self, value: str) -> None:
        self.runtime_state.prompt.skill_base_prompt = value

    @property
    def inherited_memory_ids(self) -> tuple[str, ...]:
        return self.runtime_state.prompt.inherited_memory_ids

    @inherited_memory_ids.setter
    def inherited_memory_ids(self, value: tuple[str, ...]) -> None:
        self.runtime_state.prompt.inherited_memory_ids = value

    @property
    def parent_history_inherited(self) -> bool:
        return self.runtime_state.prompt.parent_history_inherited

    @parent_history_inherited.setter
    def parent_history_inherited(self, value: bool) -> None:
        self.runtime_state.prompt.parent_history_inherited = value

    @property
    def subagent_definition_name(self) -> str:
        return self.runtime_state.prompt.subagent_definition_name

    @subagent_definition_name.setter
    def subagent_definition_name(self, value: str) -> None:
        self.runtime_state.prompt.subagent_definition_name = value

    @property
    def subagent_instruction_layers(self) -> tuple[InstructionLayerPort, ...]:
        return self.runtime_state.prompt.subagent_instruction_layers

    @subagent_instruction_layers.setter
    def subagent_instruction_layers(
        self, value: Sequence[InstructionLayerPort]
    ) -> None:
        self.runtime_state.prompt.subagent_instruction_layers = tuple(value)

    @property
    def runtime_state(self) -> AgentRuntimeState:
        return self._runtime_state
