"""交互会话的显式界面状态与 QueryEvent reducer。

状态对象只描述 REPL 当前“看到什么”：是否有一轮任务、是否正在中止、
输入框模式、当前阶段、工具进度与计划。历史消息和执行任务仍分别由
AgentLoop 与 AgentRuntime 持有，避免把业务事实复制到 UI 状态中。

``SessionEventReducer`` 是 session 层唯一的 QueryEvent → view state 映射点。
它不导入终端或渲染器，因此 REPL、未来 TUI 与测试可以共享完全相同的状态语义。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..agent.query import QueryEvent
from ..core.events import (
    ApprovalRequested,
    ContextCompressed,
    ErrorOccurred,
    LLMResponseReceived,
    LLMStreamDelta,
    LLMThinkingDelta,
    PermissionDenied,
    PlanUpdated,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunStarted,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserInputResolved,
    UserSteered,
)


class SessionPhase(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"


@dataclass
class SessionViewState:
    """REPL 的轻量状态机，不依赖终端、事件循环或 Agent 实现。"""

    phase: SessionPhase = SessionPhase.IDLE
    raw_input: bool = False
    current_turn_id: Optional[str] = None
    progress: str = ""
    query_phase: str = "idle"
    current_step: int = 0
    active_tool_id: str = ""
    active_tool_name: str = ""
    tool_calls_started: int = 0
    tool_calls_completed: int = 0
    tool_output_chars: int = 0
    plan_items: tuple[str, ...] = ()
    plan_statuses: tuple[str, ...] = ()
    plan_active_index: int = -1
    plan_explanation: str = ""
    input_request_id: str = ""
    input_prompt: str = ""
    input_options: tuple[str, ...] = ()
    input_allow_free_text: bool = True
    last_activity: float = 0.0
    started_at: Optional[float] = None
    _turn_sequence: int = field(default=0, init=False, repr=False)

    @property
    def is_running(self) -> bool:
        return self.phase is not SessionPhase.IDLE

    @property
    def is_stopping(self) -> bool:
        return self.phase is SessionPhase.STOPPING

    @property
    def active_tool(self) -> str:
        return self.active_tool_name

    @property
    def awaiting_user_input(self) -> bool:
        return bool(self.input_request_id)

    def begin_turn(
        self, progress: str = "等待模型响应", *, turn_id: Optional[str] = None,
    ) -> str:
        """记录新一轮开始，返回供日志/UI 关联的稳定 turn id。"""
        if self.is_running:
            raise RuntimeError("已有任务正在运行")
        self._turn_sequence += 1
        self.current_turn_id = turn_id or f"turn-{self._turn_sequence}"
        self.phase = SessionPhase.RUNNING
        self.progress = progress
        self.query_phase = "awaiting_model"
        self.current_step = 0
        self.active_tool_id = ""
        self.active_tool_name = ""
        self.tool_calls_started = 0
        self.tool_calls_completed = 0
        self.tool_output_chars = 0
        self.plan_items = ()
        self.plan_statuses = ()
        self.plan_active_index = -1
        self.plan_explanation = ""
        self.input_request_id = ""
        self.input_prompt = ""
        self.input_options = ()
        self.input_allow_free_text = True
        self.started_at = time.monotonic()
        self.last_activity = self.started_at
        return self.current_turn_id

    def update_progress(self, progress: str) -> None:
        if self.is_running:
            self.progress = progress
            self.last_activity = time.monotonic()

    def request_stop(self) -> bool:
        """进入 stopping；空闲时返回 False。"""
        if not self.is_running:
            return False
        self.phase = SessionPhase.STOPPING
        self.progress = "正在中止"
        self.query_phase = "stopping"
        self.last_activity = time.monotonic()
        return True

    def finish_turn(self) -> None:
        """清理本轮现场；可重复调用。"""
        self.phase = SessionPhase.IDLE
        self.current_turn_id = None
        self.progress = ""
        self.query_phase = "idle"
        self.current_step = 0
        self.active_tool_id = ""
        self.active_tool_name = ""
        self.tool_calls_started = 0
        self.tool_calls_completed = 0
        self.tool_output_chars = 0
        self.input_request_id = ""
        self.input_prompt = ""
        self.input_options = ()
        self.input_allow_free_text = True
        self.last_activity = 0.0
        self.started_at = None

    def status_text(self, now: Optional[float] = None) -> str:
        """生成单行状态栏；纯文本、无 ANSI、无终端依赖。"""
        if not self.is_running:
            return ""
        text = self.progress or "工作中"
        parts = [text]
        if self.current_step > 0:
            parts.append(f"Step {self.current_step}")
        if self.tool_calls_started:
            parts.append(
                f"工具 {self.tool_calls_completed}/{self.tool_calls_started}")
        if self.active_tool_name and self.tool_output_chars:
            parts.append(f"输出 {self.tool_output_chars} 字符")
        if self.started_at is not None:
            elapsed = max(0.0, (now if now is not None else time.monotonic())
                          - self.started_at)
            parts.append(f"{elapsed:.1f}s")
        return " · ".join(parts)


class SessionEventReducer:
    """把有序 QueryEvent 折叠为 ``SessionViewState``。

    reducer 故意不发出任何 UI 副作用；调用方在 apply 后自行渲染原始事件并
    刷新状态栏。这样同一套状态语义可被 REPL/TUI/测试复用。
    """

    def apply(self, query_event: QueryEvent, state: SessionViewState) -> None:
        event = query_event.event
        now = time.monotonic()

        if isinstance(event, RunStarted):
            if not state.is_running:
                state.begin_turn(turn_id=query_event.turn_id)
            state.query_phase = "awaiting_model"
            state.progress = "等待模型响应"
        elif isinstance(event, StepStarted):
            state.current_step = event.step
            state.query_phase = "preparing"
            state.progress = f"准备第 {event.step} 步"
        elif isinstance(event, QueryPhaseChanged):
            state.query_phase = event.phase
            state.progress = event.detail or _PHASE_LABELS.get(
                event.phase, event.phase)
        elif isinstance(event, LLMThinkingDelta):
            if event.text:
                state.query_phase = "thinking"
                state.progress = "模型正在思考"
        elif isinstance(event, LLMStreamDelta):
            if event.text:
                state.query_phase = "responding"
                state.progress = "正在接收回答"
        elif isinstance(event, LLMResponseReceived):
            state.query_phase = "model_done"
            state.progress = "模型响应完成"
        elif isinstance(event, ToolCallStarted):
            tool_call = event.tool_call
            state.query_phase = "tool"
            state.active_tool_id = tool_call.id
            state.active_tool_name = tool_call.name
            state.tool_calls_started += 1
            state.tool_output_chars = 0
            state.progress = f"执行工具 {tool_call.name}"
        elif isinstance(event, ToolProgress):
            state.active_tool_id = event.tool_call_id
            state.active_tool_name = event.tool_name
            state.progress = f"{event.tool_name} · {event.message}"
        elif isinstance(event, ToolOutputDelta):
            if event.tool_call_id and event.tool_call_id == state.active_tool_id:
                state.tool_output_chars += len(event.text)
        elif isinstance(event, ToolCallCompleted):
            state.tool_calls_completed += 1
            if event.tool_call.id == state.active_tool_id:
                state.active_tool_id = ""
                state.active_tool_name = ""
            state.progress = (
                f"工具 {event.tool_call.name} "
                f"{'失败' if event.is_error else '完成'}")
        elif isinstance(event, ApprovalRequested):
            state.query_phase = "approval"
            state.progress = f"等待审批 {event.tool_call.name}"
        elif isinstance(event, PermissionDenied):
            state.progress = f"{event.tool_call.name} 权限被拒绝"
        elif isinstance(event, ContextCompressed):
            state.progress = (
                f"上下文已压缩 {event.before_tokens} → {event.after_tokens} tokens")
        elif isinstance(event, UserSteered):
            state.progress = "已接收运行中插话"
        elif isinstance(event, PlanUpdated):
            state.plan_items = tuple(event.items)
            state.plan_statuses = tuple(event.statuses)
            if len(state.plan_statuses) != len(state.plan_items):
                state.plan_statuses = tuple(
                    "in_progress" if index == event.active_index else "pending"
                    for index in range(len(state.plan_items))
                )
            state.plan_active_index = event.active_index
            state.plan_explanation = event.explanation
            if 0 <= event.active_index < len(state.plan_items):
                state.progress = f"计划：{state.plan_items[event.active_index]}"
        elif isinstance(event, UserInputRequested):
            state.input_request_id = event.request_id
            state.input_prompt = event.prompt
            state.input_options = tuple(event.options)
            state.input_allow_free_text = event.allow_free_text
            state.query_phase = "input"
            state.progress = "等待用户输入"
        elif isinstance(event, UserInputResolved):
            if event.request_id == state.input_request_id:
                state.input_request_id = ""
                state.input_prompt = ""
                state.input_options = ()
                state.input_allow_free_text = True
            state.query_phase = "tool"
            state.progress = (
                "已收到用户输入" if event.answered else "用户输入请求已取消")
        elif isinstance(event, ErrorOccurred):
            state.progress = f"错误：{event.error}"
        elif isinstance(event, RunCancelled):
            state.query_phase = "cancelled"
            state.progress = event.reason or "已取消"
            state.input_request_id = ""
            state.input_prompt = ""
            state.input_options = ()
            state.input_allow_free_text = True
        elif isinstance(event, RunFinished):
            state.query_phase = "finished"
            state.progress = "回答完成"
            state.input_request_id = ""
            state.input_prompt = ""
            state.input_options = ()
            state.input_allow_free_text = True
            state.active_tool_id = ""
            state.active_tool_name = ""
        state.last_activity = now


_PHASE_LABELS = {
    "awaiting_model": "等待模型响应",
    "preparing": "准备请求",
    "model": "模型生成中",
    "thinking": "模型正在思考",
    "responding": "正在接收回答",
    "tool": "执行工具",
    "finalizing": "正在整理回答",
    "approval": "等待审批",
    "input": "等待用户输入",
}
