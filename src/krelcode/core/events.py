"""类型化事件与事件总线（借鉴 DeepSeek Harness 的 typed events）。

UI / 会话持久化 / 审计都是事件总线的旁路订阅者，agent 内核对其零感知。
handler 可以是同步或异步函数；subscribe 返回退订函数。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import (
    Awaitable, Callable, Dict, List, Mapping, Optional, Tuple, Union,
)

from .context_usage import ContextUsageSnapshot
from .messages import Message, ToolCall, ToolResultBlock


# ---------------------------------------------------------------------------
# 事件家族
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentEvent:
    """所有事件的基类。"""


@dataclass(frozen=True)
class RunStarted(AgentEvent):
    user_input: str
    turn_id: str = ""
    session_id: str = ""
    application: Mapping[str, object] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class StepStarted(AgentEvent):
    step: int  # 从 1 开始


@dataclass(frozen=True)
class LLMStreamDelta(AgentEvent):
    text: str


@dataclass(frozen=True)
class LLMThinkingDelta(AgentEvent):
    """reasoning 模型的增量思考文本（仅展示用，不进历史）。"""
    text: str


@dataclass(frozen=True)
class LLMResponseReceived(AgentEvent):
    message: Message
    usage: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryPhaseChanged(AgentEvent):
    """查询推进层进入了一个新的可观察阶段。

    phase 是稳定机器标识（如 ``model`` / ``tool`` / ``finalizing``）；
    detail 只用于前台状态栏，不参与控制流。
    """

    phase: str
    detail: str = ""


@dataclass(frozen=True)
class ToolCallStarted(AgentEvent):
    tool_call: ToolCall


@dataclass(frozen=True)
class ToolProgress(AgentEvent):
    """工具执行中的短状态；不包含大段正文。"""

    tool_call_id: str
    tool_name: str
    message: str
    data: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutputDelta(AgentEvent):
    """工具输出的增量片段，供前台缓存/按需展开。"""

    tool_call_id: str
    tool_name: str
    text: str
    stream: str = "stdout"


@dataclass(frozen=True)
class ToolCallCompleted(AgentEvent):
    tool_call: ToolCall
    content: str
    is_error: bool
    blocks: tuple[ToolResultBlock, ...] = ()
    data: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HookTriggered(AgentEvent):
    """Hook 已完成一次可观察/可阻断的决策（不暴露完整命令与输出）。"""
    trigger: str
    matched: bool = False
    blocked: bool = False
    tool_name: str = ""
    reason: str = ""
    system_message: str = ""


@dataclass(frozen=True)
class ApprovalRequested(AgentEvent):
    """策略层需要人工审批（UI 订阅后弹确认；非交互场景由策略 fail-closed）。"""
    tool_call: ToolCall
    reason: str
    # Broker/transport 层的一次性关联键；空值时兼容旧订阅者并退回 tool_call.id。
    request_id: str = ""


@dataclass(frozen=True)
class PermissionDenied(AgentEvent):
    tool_call: ToolCall
    reason: str


@dataclass(frozen=True)
class SecurityDecisionRecorded(AgentEvent):
    """Content-free unified security evidence for audit and trace joins."""

    tool_call_id: str
    security: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AcceptanceEvaluated(AgentEvent):
    """Final acceptance evidence for the current application request."""

    acceptance: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextMicrocompacted(AgentEvent):
    """Old tool results were deterministically summarized/referenced.

    The event carries only accounting metadata; no tool output or working
    context text is exposed to recorder, stream-json or UI subscribers.
    """

    trigger: str = "manual"
    before_tokens: int = 0
    after_tokens: int = 0
    compacted_count: int = 0
    preserved_count: int = 0
    protected_count: int = 0
    summarized_count: int = 0
    referenced_count: int = 0


# Compatibility name used by early Step 89 integrations.
ToolsMicrocompacted = ContextMicrocompacted


@dataclass(frozen=True)
class ContextCompressed(AgentEvent):
    """历史被结构化摘要压缩，并保留可审计的交接元数据。"""
    before_tokens: int
    after_tokens: int
    strategy: str = "structured_summary"
    summary_source: str = "model"
    generation: int = 0
    high_watermark_tokens: int = 0
    low_watermark_tokens: int = 0
    source_savings: Tuple[Tuple[str, int], ...] = ()
    checkpoint_id: str = ""


@dataclass(frozen=True)
class ContextCheckpointCreated(AgentEvent):
    """A full resume/fork context checkpoint was persisted.

    The event carries identifiers, counts and hashes only. Full history,
    Working Context bodies and memory text remain in the workspace-local
    checkpoint store and are never copied into UI or stream-json channels.
    """

    checkpoint_id: str
    session_id: str = ""
    turn_id: str = ""
    phase: str = "end"
    history_hash: str = ""
    file_count: int = 0
    memory_count: int = 0
    compact_checkpoint_id: str = ""
    context_plan_hash: str = ""


@dataclass(frozen=True)
class ContextAssembled(AgentEvent):
    """一次真实模型请求的上下文组装快照。

    快照只包含 token 分项、缓存边界和哈希，不携带 Prompt 正文。
    """
    snapshot: ContextUsageSnapshot
    ledger: Optional[Mapping[str, object]] = None


@dataclass(frozen=True)
class ContextDropped(AgentEvent):
    """Budget or scope filtering omitted one or more context sources.

    Only stable source identifiers and accounting metadata are exposed. The
    omitted source bodies remain in the workspace-local stores.
    """

    items: Tuple[str, ...] = ()
    reason: str = "budget"


@dataclass(frozen=True)
class MemoryLoaded(AgentEvent):
    """Memory entries injected into one concrete model request.

    Entry identities, scopes and token accounting are content-free. Memory
    bodies stay inside the assembled prompt and are never copied into events.
    """

    entry_ids: Tuple[str, ...] = ()
    scope_counts: Tuple[Tuple[str, int], ...] = ()
    tokens: int = 0
    source: str = "scoped_memory"


@dataclass(frozen=True)
class MemoryCandidateObserved(AgentEvent):
    """Auto Memory produced or refreshed one review candidate."""

    candidate_id: str
    status: str = "pending"
    trigger: str = ""
    confidence: float = 0.0
    conflict_count: int = 0
    digest: str = ""


@dataclass(frozen=True)
class MemoryWritten(AgentEvent):
    """A memory mutation completed; no memory body is included."""

    memory_id: str = ""
    action: str = "added"
    scope: str = ""
    kind: str = ""
    tokens: int = 0
    source: str = "manual"


@dataclass(frozen=True)
class CompactCheckpointCreated(AgentEvent):
    """A pre-compact handoff checkpoint was persisted."""

    checkpoint_id: str
    generation: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    summary_digest: str = ""
    strategy: str = "structured_summary"
    trigger: str = "manual"
    source_savings: Tuple[Tuple[str, int], ...] = ()


@dataclass(frozen=True)
class UserSteered(AgentEvent):
    """运行中用户插话被注入历史（Step 26 steering）。"""
    text: str


@dataclass(frozen=True)
class PlanUpdated(AgentEvent):
    """执行计划快照；items 与 statuses 一一对应。

    status 取值为 pending / in_progress / completed；active_index 指向唯一的
    in_progress，未开始或已全部完成时为 -1。turn_id 供会话时间线把计划
    更新归入正确边界；旁路消费者可继续忽略该可选字段。
    """

    items: Tuple[str, ...] = ()
    active_index: int = -1
    statuses: Tuple[str, ...] = ()
    explanation: str = ""
    turn_id: str = ""


@dataclass(frozen=True)
class UserInputRequested(AgentEvent):
    """工具/工作流请求前台补充输入。"""

    request_id: str
    prompt: str
    options: Tuple[str, ...] = ()
    allow_free_text: bool = True


@dataclass(frozen=True)
class UserInputResolved(AgentEvent):
    """请求已结束；不携带答案正文，避免重复进入可视化/持久化通道。"""

    request_id: str
    answered: bool = False


@dataclass(frozen=True)
class ShellJobStarted(AgentEvent):
    """后台 Shell job 已启动；命令正文应已脱敏。"""

    job_id: str
    command: str
    pid: int
    cwd: str = ""


@dataclass(frozen=True)
class ShellJobOutput(AgentEvent):
    """后台 Shell job 的实时输出片段（已脱敏）。"""

    job_id: str
    sequence: int
    text: str


@dataclass(frozen=True)
class ShellJobFinished(AgentEvent):
    """后台 Shell job 到达终态；preview 为输出尾部摘要。"""

    job_id: str
    command: str
    status: str
    exit_code: Optional[int] = None
    preview: str = ""


@dataclass(frozen=True)
class SubagentSpawned(AgentEvent):
    """子代理创建并开始后台执行。"""
    agent_id: str
    task: str
    definition: str = ""


@dataclass(frozen=True)
class SubagentFeedUpdated(AgentEvent):
    """子代理 feed 新增一条已脱敏的有序事件。"""
    agent_id: str
    sequence: int
    kind: str
    text: str
    data: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentStateChanged(AgentEvent):
    """子代理运行状态变化；terminal 仍由 SubagentFinished 收口。"""
    agent_id: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class SubagentFinished(AgentEvent):
    """子代理一轮或整个运行到达终态：completed / failed / cancelled / killed。"""
    agent_id: str
    status: str
    preview: str


@dataclass(frozen=True)
class RunFinished(AgentEvent):
    final_text: str
    turn_id: str = ""


@dataclass(frozen=True)
class RunCancelled(AgentEvent):
    reason: str = "用户中止"
    turn_id: str = ""


@dataclass(frozen=True)
class ErrorOccurred(AgentEvent):
    error: str
    context: str = ""


# ---------------------------------------------------------------------------
# 事件总线
# ---------------------------------------------------------------------------

EventHandler = Callable[[AgentEvent], Union[None, Awaitable[None]]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: List[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """注册 handler，返回退订函数。"""
        self._handlers.append(handler)

        def unsubscribe() -> None:
            try:
                self._handlers.remove(handler)
            except ValueError:
                pass

        return unsubscribe

    async def emit(self, event: AgentEvent) -> None:
        """按订阅顺序分发事件；handler 抛出的异常向上传播（尽早暴露 bug）。"""
        for handler in list(self._handlers):
            result = handler(event)
            if inspect.isawaitable(result):
                await result

    @property
    def handler_count(self) -> int:
        return len(self._handlers)
