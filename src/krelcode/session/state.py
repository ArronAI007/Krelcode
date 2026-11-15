"""交互会话的显式界面状态。

状态对象只描述 REPL 当前“看到什么”：是否有一轮任务、是否正在中止、
输入框模式与当前进度。历史消息和执行任务仍分别由 AgentLoop 与
AgentRuntime 持有，避免把业务事实复制到 UI 状态中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


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
    _turn_sequence: int = field(default=0, init=False, repr=False)

    @property
    def is_running(self) -> bool:
        return self.phase is not SessionPhase.IDLE

    @property
    def is_stopping(self) -> bool:
        return self.phase is SessionPhase.STOPPING

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
        return self.current_turn_id

    def update_progress(self, progress: str) -> None:
        if self.is_running:
            self.progress = progress

    def request_stop(self) -> bool:
        """进入 stopping；空闲时返回 False。"""
        if not self.is_running:
            return False
        self.phase = SessionPhase.STOPPING
        self.progress = "正在中止"
        return True

    def finish_turn(self) -> None:
        """清理本轮现场；可重复调用。"""
        self.phase = SessionPhase.IDLE
        self.current_turn_id = None
        self.progress = ""
