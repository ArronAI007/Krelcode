"""Structural command-layer service ports.

Command handlers must not require concrete AgentLoop, MCP, plugin, memory or
session implementations.  These protocols describe the shell each handler
needs while leaving lifecycle and wire-level behavior in the owning modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from ...core.messages import Message


@runtime_checkable
class ToolDescriptorPort(Protocol):
    read_only: bool
    source: object
    tier: object


@runtime_checkable
class ToolEntryPort(Protocol):
    name: str
    tool: object
    descriptor: ToolDescriptorPort


@runtime_checkable
class ToolPoolPort(Protocol):
    def catalog_entries(self) -> Sequence[ToolEntryPort]: ...
    def is_loaded(self, name: str) -> bool: ...
    def excluded_items(self) -> Sequence[Tuple[str, str]]: ...
    def names(self) -> Sequence[str]: ...


@runtime_checkable
class ContextManagerPort(Protocol):
    max_tokens: int
    high_watermark_tokens: int
    low_watermark_tokens: int


@runtime_checkable
class CommandLoopPort(Protocol):
    """Runtime state and operations command handlers may touch directly."""

    history: Sequence[Message]
    policy: object
    prompt_registry: object
    workspace_context: object
    context_manager: Optional[ContextManagerPort]
    skill_manager: object
    skill_base_prompt: object

    def current_tool_pool(self) -> ToolPoolPort: ...
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
    ) -> object: ...


MCPReloadPort = Callable[[], Awaitable[Sequence[str]]]


@runtime_checkable
class SubagentHandlePort(Protocol):
    id: str
    status: object


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
    restored: Sequence[str]
    deleted: Sequence[str]
    label: str


@runtime_checkable
class CheckpointManagerPort(Protocol):
    def list_groups(self) -> Sequence[str]: ...
    def undo_last(self) -> Optional[CheckpointUndoResultPort]: ...


@runtime_checkable
class UsageTrackerPort(Protocol):
    def summary(self) -> str: ...


ModelSwitchPort = Callable[[str], str]


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
    source: Path
    archive_path: Path


@runtime_checkable
class SessionLifecyclePort(Protocol):
    session_path: Optional[Path]
    metadata: Optional[SessionMetadataPort]

    def resume(self, path: Path, *, log: Callable[[str], None]) -> bool: ...
    def fork(self, title: str = "") -> Path: ...
    def rename(self, title: str) -> Path: ...
    def delete(self, target: str, *, confirmed: bool = False) -> DeletedSessionPort: ...


@runtime_checkable
class PermissionRulesPort(Protocol):
    path: object


@runtime_checkable
class PermissionManagerPort(Protocol):
    mode: object

    def mode_lines(self) -> Sequence[str]: ...
    def set_mode(self, mode: object) -> None: ...
    def status_line(self) -> str: ...


@runtime_checkable
class PlanManagerPort(Protocol):
    def on_mode_change(self, previous: object, current: object) -> None: ...


@runtime_checkable
class RewindTimelinePort(Protocol):
    def render_lines(self) -> Sequence[str]: ...
    def clear(self) -> None: ...


@runtime_checkable
class RewindResultPort(Protocol):
    source_turn: int
    path: Path
    target_index: int
    label: str
    removed_turns: int
    files: object


@runtime_checkable
class RewindPort(Protocol):
    timeline: RewindTimelinePort

    def fork_turn(self, turn_index: int, title: str = "") -> RewindResultPort: ...
    def rewind(self, turn_index: int) -> RewindResultPort: ...


@runtime_checkable
class ShellJobCommandPort(Protocol):
    def list_jobs(self) -> Sequence[object]: ...
    async def output(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        wait_seconds: float = 0,
    ) -> object: ...
    async def kill(self, job_id: str) -> object: ...


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
