"""Provider-neutral runtime and permission values.

These values are shared by the agent loop, tool catalogue, approval policy,
subagents and prompt assembly.  They deliberately depend only on the standard
library so no adapter layer has to import another adapter to describe runtime
state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Callable,
    FrozenSet,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)

from ..core.events import EventBus
from ..core.messages import ImageRef, SteeringInput
from .application import ApplicationRequest, TaskBudget
from .identity import (
    HarnessReleaseIdentity,
    RuntimeIdentity,
    RuntimeSandboxBinding,
)
from .loop import WaitCondition

if TYPE_CHECKING:
    from .agent import AgentContract

RUNTIME_RELEASE_SCHEMA_VERSION = 1

_RELEASE_COMPONENTS = (
    "agent_definition",
    "prompt_release",
    "model_profile",
    "tool_catalog",
    "mcp_servers",
    "plugins",
    "skills",
    "policy_version",
    "memory_schema",
    "context_engine",
    "code_version",
)


class RunMode(str, Enum):
    """当前进程发起查询的运行形态；工具池和权限据此做差异化暴露。"""

    REPL = "repl"
    PRINT = "print"
    JSON = "json"
    SDK = "sdk"
    REVIEW = "review"
    SUBAGENT = "subagent"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    READ_ONLY = "read-only"
    AUTO_APPROVE = "auto-approve"
    PLAN = "plan"


class RuntimeTaskStatus(str, Enum):
    """Lifecycle state owned by a production task runtime."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    RETRYING = "retrying"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            RuntimeTaskStatus.COMPLETED,
            RuntimeTaskStatus.FAILED,
            RuntimeTaskStatus.CANCELLED,
            RuntimeTaskStatus.TIMED_OUT,
            RuntimeTaskStatus.INTERRUPTED,
        }


@dataclass(frozen=True)
class RuntimeRetryPolicy:
    """Bounded retry behavior for one task."""

    max_attempts: int = 1
    backoff_seconds: float = 0.0
    max_backoff_seconds: float = 30.0
    retry_on_failure: bool = False
    retry_on_timeout: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_attempts", max(1, int(self.max_attempts)))
        object.__setattr__(
            self, "backoff_seconds", max(0.0, float(self.backoff_seconds)))
        object.__setattr__(
            self,
            "max_backoff_seconds",
            max(self.backoff_seconds, float(self.max_backoff_seconds)),
        )

    def delay_for(self, attempt: int) -> float:
        if self.backoff_seconds <= 0:
            return 0.0
        return float(min(
            self.max_backoff_seconds,
            self.backoff_seconds * (2 ** max(0, attempt - 1)),
        ))


@dataclass(frozen=True)
class RuntimeLease:
    """Ownership proof for one physically running task."""

    lease_id: str
    task_id: str
    owner: str
    acquired_at: float
    expires_at: float


@dataclass(frozen=True)
class RuntimeCheckpointRef:
    """Content-light reference to durable task or context state."""

    checkpoint_id: str
    kind: str
    session_id: str = ""
    turn_id: str = ""
    task_id: str = ""
    digest: str = ""
    path: str = ""
    metadata: Mapping[str, object] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "checkpoint_id", str(self.checkpoint_id or ""))
        object.__setattr__(self, "kind", str(self.kind or "context"))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class RuntimeTaskState:
    """Physical task state exchanged across the runtime boundary."""

    task_id: str
    turn_id: str
    status: RuntimeTaskStatus
    lease: Optional[RuntimeLease] = None
    checkpoint: Optional[RuntimeCheckpointRef] = None
    sequence: int = 0
    error: str = ""
    attempts: int = 0
    deadline_at: Optional[float] = None
    started_at: Optional[float] = None
    paused_at: Optional[float] = None
    resume_from_checkpoint_id: str = ""
    updated_at: float = 0.0
    wait_reason: str = ""
    resume_token: str = ""
    waiting_since: Optional[float] = None
    resumed_at: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RuntimeTaskStatus(self.status))


@dataclass(frozen=True)
class RuntimeStateRef:
    """Opaque reference to one immutable task-state revision."""

    state_id: str
    task_id: str
    turn_id: str
    sequence: int
    updated_at: float
    checkpoint: Optional[RuntimeCheckpointRef] = None


