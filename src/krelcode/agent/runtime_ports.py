"""Typed boundaries shared by the agent turn executors.

The ports in this module deliberately describe capabilities rather than the
concrete ``AgentLoop`` implementation.  This keeps the runtime dependency
container and executor hosts structurally typed while preserving the existing
composition root and public import paths.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
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
    from ..core.context_usage import ContextUsageSnapshot
    from ..core.events import AgentEvent
    from ..core.hooks import HookResult
    from ..core.messages import Message, ToolCall
    from ..core.redact import SecretRedactor
    from ..contracts.tool import ToolContext, ToolResult
    from ..safety.contract import ModePermissionContract
    from ..safety.policy import ApprovalPolicy
    from .attachments import ContextAttachmentDiff, ContextAttachmentStore
    from .compression import CompactResult
    from .context import WorkspaceContext
    from .context_cache_ledger import ContextCacheLedger
    from .context_checkpoint import (
        ContextRestoreResult,
        FileHash,
        FileIntegrity,
        WorkspaceFingerprint,
    )
    from .context_plan import ContextPlanSnapshot
    from .context_source import (
        ContextAssembly,
        ContextRequest,
        ContextSource,
    )
    from .fragments import PromptFragmentRegistry
    from .intent import TurnIntent
    from .prompt_models import PromptPlan, ProviderMessages as PromptProviderMessages
    from .prompt_assembler import PromptAssemblyRequest
    from .prompt_plan import PromptPlanSnapshot
    from .fragments import PromptSnapshot
    from .reminders import ReminderCatalog, ReminderContext, ReminderSelection
    from .tool_microcompact import MicrocompactReport
    from .tool_pool import TurnToolPool
    from .working_context import (
        WorkingContextSnapshot,
        WorkingEntry,
    )
    from .workflows import TaskWorkflowSelection
    from .compact_summary import CompactSummary


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

    def snapshot(self) -> "WorkingContextSnapshot": ...

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

    digest: str


class CompactCheckpointResultPort(Protocol):
    """Checkpoint result needed by the context turn, without adapter imports."""

    id: str
    summary: CompactSummaryResultPort


class CompactCheckpointStorePort(Protocol):
    """Persistence boundary for pre-compact history checkpoints."""

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
        file_hashes: Optional[Sequence["FileHash"]] = None,
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
    last_context_restore: Optional["ContextRestoreResult"]
    context_integrity: Optional["FileIntegrity"]
    context_workspace_fingerprint: Optional["WorkspaceFingerprint"]
    restored_workspace_fingerprint: Optional["WorkspaceFingerprint"]
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
    "ContextTurnHostPort",
    "ModelTurnHostPort",
    "PromptAssemblerPort",
    "ReminderCatalogPort",
    "ToolMicrocompactorPort",
    "ToolTurnHostPort",
    "WorkingContextPort",
]
