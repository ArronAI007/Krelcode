"""Typed boundaries shared by the agent turn executors.

The ports in this module deliberately describe capabilities rather than the
concrete ``AgentLoop`` implementation.  This keeps the runtime dependency
container and executor hosts structurally typed while preserving the existing
composition root and public import paths.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

if TYPE_CHECKING:
    from krelcode.core.context_usage import ContextUsageSnapshot
    from krelcode.core.events import AgentEvent
    from krelcode.core.hooks import HookResult
    from krelcode.core.messages import Message, ToolCall
    from krelcode.core.redact import SecretRedactor
    from krelcode.contracts.application import ApplicationRequest
    from krelcode.contracts.tool import ToolContext, ToolResult
    from krelcode.safety.contract import ModePermissionContract
    from krelcode.safety.policy import ApprovalPolicy
    from krelcode.agent.context.attachments import ContextAttachmentDiff, ContextAttachmentStore
    from krelcode.agent.context.compression import CompactResult
    from krelcode.agent.context.workspace import WorkspaceContext
    from krelcode.agent.context.context_cache_ledger import ContextCacheLedger
    from krelcode.agent.context.context_plan import ContextPlanSnapshot
    from krelcode.agent.context.context_source import (
        ContextAssembly,
        ContextRequest,
        ContextSource,
    )
    from krelcode.agent.prompt.fragments import PromptFragmentRegistry
    from krelcode.agent.prompts import PromptProfileResolution
    from krelcode.agent.turn.intent import TurnIntent
    from krelcode.agent.prompt.prompt_models import PromptPlan, ProviderMessages as PromptProviderMessages
    from krelcode.agent.prompt.prompt_assembler import PromptAssemblyRequest
    from krelcode.agent.prompt.prompt_plan import PromptPlanSnapshot
    from krelcode.agent.prompt.fragments import PromptSnapshot
    from krelcode.agent.turn.reminders import ReminderCatalog, ReminderContext, ReminderSelection
    from krelcode.agent.context.tool_microcompact import MicrocompactReport
    from krelcode.agent.turn.tool_pool import TurnToolPool
    from krelcode.agent.context.working_context import (
        WorkingContextSnapshot,
        WorkingEntry,
    )
    from krelcode.agent.turn.workflows import TaskWorkflowSelection
    from krelcode.agent.context.compact_summary import CompactSummary


class ContextManagerPort(Protocol):
    """Behavior required from the context budget/compaction manager."""

    max_tokens: int
    high_watermark_tokens: int
    low_watermark_tokens: int
    last_microcompact_report: Optional["MicrocompactReport"]
    last_compact_result: Optional["CompactResult"]

    def over_budget(
        self,
        history: list["Message"],
        *,
        extra_tokens: int = 0,
        output_reserve_tokens: int = 0,
        model_context_window: Optional[int] = None,
    ) -> bool: ...

    async def maybe_compress(
        self,
        history: list["Message"],
        force: bool = False,
        *,
        extra_tokens: int = 0,
        output_reserve_tokens: int = 0,
        model_context_window: Optional[int] = None,
        microcompact: bool = True,
        current_turn_id: str = "",
        focus: str = "",
    ) -> bool: ...


class ContextAssemblerPort(Protocol):
    """Provider-neutral context source registry and assembler."""

    engine: str

    @property
    def source_names(self) -> Tuple[str, ...]: ...

    def register(
        self,
        source: "ContextSource",
        *,
        replace: bool = False,
    ) -> None: ...

    async def assemble(self, request: "ContextRequest") -> "ContextAssembly": ...


class PromptAssemblerPort(Protocol):
    """Single assembly entry point used by the model executor."""

    engine: str

    async def assemble(self, request: "PromptAssemblyRequest") -> "PromptPlan": ...

    def provider_messages(
        self,
        plan: "PromptPlan",
        *,
        tools: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> "PromptProviderMessages": ...


class WorkingContextPort(Protocol):
    """Durable facts the turn runtime may project or recover."""

    redactor: Optional[Callable[[str], str]]

    def snapshot(self) -> "WorkingContextSnapshot": ...

    def restore_snapshot(
        self,
        raw: Mapping[str, object],
    ) -> "WorkingContextSnapshot": ...

    def clear(self) -> None: ...

    def invalidate_files(
        self,
        paths: Sequence[str],
        *,
        reason: str = "file-changed",
    ) -> int: ...

    def begin_turn(
        self,
        turn_id: str,
        user_input: str,
    ) -> Optional["WorkingEntry"]: ...

    def record_error(
        self,
        content: str,
        *,
        source: str = "agent",
        metadata: Optional[Mapping[str, object]] = None,
    ) -> Optional["WorkingEntry"]: ...

    def rebuild_from_history(
        self,
        history: Sequence["Message"],
        *,
        max_messages: int = 400,
    ) -> "WorkingContextSnapshot": ...

    def record_plan(
        self,
        items: Sequence[str],
        statuses: Sequence[str],
        *,
        active_index: int = -1,
        explanation: str = "",
        turn_id: str = "",
    ) -> Optional["WorkingEntry"]: ...

    def record_tool_result(
        self,
        tool_call: "ToolCall",
        result: "ToolResult",
        *,
        turn_id: str = "",
    ) -> None: ...


class ToolMicrocompactorPort(Protocol):
    """Deterministic first-tier history compaction."""

    def compact(
        self,
        history: list["Message"],
        *,
        trigger: str = "manual",
        current_turn_id: str = "",
    ) -> "MicrocompactReport": ...


class CompactSummaryResultPort(Protocol):
    """Content-free summary identity exposed by a compact checkpoint."""

    @property
    def goal(self) -> str: ...

    @property
    def files(self) -> Sequence["CompactFileResultPort"]: ...

    @property
    def tests(self) -> Sequence["CompactTestResultPort"]: ...

    @property
    def next_steps(self) -> Sequence[str]: ...

    @property
    def source(self) -> str: ...

    @property
    def digest(self) -> str: ...

    def as_dict(self) -> Mapping[str, object]: ...


class CompactFileResultPort(Protocol):
    @property
    def path(self) -> str: ...


class CompactTestResultPort(Protocol):
    @property
    def status(self) -> str: ...


class ContextRestoreResultPort(Protocol):
    """Marker for checkpoint restore results projected onto turn state."""


class FileIntegrityPort(Protocol):
    """Marker for current file-integrity verification state."""


class WorkspaceFingerprintPort(Protocol):
    """Marker for workspace identity captured by a checkpoint."""


class CompactCheckpointResultPort(Protocol):
    """Checkpoint result needed by the context turn, without adapter imports."""

    @property
    def id(self) -> str: ...

    @property
    def summary(self) -> CompactSummaryResultPort: ...

    @property
    def before_tokens(self) -> int: ...

    @property
    def after_tokens(self) -> int: ...


class CompactCheckpointStorePort(Protocol):
    """Persistence boundary for pre-compact history checkpoints."""

    def load(self, checkpoint_id: str) -> object: ...

    def record(
        self,
        *,
        pre_compact_history: Sequence["Message"],
        summary: "CompactSummary",
        trigger: str,
        before_tokens: int,
        after_tokens: int,
        session_id: str = "",
        turn_id: str = "",
        boundary: Optional[Mapping[str, object]] = None,
        source_savings: Optional[Mapping[str, int]] = None,
        working_context: Optional[Mapping[str, object]] = None,
        file_hashes: Optional[Sequence[Any]] = None,
        created_at: Optional[float] = None,
    ) -> Optional["CompactCheckpointResultPort"]: ...


class ReminderCatalogPort(Protocol):
    """Turn-scoped reminder selection."""

    def select(
        self,
        context: "ReminderContext",
        *,
        budget_tokens: int = 0,
    ) -> "ReminderSelection": ...


class _EventHostPort(Protocol):
    async def _emit(self, event: "AgentEvent") -> None: ...


class _ToolPoolHostPort(Protocol):
    def _current_tool_pool(self) -> "TurnToolPool": ...


class ContextTurnHostPort(_EventHostPort, _ToolPoolHostPort, Protocol):
    """State and lifecycle operations owned by the context turn coordinator."""

    history: list["Message"]
    runtime_context: list[str]
    _current_turn_id: str
    _token_total: int
    _turn_intent: "TurnIntent"
    _turn_workflow: Optional["TaskWorkflowSelection"]
    _turn_reminder_selection: Optional["ReminderSelection"]
    _turn_requested_reminder_ids: tuple[str, ...]
    _turn_tool_context: "ToolContext"
    _active_tool_pool: Optional["TurnToolPool"]
    application_request: Optional["ApplicationRequest"]
    last_reminder_selection: Optional["ReminderSelection"]
    last_prompt_plan: Optional["PromptPlan"]
    last_provider_messages: Optional["PromptProviderMessages"]
    last_context_assembly: Optional["ContextAssembly"]
    last_context_snapshot: Optional["ContextUsageSnapshot"]
    previous_context_snapshot: Optional["ContextUsageSnapshot"]
    last_context_plan: Optional["ContextPlanSnapshot"]
    last_context_cache_ledger: Optional["ContextCacheLedger"]
    previous_context_cache_ledger: Optional["ContextCacheLedger"]
    cache_ledger_owner: str
    parent_policy_instruction_hash: str
    last_context_restore: Optional[ContextRestoreResultPort]
    context_integrity: Optional[FileIntegrityPort]
    context_workspace_fingerprint: Optional[WorkspaceFingerprintPort]
    restored_workspace_fingerprint: Optional[WorkspaceFingerprintPort]
    restored_context_checkpoint_id: str
    restored_memory_context: tuple[str, ...]
    restored_relevant_memory_ids: tuple[str, ...]
    pending_context_refresh: bool
    prompt_snapshot: Optional["PromptSnapshot"]
    prompt_registry: Optional["PromptFragmentRegistry"]
    skill_base_prompt: str
    context_attachments: "ContextAttachmentStore"
    last_attachment_diff: "ContextAttachmentDiff"
    last_mode_permission_contract: Optional["ModePermissionContract"]
    last_microcompact_report: Optional["MicrocompactReport"]
    last_compact_checkpoint: Optional["CompactCheckpointResultPort"]
    workspace_context: Optional["WorkspaceContext"]

    async def run_lifecycle_hook(
        self,
        trigger: str,
        variables: dict[str, str],
    ) -> "HookResult": ...



class ModelTurnHostPort(_EventHostPort, _ToolPoolHostPort, Protocol):
    """State required to stream one model response."""

    history: list["Message"]
    _token_total: int
    last_provider_messages: Optional["PromptProviderMessages"]


class ToolTurnHostPort(_EventHostPort, _ToolPoolHostPort, Protocol):
    """State and hook boundaries required for tool batch execution."""

    history: list["Message"]
    _current_turn_id: str
    _failure_counts: dict[str, int]
    _input_counter: Iterator[int]
    _turn_tool_context: "ToolContext"
    prompt_profile_resolution: Optional["PromptProfileResolution"]

    async def _before_tool_hook(
        self, tool_call: "ToolCall") -> "HookResult": ...

    async def _after_tool_hook(
        self,
        tool_call: "ToolCall",
        result: "ToolResult",
    ) -> "HookResult": ...


__all__ = [
    "CompactCheckpointResultPort",
    "CompactCheckpointStorePort",
    "CompactSummaryResultPort",
    "ContextAssemblerPort",
    "ContextManagerPort",
    "ContextRestoreResultPort",
    "ContextTurnHostPort",
    "FileIntegrityPort",
    "ModelTurnHostPort",
    "PromptAssemblerPort",
    "ReminderCatalogPort",
    "ToolMicrocompactorPort",
    "ToolTurnHostPort",
    "WorkingContextPort",
    "WorkspaceFingerprintPort",
]
