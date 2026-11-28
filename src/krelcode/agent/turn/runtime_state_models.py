"""Typed state groups and leaf DTOs owned by one ``AgentLoop`` instance.

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
    from krelcode.contracts.agent import AgentContract
    from krelcode.contracts.loop import Observation
    from .loop_state import AgentLoopStateMachine
    from krelcode.contracts.acceptance import AcceptanceResult
    from krelcode.contracts.tool import RuntimeSnapshot, ToolContext, TurnAbortSignal
    from krelcode.llm.base import LLMProvider
    from krelcode.safety.contract import ModePermissionContract
    from krelcode.safety.policy import ApprovalPolicy
    from krelcode.tools.registry import ToolRegistry


class InstructionLayerPort(Protocol):
    """Structural boundary for subagent instruction layers owned by this state."""

    kind: str
    source: str
    content: str


def _turn_intent() -> "TurnIntent":
    from .intent import TurnIntent

    return TurnIntent()


def _abort_signal() -> "TurnAbortSignal":
    from krelcode.contracts.tool import TurnAbortSignal

    return TurnAbortSignal()


def _attachment_store() -> "ContextAttachmentStore":
    from krelcode.agent.context.attachments import ContextAttachmentStore

    return ContextAttachmentStore()


def _attachment_diff() -> "ContextAttachmentDiff":
    from krelcode.agent.context.attachments import ContextAttachmentDiff

    return ContextAttachmentDiff(0, 0)


@dataclass
class TurnRuntimeState:
    """Per-turn execution state and the current model/tool history."""

    history: list["Message"] = field(default_factory=list)
    runtime_context: list[str] = field(default_factory=list)
    token_total: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    turn_intent: "TurnIntent" = field(default_factory=lambda: _turn_intent())
    turn_workflow: Optional["TaskWorkflowSelection"] = None
    active_tool_pool: Optional["TurnToolPool"] = None
    turn_tool_context: Optional["ToolContext"] = None
    turn_abort_signal: "TurnAbortSignal" = field(
        default_factory=lambda: _abort_signal()
    )
    turn_counter: Iterator[int] = field(default_factory=lambda: count(1))
    direct_counter: Iterator[int] = field(default_factory=lambda: count(1))
    current_turn_id: str = ""
    input_counter: Iterator[int] = field(default_factory=lambda: count(1))
    turn_prepared: bool = False
    turn_reminder_selection: Optional["ReminderSelection"] = None
    turn_requested_reminder_ids: tuple[str, ...] = ()
    last_reminder_selection: Optional["ReminderSelection"] = None
    application_request: Optional["ApplicationRequest"] = None
    agent_contract: Optional["AgentContract"] = None
    acceptance_result: Optional["AcceptanceResult"] = None
    loop_state: Optional["AgentLoopStateMachine"] = None
    last_observation: Optional["Observation"] = None
    observations: list["Observation"] = field(default_factory=list)
    steering_queue: asyncio.Queue[SteeringInput | str] = field(
        default_factory=lambda: asyncio.Queue()
    )


@dataclass
class ContextRuntimeState:
    """Context, prompt-request observability and checkpoint state."""

    pending_context_refresh: bool = False
    workspace_context: Optional["WorkspaceContext"] = None
    context_options: Optional["ContextOptions"] = None
    context_engine: str = "v3"
    context_enabled: bool = True
    context_integrity: Optional["FileIntegrity"] = None
    last_compact_checkpoint: Optional[CompactCheckpointResultPort] = None
    last_context_restore: Optional["ContextRestoreResult"] = None
    last_context_plan: Optional["ContextPlanSnapshot"] = None
    last_context_snapshot: Optional["ContextUsageSnapshot"] = None
    last_context_assembly: Optional["ContextAssembly"] = None
    last_context_cache_ledger: Optional["ContextCacheLedger"] = None
    previous_context_snapshot: Optional["ContextUsageSnapshot"] = None
    previous_context_cache_ledger: Optional["ContextCacheLedger"] = None
    last_mode_permission_contract: Optional["ModePermissionContract"] = None
    context_attachments: "ContextAttachmentStore" = field(
        default_factory=lambda: _attachment_store()
    )
    last_attachment_diff: "ContextAttachmentDiff" = field(
        default_factory=lambda: _attachment_diff()
    )
    last_microcompact_report: Optional["MicrocompactReport"] = None
    context_workspace_fingerprint: Optional["WorkspaceFingerprint"] = None
    restored_workspace_fingerprint: Optional["WorkspaceFingerprint"] = None
    restored_context_checkpoint_id: str = ""
    restored_memory_context: tuple[str, ...] = ()
    restored_relevant_memory_ids: tuple[str, ...] = ()


@dataclass
class PromptRuntimeState:
    """Prompt registry/profile and provider-message observability."""

    prompt_snapshot: Optional["PromptSnapshot"] = None
    prompt_profile: Optional["ModelPromptProfile"] = None
    prompt_profile_resolution: Optional["PromptProfileResolution"] = None
    prompt_profile_name: str = ""
    prompt_profile_config: str = "auto"
    prompt_authoring_enabled: bool = False
    auxiliary_prompt_surfaces: tuple["PromptSurfaceDescriptor", ...] = ()
    last_prompt_plan: Optional["PromptPlan"] = None
    last_provider_messages: Optional["ProviderMessages"] = None
    cache_ledger_owner: str = "main"
    parent_policy_instruction_hash: str = ""
    skill_base_prompt: str = ""
    inherited_memory_ids: tuple[str, ...] = ()
    parent_history_inherited: bool = False
    subagent_definition_name: str = ""
    subagent_instruction_layers: tuple[InstructionLayerPort, ...] = ()


@dataclass
class ExtensionRuntimeState:
    """Reserved compatibility group; extension services are typed services."""


@dataclass
class SessionRuntimeState:
    """Session-scoped mutable state reserved for future use."""


@dataclass
class AgentRuntimeState:
    """Grouped mutable state owned by an ``AgentLoop``."""

    turn: TurnRuntimeState = field(default_factory=TurnRuntimeState)
    context: ContextRuntimeState = field(default_factory=ContextRuntimeState)
    prompt: PromptRuntimeState = field(default_factory=PromptRuntimeState)
    extensions: ExtensionRuntimeState = field(default_factory=ExtensionRuntimeState)
    session: SessionRuntimeState = field(default_factory=SessionRuntimeState)


__all__ = [
    "AgentRuntimeState",
    "ContextRuntimeState",
    "ExtensionRuntimeState",
    "PromptRuntimeState",
    "SessionRuntimeState",
    "TurnRuntimeState",
]
