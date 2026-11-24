"""Narrow host and persistence ports used by context checkpoint recovery."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, Sequence

from krelcode.core.events import EventBus
from krelcode.core.messages import Message
from krelcode.contracts.tool import ToolContext

class ContextPlanPort(Protocol):
    @property
    def full_hash(self) -> str: ...
    def as_dict(self) -> Mapping[str, object]: ...


class WorkingEntryPort(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def content(self) -> str: ...
    @property
    def metadata(self) -> Mapping[str, object]: ...


class WorkingContextPort(Protocol):
    @property
    def text(self) -> str: ...
    @property
    def count(self) -> int: ...
    @property
    def entries(self) -> Sequence[WorkingEntryPort]: ...
    @property
    def truncated(self) -> bool: ...
    @property
    def dropped_entries(self) -> int: ...
    @property
    def counts_by_kind(self) -> Mapping[str, int]: ...
    @property
    def sequence(self) -> int: ...
    @property
    def token_count(self) -> int: ...

    def as_checkpoint_record(self) -> Mapping[str, object]: ...


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

    @property
    def history(self) -> list[Message]: ...

    @property
    def skill_base_prompt(self) -> str: ...

    @property
    def tool_context(self) -> ToolContext: ...

    @property
    def context_manager(self) -> Optional[ContextManagerRestorePort]: ...

    @property
    def working_context_store(self) -> WorkingStorePort: ...

    @property
    def last_compact_checkpoint(self) -> Optional[CompactCheckpointPort]: ...

    @property
    def last_context_restore(self) -> Optional[object]: ...

    @property
    def last_context_plan(self) -> Optional[ContextPlanPort]: ...

    @property
    def last_context_cache_ledger(self) -> object: ...

    @property
    def previous_context_cache_ledger(self) -> object: ...

    @property
    def pending_context_refresh(self) -> bool: ...

    @property
    def restored_memory_context(self) -> tuple[str, ...]: ...

    @property
    def restored_relevant_memory_ids(self) -> tuple[str, ...]: ...

    @property
    def restored_context_checkpoint_id(self) -> str: ...

    @property
    def context_integrity(self) -> object: ...

    @property
    def context_workspace_fingerprint(self) -> object: ...

    @property
    def restored_workspace_fingerprint(self) -> object: ...

    @property
    def bus(self) -> EventBus: ...

    @property
    def prompt_profile_config(self) -> object: ...

    def rebuild_working_context(self) -> WorkingContextPort: ...
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
    def update_runtime_state(self, **values: object) -> None: ...


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
