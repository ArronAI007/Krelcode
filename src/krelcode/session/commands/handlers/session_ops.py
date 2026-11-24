"""会话、历史和导出类内置命令 handlers。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from ....agent.context.context_checkpoint_ports import CheckpointLoopPort
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
from ...rewind import RewindError
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
        restored = restore_session(
            cast(CheckpointLoopPort, ctx.loop), path, log=output.append)
    return _ok(*output, clear_tool_outputs=restored)


async def handle_fork(ctx: CommandContext) -> CommandResult:
    if ctx.session_lifecycle is None:
        return _ok("  （会话分支不可用）")
    arguments = ctx.request.arguments.strip()
    fork_parts = arguments.split(maxsplit=2)
    if fork_parts and fork_parts[0] == "--turn":
        if ctx.rewind is None:
            return _ok("  （历史 turn 分叉不可用）")
        parts = fork_parts
        if len(parts) < 2:
            return _ok("  用法: /fork --turn <n> [名称]")
        try:
            turn_index = int(parts[1])
            title = parts[2] if len(parts) > 2 else ""
        except ValueError:
            return _ok("  turn 编号必须是正整数")
        try:
            fork_result = ctx.rewind.fork_turn(turn_index, title)
        except RewindError as exc:
            return _ok(f"  [fork] {exc}")
        return _ok(
            f"  [fork] 已从 turn {fork_result.source_turn}"
            f" 创建并切换到: {fork_result.path}",
            "  仅分叉 history + plan；工作区文件保持当前状态",
            clear_tool_outputs=True,
        )
    try:
        path = ctx.session_lifecycle.fork(arguments)
    except SessionLifecycleError as exc:
        return _ok(f"  [fork] {exc}")
    return _ok(f"  [fork] 已创建并切换到: {path}", clear_tool_outputs=True)


async def handle_rewind(ctx: CommandContext) -> CommandResult:
    if ctx.rewind is None:
        return _ok("  （对话级 rewind 未启用）")
    arguments = ctx.request.arguments.strip()
    if not arguments or arguments == "list":
        return _ok(*ctx.rewind.timeline.render_lines())
    parts = arguments.split(maxsplit=2)
    if parts and parts[0] == "fork":
        if len(parts) < 2:
            return _ok("  用法: /rewind fork <n> [名称]")
        try:
            turn_index = int(parts[1])
            title = parts[2] if len(parts) > 2 else ""
        except ValueError:
            return _ok("  turn 编号必须是正整数")
        try:
            rewind_fork_result = ctx.rewind.fork_turn(turn_index, title)
        except RewindError as exc:
            return _ok(f"  [rewind fork] {exc}")
        return _ok(
            f"  [rewind fork] 已从 turn {rewind_fork_result.source_turn}"
            f" 创建并切换到: {rewind_fork_result.path}",
            "  仅分叉 history + plan；工作区文件保持当前状态",
            clear_tool_outputs=True,
        )
    try:
        turn_index = int(arguments)
        rewind_result = ctx.rewind.rewind(turn_index)
    except ValueError:
        return _ok(
            "  用法: /rewind [n|list] | /rewind fork <n> [名称]")
    except RewindError as exc:
        return _ok(f"  [rewind] {exc}")
    files = rewind_result.files
    detail = (
        f"恢复 {len(files.restored)} 个文件、删除 {len(files.deleted)} 个新建文件"
        if files.groups else "该范围没有可恢复的文件 checkpoint")
    return _ok(
        f"  [rewind] 已回到 turn {rewind_result.target_index} 开始前"
        f"（{rewind_result.label}）；移除 {rewind_result.removed_turns} 轮",
        f"  history + plan 已恢复；{detail}",
        "  注意：run_command 造成的文件修改不在 checkpoint 追踪范围",
        clear_tool_outputs=True,
    )


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
    if ctx.rewind is not None:
        ctx.rewind.timeline.clear()
    return _ok("（历史已清空）", clear_tool_outputs=True)
