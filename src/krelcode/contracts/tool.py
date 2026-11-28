"""Provider-neutral Tool DTOs and execution contracts."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Awaitable, Callable, Dict, FrozenSet, Optional, Protocol, Sequence, Tuple,
    Union,
)

from ..core.messages import (
    ImageBlock,
    ResourceBlock,
    StructuredBlock,
    TextBlock,
    ToolResultBlock,
)
from .runtime import PermissionMode, RunMode, RuntimeSnapshot
from .governance import GovernanceDecision
from .json import (
    JsonObject,
    ToolExecutionData,
    ensure_json_object,
)
from .safety import SandboxPort
from .service_ports import (
    EnterprisePolicyPort,
    PrincipalPort,
    PromptBenchmarkServicesPort,
    PromptFragmentRegistryPort,
    PromptReleasePipelinePort,
    ShellJobManagerPort,
)
from .loop import WaitCondition
from .external_event import ExternalEventPort

@dataclass
class ToolResult:
    """工具执行结果；错误会回填给模型用于自我修正。"""

    content: str
    is_error: bool = False
    data: ToolExecutionData = field(default_factory=dict)
    blocks: Tuple[ToolResultBlock, ...] = ()

    @classmethod
    def from_blocks(
        cls,
        blocks: Tuple[ToolResultBlock, ...] | list[ToolResultBlock],
        *,
        is_error: bool = False,
        data: Optional[ToolExecutionData] = None,
    ) -> "ToolResult":
        normalized = tuple(blocks)
        text_parts = []
        for block in normalized:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ImageBlock):
                text_parts.append(block.alt or f"[图片 {block.media_type}]")
            elif isinstance(block, ResourceBlock):
                label = block.name or block.uri
                detail = block.text or (
                    f"[二进制资源 {block.mime_type or 'application/octet-stream'}]"
                    if block.blob else "")
                text_parts.append(f"{label}\n{detail}".strip())
            elif isinstance(block, StructuredBlock):
                import json
                text_parts.append(json.dumps(
                    block.data, ensure_ascii=False, sort_keys=True))
        return cls(
            content="\n".join(part for part in text_parts if part),
            is_error=is_error,
            data=dict(data or {}),
            blocks=normalized,
        )

    def __post_init__(self) -> None:
        self.data = ensure_json_object(
            self.data, path="ToolResult.data")


class ToolTier(str, Enum):
    CORE = "core"
    DISCOVERABLE = "discoverable"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class ToolCatalogMatch:
    name: str
    description: str
    tier: ToolTier
    source: str
    read_only: bool
    loaded: bool


class ToolCatalog(Protocol):
    def search_catalog(
        self, query: str, *, limit: int = 5,
    ) -> Tuple[ToolCatalogMatch, ...]:
        ...

    def load_tools(self, names: Sequence[str]) -> Tuple[str, ...]:
        ...


@dataclass(frozen=True)
class CommandDescriptor:
    name: str
    description: str = ""
    kind: str = "command"
    source: str = "builtin"


@dataclass(frozen=True)
class ToolServices:
    commands: Tuple[CommandDescriptor, ...] = ()
    mcp_servers: Tuple[str, ...] = ()
    external_events: Optional[ExternalEventPort] = None

    def has_command(self, name: str) -> bool:
        normalized = name if name.startswith("/") else f"/{name}"
        return any(command.name == normalized for command in self.commands)


@dataclass
class TurnAbortSignal:
    """工具可见的协作式中止信号；Task.cancel 仍负责强制中断。"""

    _event: Optional[asyncio.Event] = field(default=None, repr=False)
    reason: str = ""

    def __post_init__(self) -> None:
        if self._event is None:
            self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return bool(self._event is not None and self._event.is_set())

    def cancel(self, reason: str = "用户中止") -> None:
        self.reason = reason
        if self._event is None:
            self._event = asyncio.Event()
        self._event.set()

    def check(self) -> None:
        if self.is_cancelled:
            raise asyncio.CancelledError(self.reason or "任务已中止")

    async def wait(self) -> None:
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()


ProgressEmitter = Callable[[str, Dict[str, object]], Union[None, Awaitable[None]]]
OutputEmitter = Callable[[str, str], Union[None, Awaitable[None]]]
InputRequester = Callable[
    [str, Tuple[str, ...], bool], Awaitable[Optional[str]]]
PlanEmitter = Callable[
    [Tuple[str, ...], Tuple[str, ...], int, str],
    Union[None, Awaitable[None]],
]


@dataclass
class ToolContext:
    """工具执行环境的数据契约；具体 Sandbox 由适配层注入。"""

    workspace_root: Path
    sandbox: Optional[SandboxPort] = None
    sandbox_enabled: bool = True
    tool_descriptor: Optional["ToolDescriptor"] = None
    runtime: RuntimeSnapshot = field(default_factory=RuntimeSnapshot)
    services: ToolServices = field(default_factory=ToolServices)
    abort: TurnAbortSignal = field(default_factory=TurnAbortSignal)
    progress_emitter: Optional[ProgressEmitter] = field(
        default=None, repr=False)
    output_emitter: Optional[OutputEmitter] = field(default=None, repr=False)
    input_requester: Optional[InputRequester] = field(default=None, repr=False)
    plan_emitter: Optional[PlanEmitter] = field(default=None, repr=False)
    shell_jobs: Optional[ShellJobManagerPort] = field(
        default=None, repr=False)
    tool_catalog: Optional[ToolCatalog] = field(default=None, repr=False)
    prompt_registry: Optional[PromptFragmentRegistryPort] = field(
        default=None, repr=False)
    prompt_release_pipeline: Optional[PromptReleasePipelinePort] = field(
        default=None, repr=False)
    prompt_benchmark_services: Optional[PromptBenchmarkServicesPort] = field(
        default=None, repr=False)
    enterprise_policy: Optional[EnterprisePolicyPort] = field(
        default=None, repr=False)
    principal: Optional[PrincipalPort] = field(default=None, repr=False)
    governance_decision: Optional[GovernanceDecision] = field(
        default=None, repr=False)
    waiting_emitter: Optional[
        Callable[[WaitCondition], Awaitable[None]]
    ] = field(default=None, repr=False)
    resume_emitter: Optional[
        Callable[[str], Awaitable[None]]
    ] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.sandbox_enabled:
            self.sandbox = None

    async def emit_progress(
        self,
        message: str,
        data: Optional[Dict[str, object]] = None,
    ) -> None:
        if self.progress_emitter is None:
            return
        pending = self.progress_emitter(message, dict(data or {}))
        if inspect.isawaitable(pending):
            await pending

    async def emit_output(self, text: str, stream: str = "stdout") -> None:
        if not text or self.output_emitter is None:
            return
        pending = self.output_emitter(text, stream)
        if inspect.isawaitable(pending):
            await pending

    async def request_user_input(
        self,
        prompt: str,
        options: Tuple[str, ...] = (),
        *,
        allow_free_text: bool = True,
    ) -> Optional[str]:
        if self.input_requester is None:
            return None
        return await self.input_requester(prompt, tuple(options), allow_free_text)

    async def emit_plan(
        self,
        items: Tuple[str, ...],
        statuses: Tuple[str, ...],
        active_index: int = -1,
        explanation: str = "",
    ) -> None:
        if self.plan_emitter is None:
            return
        pending = self.plan_emitter(
            tuple(items), tuple(statuses), active_index, explanation)
        if inspect.isawaitable(pending):
            await pending

    async def emit_waiting(self, condition: WaitCondition) -> None:
        if self.waiting_emitter is None:
            return
        await self.waiting_emitter(condition)

    async def emit_resumed(self, request_id: str) -> None:
        if self.resume_emitter is None:
            return
        await self.resume_emitter(request_id)


class Tool(Protocol):
    name: str
    description: str
    parameters: JsonObject

    async def execute(self, args: JsonObject, ctx: ToolContext) -> ToolResult:
        ...


class ToolSource(str, Enum):
    BUILTIN = "builtin"
    SKILL = "skill"
    SUBAGENT = "subagent"
    MCP = "mcp"
    PLUGIN = "plugin"


class ToolExecutionRuntime(str, Enum):
    """Concrete execution tier selected before a tool is invoked."""

    LOCAL = "local"
    MCP = "mcp"
    SANDBOX = "sandbox"
    REMOTE = "remote"


@dataclass(frozen=True)
class ToolDescriptor:
    source: ToolSource = ToolSource.BUILTIN
    mcp_server: Optional[str] = None
    read_only: bool = False
    required_feature: Optional[str] = None
    run_modes: Optional[FrozenSet[RunMode]] = None
    plugin_name: Optional[str] = None
    tier: Optional[ToolTier] = None
    search_terms: Tuple[str, ...] = ()
    execution_runtime: Optional[ToolExecutionRuntime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", ToolSource(self.source))
        tier = self.tier
        if tier is None:
            tier = (
                ToolTier.DEFERRED
                if self.source in {ToolSource.MCP, ToolSource.PLUGIN}
                else ToolTier.CORE
            )
        object.__setattr__(self, "tier", ToolTier(tier))
        if self.run_modes is not None:
            object.__setattr__(
                self,
                "run_modes",
                frozenset(RunMode(mode) for mode in self.run_modes),
            )
        raw_terms = self.search_terms
        if isinstance(raw_terms, str):
            raw_terms = (raw_terms,)
        object.__setattr__(
            self,
            "search_terms",
            tuple(dict.fromkeys(
                str(term).strip().casefold()
                for term in (raw_terms or ())
                if str(term).strip()
            )),
        )
        if self.execution_runtime is not None:
            object.__setattr__(
                self,
                "execution_runtime",
                ToolExecutionRuntime(self.execution_runtime),
            )

    @property
    def resolved_execution_runtime(self) -> ToolExecutionRuntime:
        if self.execution_runtime is not None:
            return self.execution_runtime
        if self.source is ToolSource.MCP:
            return ToolExecutionRuntime.MCP
        if self.source is ToolSource.SUBAGENT:
            return ToolExecutionRuntime.REMOTE
        return ToolExecutionRuntime.LOCAL

    @classmethod
    def from_tool(
        cls,
        tool: "Tool",
        *,
        source: ToolSource = ToolSource.BUILTIN,
        mcp_server: Optional[str] = None,
    ) -> "ToolDescriptor":
        source = ToolSource(source)
        return cls(
            source=source,
            mcp_server=mcp_server,
            read_only=bool(getattr(tool, "read_only", False)),
            required_feature=getattr(tool, "required_feature", None),
            run_modes=getattr(tool, "run_modes", None),
            plugin_name=getattr(tool, "plugin_name", None),
            tier=getattr(tool, "tool_tier", None),
            search_terms=getattr(tool, "search_terms", ()),
            execution_runtime=getattr(tool, "execution_runtime", None),
        )


__all__ = [
    "CommandDescriptor",
    "PermissionMode",
    "ProgressEmitter",
    "InputRequester",
    "OutputEmitter",
    "PlanEmitter",
    "RunMode",
    "RuntimeSnapshot",
    "Tool",
    "ToolCatalog",
    "ToolCatalogMatch",
    "ToolContext",
    "ToolDescriptor",
    "ToolExecutionRuntime",
    "ToolResult",
    "ToolServices",
    "ToolSource",
    "ToolTier",
    "TurnAbortSignal",
]
