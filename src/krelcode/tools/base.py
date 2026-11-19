"""Tool 端口：工具是纯数据对象（name/description/schema/execute）。

agent 循环只依赖本协议；具体工具在 builtin/ 下各自独立实现，
由组装根注册进 ToolRegistry。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Dict, FrozenSet, Optional, Protocol, Sequence,
    Tuple, Union,
)

from ..core.messages import (
    ImageBlock,
    ResourceBlock,
    StructuredBlock,
    TextBlock,
    ToolResultBlock,
)
from ..safety.sandbox import ExecutionSandbox


@dataclass
class ToolResult:
    """工具执行结果。is_error=True 时 content 是错误描述，
    仍会回填给 LLM（错误恢复原则：让模型自我修正而非中断循环）。"""

    content: str
    is_error: bool = False
    data: Dict[str, Any] = field(default_factory=dict)  # 给 UI/测试用的结构化附加信息
    blocks: Tuple[ToolResultBlock, ...] = ()             # 富结果；content 为文本降级

    @classmethod
    def from_blocks(
        cls,
        blocks: Tuple[ToolResultBlock, ...] | list[ToolResultBlock],
        *,
        is_error: bool = False,
        data: Optional[Dict[str, Any]] = None,
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


class ToolTier(str, Enum):
    """Dynamic tool catalogue exposure level.

    ``CORE`` tools always carry a full provider schema.  ``DISCOVERABLE``
    tools advertise only a name and one-line description.  ``DEFERRED`` tools
    stay out of the default directory and are found through ``tool_search``.
    """

    CORE = "core"
    DISCOVERABLE = "discoverable"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class ToolCatalogMatch:
    """One deterministic result returned by the active turn tool catalogue."""

    name: str
    description: str
    tier: ToolTier
    source: str
    read_only: bool
    loaded: bool


class ToolCatalog(Protocol):
    """Narrow catalogue port exposed to ToolSearch.

    The implementation is a ``TurnToolPool``; tools never receive the mutable
    global registry and therefore cannot bypass runtime filtering.
    """

    def search_catalog(
        self, query: str, *, limit: int = 5,
    ) -> Tuple[ToolCatalogMatch, ...]:
        ...

    def load_tools(self, names: Sequence[str]) -> Tuple[str, ...]:
        ...


@dataclass(frozen=True)
class CommandDescriptor:
    """ToolUseContext 中可见的只读命令摘要，不暴露可执行命令对象。"""

    name: str
    description: str = ""
    kind: str = "command"
    source: str = "builtin"


@dataclass(frozen=True)
class ToolServices:
    """提供给工具/审批层的窄服务索引，避免传递整个应用容器。"""

    commands: Tuple[CommandDescriptor, ...] = ()
    mcp_servers: Tuple[str, ...] = ()

    def has_command(self, name: str) -> bool:
        normalized = name if name.startswith("/") else f"/{name}"
        return any(command.name == normalized for command in self.commands)


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


@dataclass
class TurnAbortSignal:
    """工具可见的协作式中止信号；Task.cancel 仍负责强制中断。"""

    _event: Any = field(default=None, repr=False)
    reason: str = ""

    def __post_init__(self) -> None:
        if self._event is None:
            self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return bool(self._event.is_set())

    def cancel(self, reason: str = "用户中止") -> None:
        self.reason = reason
        self._event.set()

    def check(self) -> None:
        if self.is_cancelled:
            raise asyncio.CancelledError(self.reason or "任务已中止")

    async def wait(self) -> None:
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
    """工具执行环境；默认自动创建运行时 Sandbox（fail-safe）。

    ``tool_descriptor`` 是 AgentLoop 为单次调用派生的上下文快照。工具和审批
    策略看到同一份来源/副作用信息，不共享可变的全局 AppState。
    """

    workspace_root: Path
    sandbox: Optional[ExecutionSandbox] = None
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
    shell_jobs: Any = field(default=None, repr=False)
    tool_catalog: Optional[ToolCatalog] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.sandbox_enabled:
            self.sandbox = None
        elif self.sandbox is None:
            self.sandbox = ExecutionSandbox(self.workspace_root)

    async def emit_progress(
        self,
        message: str,
        data: Optional[Dict[str, object]] = None,
    ) -> None:
        """发布短进度；具体工具不直接依赖 EventBus 或 AgentLoop。"""
        if self.progress_emitter is None:
            return
        pending = self.progress_emitter(message, dict(data or {}))
        if inspect.isawaitable(pending):
            await pending

    async def emit_output(self, text: str, stream: str = "stdout") -> None:
        """发布输出增量；空片段被静默忽略。"""
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
        """请求前台补充输入；没有交互前台时明确返回 None（fail-closed）。"""
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
        """发布计划快照；具体工具不依赖 UI 或 EventBus。"""
        if self.plan_emitter is None:
            return
        pending = self.plan_emitter(
            tuple(items), tuple(statuses), active_index, explanation)
        if inspect.isawaitable(pending):
            await pending


class Tool(Protocol):
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema (object)

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...


class ToolSource(str, Enum):
    """工具来源；用于动态工具池过滤、/tools 展示和审计。"""

    BUILTIN = "builtin"
    SKILL = "skill"
    SUBAGENT = "subagent"
    MCP = "mcp"
    PLUGIN = "plugin"


@dataclass(frozen=True)
class ToolDescriptor:
    """Tool 的实现元数据，不进入 provider 的 function schema。"""

    source: ToolSource = ToolSource.BUILTIN
    mcp_server: Optional[str] = None
    read_only: bool = False
    required_feature: Optional[str] = None
    run_modes: Optional[FrozenSet[RunMode]] = None
    plugin_name: Optional[str] = None
    tier: Optional[ToolTier] = None
    search_terms: Tuple[str, ...] = ()

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

    @classmethod
    def from_tool(
        cls,
        tool: "Tool",
        *,
        source: ToolSource = ToolSource.BUILTIN,
        mcp_server: Optional[str] = None,
    ) -> "ToolDescriptor":
        """从 Tool 的可选能力标注构建描述符。

    未声明 ``read_only`` 的工具按 mutating 处理，这是调度器的保守默认值。
        """
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
        )
