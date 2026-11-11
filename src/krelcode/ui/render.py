"""事件渲染器：EventBus 的旁路订阅者，负责流式输出与上色。

内核不知道渲染器的存在；渲染器只消费事件。
write/color 均可注入，测试无需真实终端。
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional

from ..core.events import (
    AgentEvent,
    ContextCompressed,
    ErrorOccurred,
    LLMStreamDelta,
    PermissionDenied,
    RunFinished,
    ToolCallCompleted,
    ToolCallStarted,
)

GREEN = "\x1b[32m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

TOOL_CONTENT_PREVIEW = 200


def auto_color() -> bool:
    """tty 且未设置 NO_COLOR 时才上色。"""
    return bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")


def colorize_diff(text: str, enabled: bool = True) -> str:
    """unified diff 上色：+绿 -红 @@青。"""
    if not enabled:
        return text
    out = []
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out.append(DIM + line + RESET)
        elif line.startswith("+"):
            out.append(GREEN + line + RESET)
        elif line.startswith("-"):
            out.append(RED + line + RESET)
        elif line.startswith("@@"):
            out.append(CYAN + line + RESET)
        else:
            out.append(line)
    return "\n".join(out)


def _default_write(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


class StreamRenderer:
    """把事件流渲染到终端：LLM 文本增量流式打印，工具活动打状态行。"""

    def __init__(
        self,
        write: Optional[Callable[[str], None]] = None,
        color: Optional[bool] = None,
    ) -> None:
        self._write = write or _default_write
        self._color = auto_color() if color is None else color
        self._streaming = False

    # EventBus handler（同步即可）
    def __call__(self, event: AgentEvent) -> None:
        if isinstance(event, LLMStreamDelta):
            if not self._streaming:
                self._write("agent> ")
                self._streaming = True
            self._write(event.text)
        elif isinstance(event, ToolCallStarted):
            self.end_stream()
            args = event.tool_call.arguments
            self._write(f"  {DIM}[tool] 调用 {event.tool_call.name}({args}){RESET}\n"
                        if self._color else
                        f"  [tool] 调用 {event.tool_call.name}({args})\n")
        elif isinstance(event, ToolCallCompleted):
            self.end_stream()
            self._render_tool_result(event)
        elif isinstance(event, PermissionDenied):
            self.end_stream()
            self._write(f"  [deny] {event.tool_call.name} 被拒绝: {event.reason}\n")
        elif isinstance(event, ContextCompressed):
            self.end_stream()
            self._write(
                f"  [context] 历史已压缩: {event.before_tokens} → {event.after_tokens} tokens\n")
        elif isinstance(event, ErrorOccurred):
            self.end_stream()
            self._write(f"  [error] {event.context}: {event.error}\n")
        elif isinstance(event, RunFinished):
            if self._streaming:
                self._write("\n")
                self._streaming = False
            else:
                # 未经流式的最终文本（如 max_steps/卡死中止的系统消息）
                self._write(f"agent> {event.final_text}\n")

    def end_stream(self) -> None:
        """结束当前流式行（工具活动插入前调用）。"""
        if self._streaming:
            self._write("\n")
            self._streaming = False

    def _render_tool_result(self, event: ToolCallCompleted) -> None:
        mark = "✗" if event.is_error else "✓"
        name = event.tool_call.name
        if not event.is_error and name == "edit_file":
            # edit_file：首行结果 + 完整（工具已限 100 行）上色 diff
            lines = event.content.split("\n", 1)
            self._write(f"  [tool] {mark} {name}: {lines[0]}\n")
            if len(lines) > 1:
                self._write(colorize_diff(lines[1], self._color) + "\n")
            return
        content = event.content.replace("\n", " ")
        if len(content) > TOOL_CONTENT_PREVIEW:
            content = content[:TOOL_CONTENT_PREVIEW] + "…"
        self._write(f"  [tool] {mark} {name} → {content}\n")
