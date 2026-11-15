"""会话、历史和导出类内置命令 handlers。"""

from __future__ import annotations

from pathlib import Path

from ...export import export_session
from ...manager import format_sessions, list_sessions, resolve_resume_target
from ...restore import restore_session
from ..builtin import render_help
from ..context import CommandContext
from ..types import CommandResult


def _ok(*output: str, clear_tool_outputs: bool = False) -> CommandResult:
    return CommandResult(
        handled=True,
        output=tuple(output),
        clear_tool_outputs=clear_tool_outputs,
    )


async def handle_help(ctx: CommandContext) -> CommandResult:
    return _ok(render_help(ctx.command_registry))


async def handle_history(ctx: CommandContext) -> CommandResult:
    output: list[str] = []
    for message in ctx.loop.history:
        content = message.content or ""
        extra = (f" tool_calls={[tc.name for tc in message.tool_calls]}"
                 if message.tool_calls else "")
        output.append(f"  [{message.role}]{extra} {content}")
    return _ok(*output)


async def handle_session(ctx: CommandContext) -> CommandResult:
    return _ok(f"  当前会话文件: {ctx.session_path or '（未启用，--no-session）'}")


async def handle_sessions(ctx: CommandContext) -> CommandResult:
    if ctx.sessions_dir is None:
        return _ok("  （会话目录未知）")
    return _ok(format_sessions(list_sessions(ctx.sessions_dir)))


async def handle_resume(ctx: CommandContext) -> CommandResult:
    target = ctx.request.arguments or "latest"
    path = resolve_resume_target(target, ctx.sessions_dir)
    if path is None:
        return _ok(f"  （找不到会话: {target}；用 /sessions 查看列表）")
    output: list[str] = []
    restored = restore_session(ctx.loop, path, log=output.append)
    return _ok(*output, clear_tool_outputs=restored)


async def handle_export(ctx: CommandContext) -> CommandResult:
    try:
        if ctx.request.arguments:
            out = export_session(ctx.loop.history, Path(ctx.request.arguments))
        else:
            out = export_session(
                ctx.loop.history,
                export_dir=ctx.workspace_root / ".krelcode" / "exports")
    except OSError as exc:
        return _ok(f"  [export] 导出失败: {exc}")
    return _ok(f"  [export] 已导出: {out}")


async def handle_reset(ctx: CommandContext) -> CommandResult:
    ctx.loop.reset()
    return _ok("（历史已清空）", clear_tool_outputs=True)
