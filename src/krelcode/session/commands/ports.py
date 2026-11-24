"""Structural command-layer service ports.

Command handlers must not require concrete AgentLoop, MCP, plugin, memory or
session implementations.  These protocols describe the shell each handler
needs while leaving lifecycle and wire-level behavior in the owning modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from ...contracts.service_ports import ShellJobPagePort, ShellJobPort
from ...contracts.runtime import PermissionMode
from ...contracts.tool import Tool, ToolDescriptor
from ...core.messages import Message

if TYPE_CHECKING:
    from ...agent.turn.runtime_services import AgentServices


@runtime_checkable
class ToolDescriptorPort(Protocol):
    @property
    def read_only(self) -> bool: ...

    @property
    def source(self) -> object: ...

    @property
    def tier(self) -> Optional[object]: ...


@runtime_checkable
class ToolEntryPort(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def tool(self) -> Tool: ...

    @property
    def descriptor(self) -> ToolDescriptor: ...


@runtime_checkable
class ToolPoolPort(Protocol):
    def catalog_entries(self) -> Sequence[ToolEntryPort]: ...
    def is_loaded(self, name: str) -> bool: ...
    def excluded_items(self) -> Sequence[Tuple[str, str]]: ...
    def names(self) -> Sequence[str]: ...


@runtime_checkable
class ContextManagerPort(Protocol):
    @property
    def max_tokens(self) -> int: ...

    @property
    def high_watermark_tokens(self) -> int: ...

    @property
    def low_watermark_tokens(self) -> int: ...

    def preview_compact(
        self,
        history: list[Message],
        force: bool = False,
        *,
        current_turn_id: str = "",
    ) -> "CompactPreviewPort": ...


@runtime_checkable
class CompactPreviewPort(Protocol):
    def render(self, *, include_messages: bool = True) -> str: ...


@runtime_checkable
class CommandLoopPort(Protocol):
    """Runtime state and operations command handlers may touch directly."""

    @property
    def services(self) -> "AgentServices": ...

    @property
    def history(self) -> list[Message]: ...

    @property
    def prompt_profile_resolution(self) -> Optional[Any]: ...

    @property
    def prompt_profile_name(self) -> str: ...

    @property
    def auxiliary_prompt_surfaces(self) -> Sequence[Any]: ...

    @property
    def last_prompt_plan(self) -> Optional[Any]: ...

    @property
    def runtime_state(self) -> Any: ...

    @property
    def policy(self) -> object: ...

    @property
    def prompt_registry(self) -> Optional[Any]: ...

    @property
    def prompt_snapshot(self) -> Optional[Any]: ...

    @property
    def workspace_context(self) -> object: ...

    @property
    def context_manager(self) -> Optional[ContextManagerPort]: ...

    @property
    def skill_manager(self) -> Optional["SkillManagerPort"]: ...

    @property
    def skill_base_prompt(self) -> str: ...

    def update_runtime_state(self, **values: object) -> None: ...
    def current_tool_pool(self) -> ToolPoolPort: ...
    async def refresh_workspace_context(self) -> Any: ...
    def reset(self) -> None: ...
    async def compress_context(
        self,
        *,
        force: bool = False,
        focus: str = "",
    ) -> object: ...


@runtime_checkable
class MCPSupervisorPort(Protocol):
    def status(self) -> Sequence[Mapping[str, object]]: ...
    def conformance_lines(self, target: str = "") -> Sequence[str]: ...
    async def authorize(self, name: str) -> object: ...
    async def get_prompt(
        self,
        server: str,
        prompt_name: str,
        arguments: Mapping[str, str],
    ) -> Mapping[str, Any]: ...


MCPReloadPort = Callable[[], Awaitable[Sequence[str]]]


@runtime_checkable
class SubagentHandlePort(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def status(self) -> object: ...


@runtime_checkable
class SubagentFeedPort(Protocol):
    def render(self) -> str: ...


@runtime_checkable
class SubagentManagerCommandPort(Protocol):
    async def spawn(self, task: str, *, definition: str = "") -> SubagentHandlePort: ...
    def feed(
        self,
        agent_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> SubagentFeedPort: ...
    async def pause(self, agent_id: str) -> SubagentHandlePort: ...
    async def resume(self, agent_id: str) -> SubagentHandlePort: ...
    async def kill(self, agent_id: str) -> SubagentHandlePort: ...
    def definition_lines(self) -> Sequence[str]: ...
    def status_lines(self) -> Sequence[str]: ...
    def sidechain_lines(self, *, limit: int = 50) -> Sequence[str]: ...
    def read_sidechain(
        self,
        target: str,
    ) -> Tuple[str, Sequence[Mapping[str, object]]]: ...


@runtime_checkable
class CheckpointUndoResultPort(Protocol):
    @property
    def restored(self) -> Sequence[str]: ...

    @property
    def deleted(self) -> Sequence[str]: ...

    @property
    def label(self) -> str: ...


@runtime_checkable
class CheckpointManagerPort(Protocol):
    def list_groups(self) -> Sequence[str]: ...
    def undo_last(self) -> Optional[CheckpointUndoResultPort]: ...


@runtime_checkable
class UsageTrackerPort(Protocol):
    def summary(self) -> str: ...


ModelSwitchPort = Callable[[Optional[str]], str]


@runtime_checkable
class SkillIssuePort(Protocol):
    path: object
    message: str


@runtime_checkable
class SkillPort(Protocol):
    name: str
    description: str
    source: str
    location: object
    argument_hint: str
    allowed_tools: Sequence[str]
    user_invocable: bool
    disable_model_invocation: bool
    version: str
    license: str
    metadata: Mapping[str, object]


@runtime_checkable
class SkillResourcePort(Protocol):
    kind: str
    size: int
    path: object


@runtime_checkable
class SkillReloadReportPort(Protocol):
    count: int
    issues: Sequence[SkillIssuePort]


@runtime_checkable
class SkillManagerPort(Protocol):
    enabled: bool
    issues: Sequence[SkillIssuePort]

    def reload(self) -> SkillReloadReportPort: ...
    def list(self, query: str = "") -> Sequence[SkillPort]: ...
    def get(self, name: str) -> Optional[SkillPort]: ...
    def load(
        self,
        name: str,
        arguments: str = "",
        *,
        for_model: bool = True,
    ) -> str: ...
    def user_prompt(self, name: str, arguments: str = "") -> str: ...
    def list_resources(self, name: str) -> Sequence[SkillResourcePort]: ...
    def prompt_index(self) -> str: ...
    def apply_to_prompt(self, prompt: str) -> str: ...


SkillReloadPort = Callable[[], SkillReloadReportPort]
PluginReloadPort = Callable[[], object | Awaitable[object]]


@runtime_checkable
class PluginIssuePort(Protocol):
    path: object
    message: str


@runtime_checkable
class PluginLifecycleResultPort(Protocol):
    name: str
    scope: str
    path: Path
    message: str


@runtime_checkable
class PluginRecordPort(Protocol):
    name: str
    version: str
    status: str
    path: Path
    scope: str
    manifest_version: int
    entrypoint: str
    tools_count: int
    skills_count: int
    agents_count: int
    commands_count: int
    hooks_count: int
    mcp_servers_count: int
    trust_status: str
    registry: str
    key_id: str
    dependencies: Sequence[str]
    dependents: Sequence[str]
    description: str
    error: str
    integrity_digest: str


@runtime_checkable
class TrustedKeyPort(Protocol):
    registry: str
    key_id: str
    fingerprint: str


@runtime_checkable
class PluginTrustStorePort(Protocol):
    def status_lines(self) -> Sequence[str]: ...


@runtime_checkable
class PluginVerificationPort(Protocol):
    status: str
    registry: str
    key_id: str
    signer: str
    integrity_digest: str
    message: str


@runtime_checkable
class PluginManifestPort(Protocol):
    name: str
    version: str


@runtime_checkable
class PluginCommandPort(Protocol):
    name: str
    local_name: str
    plugin_name: str
    scope: str
    prompt: str
    allowed_tools: Sequence[str]
    reminder_ids: Sequence[str]


@runtime_checkable
class PluginRegistryConfigPort(Protocol):
    name: str
    path: str


@runtime_checkable
class PluginManagerPort(Protocol):
    enabled: bool
    issues: Sequence[PluginIssuePort]
    trust_store: PluginTrustStorePort

    def reload(self) -> object | Awaitable[object]: ...
    def records(self) -> Sequence[PluginRecordPort]: ...
    def get_command(self, name: str) -> Optional[PluginCommandPort]: ...
    def status_lines(self) -> Sequence[str]: ...
    def registry_lines(self) -> Sequence[str]: ...
    def dependency_lines(self, name: Optional[str] = None) -> Sequence[str]: ...
    def install(
        self,
        source: str,
        *,
        scope: str = "user",
    ) -> Awaitable[PluginLifecycleResultPort]: ...
    def update(
        self,
        source: str,
        *,
        scope: str = "user",
    ) -> Awaitable[PluginLifecycleResultPort]: ...
    async def set_enabled(
        self,
        name: str,
        enabled: bool,
    ) -> PluginLifecycleResultPort: ...
    async def remove(self, name: str) -> PluginLifecycleResultPort: ...
    def verify(
        self,
        name: str,
    ) -> Tuple[PluginManifestPort, PluginVerificationPort]: ...
    def add_registry(self, name: str, path: str) -> PluginRegistryConfigPort: ...
    def remove_registry(self, name: str) -> PluginRegistryConfigPort: ...
    async def install_from_registry(
        self,
        registry_name: str,
        plugin_name: str,
        *,
        constraint: str = "*",
        scope: str = "user",
    ) -> Sequence[PluginLifecycleResultPort]: ...
    def add_trusted_key(
        self,
        registry: str,
        key_id: str,
        public_key: str,
        *,
        label: str = "",
    ) -> TrustedKeyPort: ...
    def revoke_trusted_key(self, registry: str, key_id: str) -> TrustedKeyPort: ...


@runtime_checkable
class SessionMetadataPort(Protocol):
    def as_record(self) -> Mapping[str, object]: ...


@runtime_checkable
class DeletedSessionPort(Protocol):
    @property
    def source(self) -> Path: ...

    @property
    def archive_path(self) -> Path: ...


@runtime_checkable
class SessionLifecyclePort(Protocol):
    session_path: Optional[Path]

    @property
    def metadata(self) -> Optional[Mapping[str, object]]: ...

    def resume(self, path: Path, *, log: Callable[[str], None]) -> bool: ...
    def fork(self, title: str = "") -> Path: ...
    def rename(self, title: str) -> Path: ...
    def delete(self, target: str, *, confirmed: bool = False) -> DeletedSessionPort: ...


@runtime_checkable
class PermissionRulesPort(Protocol):
    path: object


@runtime_checkable
class PermissionManagerPort(Protocol):
    @property
    def mode(self) -> PermissionMode: ...

    def mode_lines(self) -> Sequence[str]: ...
    def set_mode(self, mode: PermissionMode | str) -> PermissionMode: ...
    def status_line(self) -> str: ...


@runtime_checkable
class PlanManagerPort(Protocol):
    def on_mode_change(
        self,
        previous: PermissionMode,
        current: PermissionMode,
    ) -> None: ...


@runtime_checkable
class RewindTimelinePort(Protocol):
    def render_lines(self) -> Sequence[str]: ...
    def clear(self) -> None: ...


@runtime_checkable
class RewindFilesPort(Protocol):
    @property
    def restored(self) -> Sequence[str]: ...

    @property
    def deleted(self) -> Sequence[str]: ...

    @property
    def groups(self) -> int: ...


@runtime_checkable
class RewindResultPort(Protocol):
    @property
    def target_index(self) -> int: ...

    @property
    def label(self) -> str: ...

    @property
    def removed_turns(self) -> int: ...

    @property
    def files(self) -> RewindFilesPort: ...


@runtime_checkable
class RewindForkResultPort(Protocol):
    @property
    def source_turn(self) -> int: ...

    @property
    def source_turn_id(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def path(self) -> Path: ...

    @property
    def context_checkpoint_id(self) -> str: ...


@runtime_checkable
class RewindPort(Protocol):
    @property
    def timeline(self) -> RewindTimelinePort: ...

    def fork_turn(
        self,
        turn_index: int,
        title: str = "",
    ) -> RewindForkResultPort: ...
    def rewind(self, turn_index: int) -> RewindResultPort: ...


@runtime_checkable
class ShellJobCommandPort(Protocol):
    def list_jobs(self) -> Sequence[ShellJobPort]: ...
    async def output(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        wait_seconds: float = 0,
    ) -> ShellJobPagePort: ...
    async def kill(self, job_id: str) -> ShellJobPort: ...


__all__ = [
    "CheckpointManagerPort",
    "CommandLoopPort",
    "MCPReloadPort",
    "MCPSupervisorPort",
    "ModelSwitchPort",
    "PermissionManagerPort",
    "PlanManagerPort",
    "PluginManagerPort",
    "PluginReloadPort",
    "RewindPort",
    "SessionLifecyclePort",
    "ShellJobCommandPort",
    "SkillManagerPort",
    "SkillReloadPort",
    "SubagentManagerCommandPort",
    "UsageTrackerPort",
]
