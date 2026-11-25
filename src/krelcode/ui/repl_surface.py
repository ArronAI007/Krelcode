"""REPL command affordances, queue helpers, and startup banner.

These are pure terminal-facing helpers with no reference back to the REPL
process host.  ``ui.repl`` re-exports them for compatibility.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from .. import __version__
from ..agent.turn.loop import AgentLoop
from ..core.events import ShellJobFinished
from ..safety.runtime import PlanApprovalCoordinator
from ..session.commands import CommandRegistry, CommandResult, builtin_command_registry
from ..session.prompt_queue import PromptQueue
from ..session.state import SessionViewState
from .repl_primitives import (
    CYAN,
    DIM,
    IDLE_PROMPT,
    MENU_ROWS,
    RESET,
    RUNNING_HINT,
    RUNNING_PROMPT,
    ReplControl,
    TerminalSubmission,
    _display_width,
    _truncate_display,
    auto_color,
)
from .render import HERMES_AMBER, HERMES_BRONZE, HERMES_GOLD

_BUILTIN_COMMANDS = builtin_command_registry()
SLASH_COMMANDS = tuple(
    (spec.invocation, spec.description) for spec in _BUILTIN_COMMANDS.list())
RUNNING_SLASH_COMMANDS = tuple(
    (spec.invocation, spec.description)
    for spec in _BUILTIN_COMMANDS.list(running=True))


def match_slash_commands(text: str, *, running: bool = False,
                         limit: int = 9,
                         registry: Optional[CommandRegistry] = None
                         ) -> list[tuple[str, str]]:
    """返回与当前输入前缀匹配的 slash 命令；进入参数后不再弹菜单。"""
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    catalog = registry or _BUILTIN_COMMANDS
    return [(spec.invocation, spec.description)
            for spec in catalog.list(query=text, running=running)[:limit]]


QUEUE_USAGE = "用法：/queue | /queue delete <n> | /queue move <n> <m> | /queue clear"

_WORDMARK = (
    "██╗  ██╗██████╗ ███████╗██╗      ██████╗ ██████╗ ██████╗ ███████╗",
    "██║ ██╔╝██╔══██╗██╔════╝██║     ██╔════╝██╔═══██╗██╔══██╗██╔════╝",
    "█████╔╝ ██████╔╝█████╗  ██║     ██║     ██║   ██║██║  ██║█████╗",
    "██╔═██╗ ██╔══██╗██╔══╝  ██║     ██║     ██║   ██║██║  ██║██╔══╝",
    "██║  ██╗██║  ██║███████╗███████╗╚██████╗╚██████╔╝██████╔╝███████╗",
    "╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝",
)

# Hermes TUI's default six-line logo uses primary, accent, border twice each.
_WORDMARK_COLORS = (
    HERMES_GOLD,
    HERMES_GOLD,
    HERMES_AMBER,
    HERMES_AMBER,
    HERMES_BRONZE,
    HERMES_BRONZE,
)


def _format_job_notification(event: ShellJobFinished) -> str:
    mark = {
        "completed": "✓",
        "failed": "✗",
        "killed": "■",
    }.get(event.status, "•")
    exit_text = f" exit {event.exit_code}" if event.exit_code is not None else ""
    preview = " ".join(event.preview.split())
    if len(preview) > 120:
        preview = preview[:119] + "…"
    suffix = f": {preview}" if preview else ""
    return (
        f"{mark} 后台任务 {event.job_id} {event.status}{exit_text}{suffix}")


def _queue_label(item: Any, limit: int = 100) -> str:
    label = str(getattr(item, "label", "") or "")
    if not label:
        label = str(getattr(item, "raw_text", "") or "")
    if not label:
        label = str(getattr(item, "text", "") or "")
    text = " ".join(label.split())
    return _truncate_display(text, limit)


def _format_prompt_queue(queue: PromptQueue[Any]) -> list[str]:
    if not queue:
        return ["下一条消息队列为空"]
    lines = [f"下一条消息队列（{len(queue)}）:"]
    for index, action in enumerate(queue.entries, 1):
        lines.append(f"  #{index} {_queue_label(action)}")
    return lines


def manage_prompt_queue(
    arguments: str,
    queue: PromptQueue[Any],
    *,
    write: Callable[[str], None] = print,
) -> None:
    """执行 /queue 的纯 UI 管理动作，不进入 SessionController。"""
    parts = arguments.split()
    if not parts:
        for line in _format_prompt_queue(queue):
            write(line)
        return
    operation = parts[0].lower()
    try:
        if operation == "clear" and len(parts) == 1:
            count = len(queue)
            queue.clear()
            write(f"已清空下一条消息队列（{count} 条）")
            return
        if operation == "delete" and len(parts) == 2:
            action = queue.delete(int(parts[1]))
            write(f"已从队列删除: {_queue_label(action)}")
            return
        if operation == "move" and len(parts) == 3:
            source = int(parts[1])
            target = int(parts[2])
            queue.move(source, target)
            write(f"已把队列 #{source} 移动到 #{target}")
            return
    except (ValueError, IndexError) as exc:
        message = str(exc) if str(exc) else "序号必须是整数"
        write(f"队列操作失败: {message}")
        return
    write(QUEUE_USAGE)


def format_plan_lines(state: SessionViewState) -> list[str]:
    """把 reducer 中的计划快照渲染为纯文本行，供 /plan 与测试复用。"""
    if not state.plan_items:
        return ["当前没有执行计划"]
    statuses = list(state.plan_statuses)
    if len(statuses) != len(state.plan_items):
        statuses = [
            "in_progress" if index == state.plan_active_index else "pending"
            for index in range(len(state.plan_items))
        ]
    marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    completed = sum(status == "completed" for status in statuses)
    lines = [f"当前执行计划（{completed}/{len(statuses)} 完成）:"]
    for index, (item, status) in enumerate(
            zip(state.plan_items, statuses), 1):
        lines.append(f"  {marks.get(status, '[?]')} {index}. {item}")
    if state.plan_explanation:
        lines.append(f"  说明: {state.plan_explanation}")
    approval = {
        "pending": "待确认 · /plan approve 执行 · /plan cancel 取消",
        "approved": "已批准",
        "cancelled": "已取消",
    }.get(state.plan_approval_status)
    if approval:
        lines.append(f"  状态: {approval}")
    return lines


def handle_plan_command(
    arguments: str,
    plan_manager: PlanApprovalCoordinator,
    state: SessionViewState,
    *,
    allow_actions: bool = True,
) -> CommandResult:
    """执行 `/plan` 的查看、生成、批准与取消动作。"""
    text = (arguments or "").strip()
    operation = text.lower()
    if operation == "approve":
        if not allow_actions:
            return CommandResult(
                handled=True,
                output=("  计划执行确认需等待当前任务结束后操作",),
            )
        ok, message, prompt = plan_manager.approve()
        state.plan_approval_status = plan_manager.status.value
        return CommandResult(
            handled=True,
            output=(f"  [plan] {message}",),
            run_prompt=prompt if ok else None,
        )
    if operation == "cancel":
        if not allow_actions:
            return CommandResult(
                handled=True,
                output=("  计划取消需等待当前任务结束后操作",),
            )
        ok, message = plan_manager.cancel()
        state.plan_approval_status = plan_manager.status.value
        return CommandResult(handled=True, output=(f"  [plan] {message}",))
    if text:
        if not allow_actions:
            return CommandResult(
                handled=True,
                output=("  当前任务运行中：只能使用 /plan 查看计划",),
            )
        try:
            prompt = plan_manager.start_request(text)
        except ValueError as exc:
            return CommandResult(
                handled=True, output=(f"  [plan] {exc}",))
        state.plan_approval_status = plan_manager.status.value
        return CommandResult(
            handled=True,
            output=("  [plan] 已进入 Plan 模式，开始只读调查并生成计划",),
            run_prompt=prompt,
        )
    lines = (
        plan_manager.render_lines()
        if plan_manager.snapshot.items
        else format_plan_lines(state)
    )
    return CommandResult(
        handled=True,
        output=tuple(f"  {line}" for line in lines),
    )




def print_banner(loop: AgentLoop, tool_names: Optional[list[str]],
                 session_path: Optional[str] = None,
                 write: Callable[[str], None] = print,
                 color: Optional[bool] = None) -> None:
    """Print a compact, low-noise startup card."""
    if color is None:
        color = auto_color()
    provider = getattr(loop.llm, "name", "?")
    model = getattr(loop.llm, "model", "?")
    workspace = loop.tool_context.workspace_root
    columns = shutil.get_terminal_size((80, 24)).columns
    width = max(36, min(columns - 4, 72))
    divider = "─" * width
    divider_rendered = f"{DIM}{divider}{RESET}" if color else divider

    def accent(text: str) -> str:
        return f"{CYAN}{text}{RESET}" if color else text

    def brand(text: str, tone: str = HERMES_GOLD) -> str:
        return f"{tone}{text}{RESET}" if color else text

    def row(label: str, value: str, path: bool = False) -> None:
        available = max(8, width - 13)
        if path and _display_width(value) > available:
            value = "…" + value[-(available - 1):]
        else:
            value = _truncate_display(value, available)
        label_text = f"{label:<9}"
        rendered_label = f"{DIM}{label_text}{RESET}" if color else label_text
        write(f"  {rendered_label} {value}")

    sandbox = getattr(loop.tool_context, "sandbox", None)
    sandbox_backend = str(getattr(sandbox, "backend", "") or "off")
    sandbox_network = (
        "network on" if bool(getattr(sandbox, "network", False))
        else "network off"
    )
    if sandbox_backend == "static":
        safety = f"static (limited) · {sandbox_network}"
    else:
        safety = f"{sandbox_backend} · {sandbox_network}"

    wordmark_width = max(_display_width(line) for line in _WORDMARK) + 2
    if columns >= wordmark_width:
        for line, tone in zip(_WORDMARK, _WORDMARK_COLORS):
            write(f"  {brand(line, tone)}")
        version = f"v{__version__}"
        write(f"  {DIM}{version}{RESET}" if color else f"  {version}")
    else:
        title = f"✻ Krelcode v{__version__}"
        write(f"  {brand(title)}")
    write(f"  {divider_rendered}")
    row("model", f"{model} · {provider}")
    row("workspace", str(workspace), path=True)
    if session_path:
        row("session", Path(session_path).stem)
    row("tools", f"{len(tool_names or [])} 个")
    row("sandbox", safety)
    write(f"  {divider_rendered}")

    write(
        f"  {accent('/help')} 命令 · {accent('Ctrl+O')} 面板 · "
        f"Shift+Tab 权限 · {accent('/exit')} 退出")
    write("")


__all__ = [
    "QUEUE_USAGE",
    "RUNNING_HINT",
    "RUNNING_SLASH_COMMANDS",
    "SLASH_COMMANDS",
    "_format_job_notification",
    "_format_prompt_queue",
    "_queue_label",
    "format_plan_lines",
    "handle_plan_command",
    "manage_prompt_queue",
    "match_slash_commands",
    "print_banner",
]
