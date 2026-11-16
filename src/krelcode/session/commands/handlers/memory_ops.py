"""Slash-command handlers for scoped memory management."""

from __future__ import annotations

from ....session.memory import (
    SCOPE_LABELS,
    coerce_memory_scope,
    sync_memory_fragment,
)
from ..context import CommandContext
from ..types import CommandResult


def _ok(*output: str) -> CommandResult:
    return CommandResult(handled=True, output=tuple(output))


async def handle_memory(ctx: CommandContext) -> CommandResult:
    store = getattr(ctx.loop, "memory_store", None)
    if store is None:
        return _ok("  （Memory 未启用）")
    raw = ctx.request.arguments.strip()
    operation, _, rest = raw.partition(" ")
    operation = operation.lower() or "list"
    rest = rest.strip()

    if operation == "list":
        entries = store.list(limit=50)
        if not entries:
            return _ok(
                "  [memory] 当前没有记忆",
                "  #内容 添加项目记忆；#user: / #project: / #session: 指定作用域")
        lines = [f"  [memory] {len(entries)} 条（最新优先）"]
        for entry in reversed(entries):
            scope = coerce_memory_scope(entry.scope)
            lines.append(
                f"  {entry.id} · {scope.value}/{entry.kind_value}"
                f" · {entry.text}")
        return _ok(*lines)

    if operation == "scopes":
        summary = store.scope_summary()
        lines = ["  [memory scopes]"]
        for scope in ("user", "project", "session"):
            item = summary[scope]
            lines.append(
                f"  {SCOPE_LABELS[coerce_memory_scope(scope)]} ({scope})"
                f" · {item['entries']} 条 · {item['path']}")
        return _ok(*lines)

    if operation == "add":
        scope_name, separator, text = rest.partition(" ")
        if not separator or not text.strip():
            return _ok("  用法: /memory add <user|project|session> <text>")
        try:
            scope = coerce_memory_scope(scope_name)
            entry = store.add(text, scope=scope)
        except (OSError, ValueError) as exc:
            return _ok(f"  [memory] 写入失败: {exc}")
        sync_memory_fragment(ctx.loop, store)
        return _ok(
            f"  [memory] 已写入{SCOPE_LABELS[scope]}记忆 {entry.id}")

    if operation == "remove":
        entry_id = rest.split(maxsplit=1)[0] if rest else ""
        if not entry_id:
            return _ok("  用法: /memory remove <id>")
        try:
            removed = store.remove(entry_id)
        except OSError as exc:
            return _ok(f"  [memory] 删除失败: {exc}")
        if removed is None:
            return _ok(f"  [memory] 未找到: {entry_id}")
        sync_memory_fragment(ctx.loop, store)
        return _ok(f"  [memory] 已删除: {entry_id}")

    return _ok(
        "  用法: /memory [list|scopes|add <scope> <text>|remove <id>]")
