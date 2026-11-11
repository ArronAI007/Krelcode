"""类型化事件与事件总线（借鉴 DeepSeek Harness 的 typed events）。

UI / 会话持久化 / 审计都是事件总线的旁路订阅者，agent 内核对其零感知。
handler 可以是同步或异步函数；subscribe 返回退订函数。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from .messages import Message, ToolCall


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
class LLMResponseReceived(AgentEvent):
    message: Message
    usage: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallStarted(AgentEvent):
    tool_call: ToolCall


@dataclass(frozen=True)
class ToolCallCompleted(AgentEvent):
    tool_call: ToolCall
    content: str
    is_error: bool


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
class RunFinished(AgentEvent):
    final_text: str


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
