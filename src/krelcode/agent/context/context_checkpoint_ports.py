"""Narrow host and persistence ports used by context checkpoint recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol, Sequence

from krelcode.agent.context.context_ports import (
    WorkingContextPort,
    WorkingEntryPort,
)
from krelcode.core.events import EventBus
from krelcode.core.messages import Message
from krelcode.contracts.tool import ToolContext

if TYPE_CHECKING:
    from krelcode.agent.context.compression import ContextManager
    from krelcode.agent.context.context_cache_ledger import ContextCacheLedger
    from krelcode.agent.context.working_context import WorkingContextSnapshot
    from krelcode.agent.context.context_checkpoint_models import (
        ContextRestoreResult,
        FileIntegrity,
        WorkspaceFingerprint,
    )
    from krelcode.agent.context.context_plan import ContextPlanSnapshot
    from krelcode.agent.turn.runtime_ports import CompactCheckpointResultPort

class ContextPlanPort(Protocol):
    @property
    def full_hash(self) -> str: ...
    def as_dict(self) -> Mapping[str, object]: ...


class MemoryEntryPort(Protocol):
    @property
    def id(self) -> str: ...


class RedactorPort(Protocol):
    def redact(self, text: str) -> str: ...


class RuleSourcePort(Protocol):
    @property
    def display_path(self) -> str: ...

    @property
    def imported(self) -> bool: ...


class RuleBundlePort(Protocol):
    @property
    def content(self) -> str: ...

    @property
    def sources(self) -> Sequence[RuleSourcePort]: ...


class RuleLoaderPort(Protocol):
    def load_base(self) -> RuleBundlePort: ...


class CompactSummaryPort(Protocol):
    def as_dict(self) -> Mapping[str, object]: ...


class CompactCheckpointPort(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def summary(self) -> CompactSummaryPort: ...


class CompactStorePort(Protocol):
    def load(self, checkpoint_id: str) -> Any: ...


class MemoryStorePort(Protocol):
    def bind_session(self, session_id: str) -> None: ...

    def copy_session(self, source: str, target: str) -> bool: ...

    def search(self, query: str, *, limit: int = 8) -> Sequence[MemoryEntryPort]: ...


class ContextCheckpointStorePort(Protocol):
    def load(self, checkpoint_id: str) -> Any: ...

    def latest_for_session(self, session_id: str) -> Any: ...


class PromptRegistryPort(Protocol):
    def snapshot(self) -> Any: ...

    def remove(self, fragment_id: str) -> object: ...

    def upsert(self, fragment: Any) -> object: ...


class SkillManagerPort(Protocol):
    def prompt_index(self) -> str: ...

    def apply_to_prompt(self, prompt: str) -> str: ...


class CheckpointContextServicesPort(Protocol):
    @property
    def manager(self) -> Optional[ContextManagerRestorePort]: ...

    @property
    def working_context(self) -> WorkingStorePort: ...

    @property
    def checkpoint(self) -> Optional[ContextCheckpointStorePort]: ...

    @property
    def compact_checkpoint(self) -> Optional[CompactStorePort]: ...

    @property
    def rule_loader(self) -> Optional[RuleLoaderPort]: ...


class CheckpointPromptServicesPort(Protocol):
    @property
    def registry(self) -> Optional[PromptRegistryPort]: ...


class CheckpointSessionServicesPort(Protocol):
    @property
    def memory(self) -> Optional[MemoryStorePort]: ...


class CheckpointExtensionServicesPort(Protocol):
    @property
    def skills(self) -> Optional[SkillManagerPort]: ...


class CheckpointCoreServicesPort(Protocol):
    @property
    def tool_context(self) -> ToolContext: ...

    @property
    def llm(self) -> object: ...

    @property
    def redactor(self) -> Optional[RedactorPort]: ...


class CheckpointRuntimeServicesPort(Protocol):
    @property
    def core(self) -> CheckpointCoreServicesPort: ...

    @property
    def context(self) -> CheckpointContextServicesPort: ...

    @property
    def prompt(self) -> CheckpointPromptServicesPort: ...

    @property
    def extensions(self) -> CheckpointExtensionServicesPort: ...

    @property
    def session(self) -> CheckpointSessionServicesPort: ...


class WorkingStorePort(Protocol):
    def restore_snapshot(
        self, raw: Mapping[str, object]
    ) -> WorkingContextPort: ...
    def snapshot(self) -> WorkingContextPort: ...
    def invalidate_files(
        self, paths: Sequence[str], *, reason: str = "",
    ) -> object: ...


class ContextManagerRestorePort(Protocol):
    def rebuild_state(self, history: Sequence[Message]) -> object: ...

    def restore_summary(self, summary: object) -> None: ...


class CheckpointLoopPort(Protocol):
    @property
    def services(self) -> CheckpointRuntimeServicesPort: ...

    history: list[Message]
    skill_base_prompt: str
    tool_context: ToolContext
    context_manager: Optional["ContextManager"]
    @property
    def working_context_store(self) -> WorkingStorePort: ...
    last_compact_checkpoint: Optional[CompactCheckpointResultPort]
    last_context_restore: Optional["ContextRestoreResult"]
    last_context_plan: Optional["ContextPlanSnapshot"]
    last_context_cache_ledger: Optional["ContextCacheLedger"]
    previous_context_cache_ledger: Optional["ContextCacheLedger"]
    pending_context_refresh: bool
    restored_memory_context: tuple[str, ...]
    restored_relevant_memory_ids: tuple[str, ...]
    restored_context_checkpoint_id: str
    context_integrity: Optional["FileIntegrity"]
    context_workspace_fingerprint: Optional["WorkspaceFingerprint"]
    restored_workspace_fingerprint: Optional["WorkspaceFingerprint"]
    bus: EventBus
    prompt_profile_config: str

    def rebuild_working_context(self) -> "WorkingContextSnapshot": ...
    def sync_context_attachments(
        self,
        *,
        include_environment: bool = True,
        include_catalogs: bool = True,
        include_mcp: bool = True,
        include_contract: bool = True,
        include_reminders: bool = True,
    ) -> object: ...
    async def refresh_workspace_context(self) -> object: ...


__all__ = [
    "CheckpointContextServicesPort",
    "CheckpointCoreServicesPort",
    "CheckpointExtensionServicesPort",
    "CheckpointLoopPort",
    "CheckpointPromptServicesPort",
    "CheckpointRuntimeServicesPort",
    "CheckpointSessionServicesPort",
    "CompactCheckpointPort",
    "CompactStorePort",
    "CompactSummaryPort",
    "ContextCheckpointStorePort",
    "ContextManagerRestorePort",
    "ContextPlanPort",
    "MemoryEntryPort",
    "MemoryStorePort",
    "PromptRegistryPort",
    "RedactorPort",
    "RuleBundlePort",
    "RuleLoaderPort",
    "RuleSourcePort",
    "SkillManagerPort",
    "WorkingStorePort",
    "WorkingContextPort",
    "WorkingEntryPort",
]
