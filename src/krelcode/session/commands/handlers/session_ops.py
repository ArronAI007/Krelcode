"""会话、历史和导出类内置命令 handlers。"""

from __future__ import annotations

from pathlib import Path

from ...export import export_native_session, export_session
from ...lifecycle import (
    SessionConfirmationRequired,
    SessionLifecycleError,
)
from ...manager import (
    format_sessions,
    inspect_session,
    list_sessions,
    resolve_resume_target,
    search_sessions,
)
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
    path = (ctx.session_lifecycle.session_path
            if ctx.session_lifecycle is not None
            else (Path(ctx.session_path) if ctx.session_path else None))
    if path is None:
        return _ok("  当前会话: （未启用，--no-session）")
    info = inspect_session(path)
    lines = [f"  当前会话文件: {path}"]
    if info is not None:
        lines.append(f"  标题: {info.title or '(未命名)'}")
        if info.parent_id:
            lines.append(f"  父会话: {info.parent_id}")
        if info.provider or info.model:
            lines.append(f"  模型: {info.provider or '?'} / {info.model or '?'}")
    return _ok(*lines)


async def handle_sessions(ctx: CommandContext) -> CommandResult:
    if ctx.sessions_dir is None:
        return _ok("  （会话目录未知）")
    arguments = ctx.request.arguments.strip()
    if arguments.startswith("search"):
        query = arguments[len("search"):].strip()
        if not query:
            return _ok("  用法: /sessions search <关键词>")
        infos = search_sessions(ctx.sessions_dir, query)
        if not infos:
            return _ok(f"  （没有匹配 {query!r} 的会话）")
        return _ok(format_sessions(infos))
    if arguments:
        return _ok("  用法: /sessions | /sessions search <关键词>")
    return _ok(format_sessions(list_sessions(ctx.sessions_dir)))


async def handle_resume(ctx: CommandContext) -> CommandResult:
    target = ctx.request.arguments or "latest"
    path = resolve_resume_target(target, ctx.sessions_dir)
    if path is None:
        return _ok(f"  （找不到会话: {target}；用 /sessions 查看列表）")
    output: list[str] = []
    if ctx.session_lifecycle is not None:
        restored = ctx.session_lifecycle.resume(path, log=output.append)
    else:
        restored = restore_session(ctx.loop, path, log=output.append)
    return _ok(*output, clear_tool_outputs=restored)


async def handle_fork(ctx: CommandContext) -> CommandResult:
    if ctx.session_lifecycle is None:
        return _ok("  （会话分支不可用）")
    try:
        path = ctx.session_lifecycle.fork(ctx.request.arguments)
    except SessionLifecycleError as exc:
        return _ok(f"  [fork] {exc}")
    return _ok(f"  [fork] 已创建并切换到: {path}", clear_tool_outputs=True)


async def handle_rename(ctx: CommandContext) -> CommandResult:
    title = ctx.request.arguments.strip()
    if not title:
        return _ok("  用法: /rename <名称>")
    if ctx.session_lifecycle is None:
        return _ok("  （会话重命名不可用）")
    try:
        path = ctx.session_lifecycle.rename(title)
    except SessionLifecycleError as exc:
        return _ok(f"  [rename] {exc}")
    return _ok(f"  [rename] 已重命名: {title}", f"  会话文件: {path}")


async def handle_delete(ctx: CommandContext) -> CommandResult:
    if ctx.session_lifecycle is None:
        return _ok("  （会话删除不可用）")
    parts = ctx.request.arguments.split()
    force = "--force" in parts
    targets = [part for part in parts if part != "--force"]
    if len(targets) != 1:
        return _ok("  用法: /delete <n|path> [--force]")
    try:
        deleted = ctx.session_lifecycle.delete(targets[0], confirmed=force)
    except SessionConfirmationRequired:
        return _ok(
            f"  [delete] 将归档会话: {targets[0]}",
            f"  确认后执行: /delete {targets[0]} --force",
        )
    except SessionLifecycleError as exc:
        return _ok(f"  [delete] {exc}")
    return _ok(
        f"  [delete] 已归档: {deleted.source.name}",
        f"  可恢复位置: {deleted.archive_path}",
    )


async def handle_export(ctx: CommandContext) -> CommandResult:
    redactor = getattr(ctx.loop, "redactor", None)
    try:
        if ctx.request.arguments:
            path = Path(ctx.request.arguments)
            if path.suffix.lower() == ".json":
                out = export_native_session(
                    ctx.loop.history, path,
                    metadata=(ctx.session_lifecycle.metadata
                              if ctx.session_lifecycle is not None else None),
                    redactor=redactor)
            else:
                out = export_session(ctx.loop.history, path, redactor=redactor)
        else:
            out = export_session(
                ctx.loop.history,
                export_dir=ctx.workspace_root / ".krelcode" / "exports",
                redactor=redactor)
    except OSError as exc:
        return _ok(f"  [export] 导出失败: {exc}")
    return _ok(f"  [export] 已导出: {out}")


async def handle_reset(ctx: CommandContext) -> CommandResult:
    ctx.loop.reset()
    return _ok("（历史已清空）", clear_tool_outputs=True)
