"""类型化事件与事件总线（借鉴 DeepSeek Harness 的 typed events）。

UI / 会话持久化 / 审计都是事件总线的旁路订阅者，agent 内核对其零感知。
handler 可以是同步或异步函数；subscribe 返回退订函数。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Tuple, Union

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


@dataclass(frozen=True)
class PermissionDenied(AgentEvent):
    tool_call: ToolCall
    reason: str


@dataclass(frozen=True)
class ContextCompressed(AgentEvent):
    """历史被摘要压缩（Step 7）。"""
    before_tokens: int
    after_tokens: int


@dataclass(frozen=True)
class UserSteered(AgentEvent):
    """运行中用户插话被注入历史（Step 26 steering）。"""
    text: str


@dataclass(frozen=True)
class PlanUpdated(AgentEvent):
    """执行计划快照；items 与 statuses 一一对应。

    status 取值为 pending / in_progress / completed；active_index 指向唯一的
    in_progress，未开始或已全部完成时为 -1。
    """

    items: Tuple[str, ...] = ()
    active_index: int = -1
    statuses: Tuple[str, ...] = ()
    explanation: str = ""


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
class SubagentSpawned(AgentEvent):
    """子代理创建并开始后台执行（Step 13）。"""
    agent_id: str
    task: str


@dataclass(frozen=True)
class SubagentFinished(AgentEvent):
    """子代理到达终态：completed / failed / cancelled。"""
    agent_id: str
    status: str
    preview: str


@dataclass(frozen=True)
class RunFinished(AgentEvent):
    final_text: str


@dataclass(frozen=True)
class RunCancelled(AgentEvent):
    reason: str = "用户中止"


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