@dataclass(frozen=True)
class RuntimeTask:
    """Provider-neutral task submitted to a production runtime."""

    turn_id: str
    message: str
    application: ApplicationRequest
    images: tuple[ImageRef, ...] = ()
    tool_allowlist: Optional[tuple[str, ...]] = None
    reminder_ids: tuple[str, ...] = ()
    identity: RuntimeIdentity = dataclass_field(default_factory=RuntimeIdentity)
    harness_release: HarnessReleaseIdentity = dataclass_field(
        default_factory=HarnessReleaseIdentity)
    retry_policy: RuntimeRetryPolicy = dataclass_field(
        default_factory=RuntimeRetryPolicy)
    sandbox: RuntimeSandboxBinding = dataclass_field(
        default_factory=RuntimeSandboxBinding)
    deadline_at: Optional[float] = None
    resume_from: Optional[RuntimeCheckpointRef] = None
    metadata: Mapping[str, object] = dataclass_field(default_factory=dict)
    agent_contract: Optional["AgentContract"] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "turn_id", str(self.turn_id or ""))
        object.__setattr__(self, "message", str(self.message or ""))
        object.__setattr__(self, "images", tuple(self.images or ()))
        if self.tool_allowlist is not None:
            object.__setattr__(
                self, "tool_allowlist", tuple(self.tool_allowlist))
        object.__setattr__(
            self, "reminder_ids", tuple(self.reminder_ids or ()))
        object.__setattr__(
            self,
            "deadline_at",
            float(self.deadline_at) if self.deadline_at is not None else None,
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.agent_contract is not None:
            if self.agent_contract.intent != self.application:
                raise ValueError(
                    "RuntimeTask.application 与 AgentContract.intent 不一致")
            if self.agent_contract.principal != self.identity:
                raise ValueError(
                    "RuntimeTask.identity 与 AgentContract.principal 不一致")
            if self.agent_contract.sandbox != self.sandbox:
                raise ValueError(
                    "RuntimeTask.sandbox 与 AgentContract.sandbox 不一致")
            if self.agent_contract.harness_release != self.harness_release:
                raise ValueError(
                    "RuntimeTask.harness_release 与 "
                    "AgentContract.harness_release 不一致")
            contract_tools = self.agent_contract.authority.allowed_tools
            if (
                contract_tools is not None
                and frozenset(self.tool_allowlist or ())
                != frozenset(contract_tools)
            ):
                raise ValueError(
                    "RuntimeTask.tool_allowlist 与 "
                    "AgentContract.authority.allowed_tools 不一致")

    @property
    def budget(self) -> TaskBudget:
        return self.application.budget

    @property
    def contract(self) -> "AgentContract":
        if self.agent_contract is None:
            raise RuntimeError("RuntimeTask 未绑定 AgentContract")
        return self.agent_contract


class RuntimeTaskHandle(Protocol):
    """Control surface returned by a runtime after task submission."""

    @property
    def task_id(self) -> str: ...

    @property
    def turn_id(self) -> str: ...

    @property
    def state(self) -> RuntimeTaskState: ...

    @property
    def done(self) -> bool: ...

    async def wait(self) -> str: ...

    async def cancel(self) -> None: ...

    async def pause(self) -> bool: ...

    async def resume(self) -> bool: ...


class RuntimeTaskExecutorPort(Protocol):
    """Harness operation executed by a production runtime."""

    async def execute(self, task: RuntimeTask) -> str: ...


class RuntimeTaskRuntimePort(Protocol):
    """Task/state boundary between Harness orchestration and execution."""

    def submit(
        self,
        task: RuntimeTask,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle: ...

    async def cancel(self, task_id: str) -> bool: ...

    async def pause(self, task_id: str) -> bool: ...

    async def resume(self, task_id: str) -> bool: ...

    def inspect(self, task_id: str) -> RuntimeTaskState: ...

    def state_ref(self, task_id: str) -> RuntimeStateRef: ...

    def checkpoint(self, task_id: str) -> Optional[RuntimeCheckpointRef]: ...

    def record_checkpoint(
        self,
        task_id: str,
        checkpoint: RuntimeCheckpointRef,
    ) -> RuntimeTaskState: ...

    def record_waiting(
        self,
        task_id: str,
        condition: WaitCondition,
    ) -> RuntimeTaskState: ...

    def record_resumed(self, task_id: str) -> RuntimeTaskState: ...


class RuntimeContinuationPort(Protocol):
    """Restore semantic task state from a durable checkpoint."""

    def resume(
        self,
        checkpoint: RuntimeCheckpointRef,
    ) -> "RuntimeContinuationResult": ...


@dataclass(frozen=True)
class RuntimeContinuationResult:
    """Content-light outcome of applying one continuation checkpoint."""

    checkpoint_id: str
    session_id: str = ""
    turn_id: str = ""
    history_messages: int = 0
    compact_checkpoint_id: str = ""
    working_context_entries: int = 0
    stale_files: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    workspace_refresh_pending: bool = True
    state: Optional[RuntimeTaskState] = None
    state_bundle_id: str = ""


class RuntimeResourcePort(Protocol):
    """One long-lived runtime resource with idempotent closure."""

    @property
    def resource_name(self) -> str: ...

    async def close(self) -> None: ...


class RuntimeResourceRegistryPort(Protocol):
    """Unified ownership for runtime-scoped long-lived resources."""

    def register(self, resource: RuntimeResourcePort) -> None: ...

    async def close_all(self) -> tuple[str, ...]: ...


class HarnessTurnControlPort(Protocol):
    """Semantic Harness controls consumed by the runtime-facing facade."""

    @property
    def bus(self) -> EventBus: ...

    @property
    def workspace_root(self) -> Path: ...

    @property
    def tool_names(self) -> Sequence[str]: ...

    @property
    def shell_jobs(self) -> object | None: ...

    def bind_session(self, session_id: Optional[str]) -> None: ...

    def bind_runtime_identity(self, identity: RuntimeIdentity) -> None: ...

    def validate_harness_release(
        self,
        release: HarnessReleaseIdentity,
    ) -> None: ...

    def set_acceptance_evaluator(
        self,
        evaluator: object | None,
    ) -> None: ...

    def set_control_plane(
        self,
        control_plane: object | None,
    ) -> None: ...

    def set_runtime_snapshot_provider(
        self,
        provider: Optional[Callable[[RuntimeSnapshot], RuntimeSnapshot]],
    ) -> None: ...

    def validate_sandbox_binding(
        self,
        binding: RuntimeSandboxBinding,
    ) -> None: ...

    async def run_turn(
        self,
        user_input: str,
        images: Optional[list[ImageRef]] = None,
        *,
        turn_id: Optional[str] = None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[Sequence[str]] = None,
        application_request: Optional[ApplicationRequest] = None,
        agent_contract: Optional["AgentContract"] = None,
    ) -> str: ...

    def submit_steering(self, item: SteeringInput | str) -> None: ...

    def drain_steering(self) -> list[SteeringInput | str]: ...

    def abort_current_turn(self, reason: str = "用户中止") -> None: ...

    async def close_runtime_resources(self) -> None: ...


@dataclass(frozen=True)
class RuntimeSnapshot:
    """一轮查询的不可变运行时快照（AppState 的窄化替代）。"""

    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    run_mode: RunMode = RunMode.REPL
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    features: FrozenSet[str] = frozenset()
    disabled_tools: FrozenSet[str] = frozenset()
    alive_mcp_servers: FrozenSet[str] = frozenset()
    mcp_health_known: bool = False
    tool_allowlist: Optional[FrozenSet[str]] = None
    mcp_allowlist: Optional[FrozenSet[str]] = None
    # Organization-level allow/deny patterns are separate from a Skill's
    # temporary allowlist. The tool pool applies them before advertising a
    # schema, so hiding a tool and denying its execution cannot drift apart.
    managed_tool_allowlist: Optional[FrozenSet[str]] = None
    managed_tool_denylist: Optional[FrozenSet[str]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_mode", RunMode(self.run_mode))
        object.__setattr__(
            self, "permission_mode", PermissionMode(self.permission_mode))
        object.__setattr__(self, "features", frozenset(self.features or ()))
        object.__setattr__(
            self, "disabled_tools", frozenset(self.disabled_tools or ()))
        object.__setattr__(
            self, "alive_mcp_servers",
            frozenset(self.alive_mcp_servers or ()))
        if self.tool_allowlist is not None:
            object.__setattr__(
                self, "tool_allowlist", frozenset(self.tool_allowlist))
        if self.mcp_allowlist is not None:
            object.__setattr__(
                self, "mcp_allowlist", frozenset(self.mcp_allowlist))
        if self.managed_tool_allowlist is not None:
            object.__setattr__(
                self, "managed_tool_allowlist",
                frozenset(self.managed_tool_allowlist))
        if self.managed_tool_denylist is not None:
            object.__setattr__(
                self, "managed_tool_denylist",
                frozenset(self.managed_tool_denylist))


@dataclass(frozen=True)
class ArtifactIdentity:
    """Content-addressed artifact/version identity."""

    kind: str
    id: str
    version: str = ""
    digest: str = ""
    required: bool = True
    expected_digest: str = ""
    metadata: Mapping[str, object] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind or "").strip())
        object.__setattr__(self, "id", str(self.id or "").strip())
        object.__setattr__(self, "version", str(self.version or "").strip())
        object.__setattr__(self, "digest", str(self.digest or "").strip())
        object.__setattr__(
            self, "expected_digest",
            str(self.expected_digest or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def artifact_id(self) -> str:
        return self.id


@dataclass(frozen=True)
class RuntimeReleaseManifest:
    """Complete, content-light identity of one runtime composition."""

    agent_definition: Mapping[str, object] = dataclass_field(default_factory=dict)
    prompt_release: Mapping[str, object] = dataclass_field(default_factory=dict)
    model_profile: Mapping[str, object] = dataclass_field(default_factory=dict)
    tool_catalog: Mapping[str, object] = dataclass_field(default_factory=dict)
    mcp_servers: Mapping[str, object] = dataclass_field(default_factory=dict)
    plugins: Mapping[str, object] = dataclass_field(default_factory=dict)
    skills: Mapping[str, object] = dataclass_field(default_factory=dict)
    policy_version: Mapping[str, object] = dataclass_field(default_factory=dict)
    memory_schema: Mapping[str, object] = dataclass_field(default_factory=dict)
    context_engine: Mapping[str, object] = dataclass_field(default_factory=dict)
    code_version: Mapping[str, object] = dataclass_field(default_factory=dict)
    schema_version: int = RUNTIME_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in _RELEASE_COMPONENTS:
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                MappingProxyType(
                    dict(value) if isinstance(value, Mapping) else {}),
            )

    @property
    def release_hash(self) -> str:
        return _stable_hash(self.to_dict(include_hashes=False))

    @property
    def release_id(self) -> str:
        return f"runtime-{self.release_hash[:16]}"

    def component_hashes(self) -> dict[str, str]:
        return {
            name: _stable_hash(dict(getattr(self, name)))
            for name in _RELEASE_COMPONENTS
        }

    def to_dict(self, *, include_hashes: bool = True) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": self.schema_version,
            **{
                name: dict(getattr(self, name))
                for name in _RELEASE_COMPONENTS
            },
        }
        if include_hashes:
            record["release_id"] = self.release_id
            record["release_hash"] = self.release_hash
            record["component_hashes"] = self.component_hashes()
        return record

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "RuntimeReleaseManifest":
        return cls(
            **{
                name: (
                    dict(value)
                    if isinstance((value := raw.get(name)), Mapping) else {}
                )
                for name in _RELEASE_COMPONENTS
            },
            schema_version=int(
                str(raw.get("schema_version")
                    or RUNTIME_RELEASE_SCHEMA_VERSION)),
        )

    def diff(self, other: "RuntimeReleaseManifest") -> dict[str, object]:
        """Return deterministic component-level release differences."""
        left_hashes = self.component_hashes()
        right_hashes = other.component_hashes()
        changed: dict[str, object] = {}
        unchanged: list[str] = []
        for name in _RELEASE_COMPONENTS:
            if left_hashes[name] == right_hashes[name]:
                unchanged.append(name)
                continue
            changed[name] = {
                "left_hash": left_hashes[name],
                "right_hash": right_hashes[name],
                "left": dict(getattr(self, name)),
                "right": dict(getattr(other, name)),
            }
        return {
            "left_release_id": self.release_id,
            "right_release_id": other.release_id,
            "left_release_hash": self.release_hash,
            "right_release_hash": other.release_hash,
            "changed_components": changed,
            "unchanged_components": unchanged,
        }

def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ArtifactIdentity",
    "HarnessReleaseIdentity",
    "HarnessTurnControlPort",
    "PermissionMode",
    "RunMode",
    "RUNTIME_RELEASE_SCHEMA_VERSION",
    "RuntimeCheckpointRef",
    "RuntimeContinuationPort",
    "RuntimeContinuationResult",
    "RuntimeLease",
    "RuntimeIdentity",
    "RuntimeRetryPolicy",
    "RuntimeResourcePort",
    "RuntimeResourceRegistryPort",
    "RuntimeSandboxBinding",
    "RuntimeReleaseManifest",
    "RuntimeSnapshot",
    "RuntimeStateRef",
    "RuntimeTask",
    "RuntimeTaskExecutorPort",
    "RuntimeTaskHandle",
    "RuntimeTaskRuntimePort",
    "RuntimeTaskState",
    "RuntimeTaskStatus",
]
