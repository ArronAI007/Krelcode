"""Slash-command handlers for scoped memory and Auto Memory review."""

from __future__ import annotations

from ....session.memory import (
    SCOPE_LABELS,
    coerce_memory_scope,
    sync_memory_fragment,
)
from ....session.memory_candidates import (
    TRIGGER_LABELS,
    AutoMemoryMode,
    MemoryCandidateStatus,
)
from ..context import CommandContext
from ..types import CommandResult


def _ok(*output: str) -> CommandResult:
    return CommandResult(handled=True, output=tuple(output))


def _resolve_candidate(queue, token: str):
    candidate = queue.get(token)
    if candidate is not None:
        return candidate
    matches = [item for item in queue.list(limit=0) if item.id.endswith(token)]
    return matches[-1] if len(matches) == 1 else None


def _candidate_lines(candidates) -> list[str]:
    lines = [f"  [memory review] {len(candidates)} 个待审核候选"]
    for item in reversed(candidates):
        trigger = TRIGGER_LABELS.get(item.trigger, item.trigger_value)
        conflict = (
            f" · 冲突 {len(item.conflict_ids)}" if item.conflict_ids else "")
        lines.append(
            f"  {item.id} · 置信度 {item.confidence:.2f} · {trigger}"
            f" · 证据 {len(item.evidence)}{conflict}")
        lines.append(f"    {item.text}")
    if candidates:
        lines.append(
            "  /memory review accept <id> 接受；/memory review reject <id> 拒绝")
    return lines


async def handle_memory(ctx: CommandContext) -> CommandResult:
    store = getattr(ctx.loop, "memory_store", None)
    if store is None:
        return _ok("  （Memory 未启用）")
    auto_memory = getattr(ctx.loop, "auto_memory", None)
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
            pinned = " · pinned" if entry.pinned else ""
            lines.append(
                f"  {entry.id} · {scope.value}/{entry.kind_value}"
                f"{pinned} · {entry.text}")
        if auto_memory is not None:
            pending = auto_memory.queue.pending_count()
            if pending:
                lines.append(f"  待审核 Auto Memory 候选: {pending}（/memory review）")
        return _ok(*lines)

    if operation == "scopes":
        summary = store.scope_summary()
        lines = ["  [memory scopes]"]
        for scope in ("user", "project", "session"):
            item = summary[scope]
            lines.append(
                f"  {SCOPE_LABELS[coerce_memory_scope(scope)]} ({scope})"
                f" · {item['entries']} 条 · {item['path']}")
        if auto_memory is not None:
            lines.append(
                f"  Auto Memory · mode={auto_memory.mode.value}"
                f" · pending={auto_memory.queue.pending_count()}"
                f" · queue={auto_memory.queue.path}")
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

    if operation in {"pin", "unpin"}:
        entry_id = rest.split(maxsplit=1)[0] if rest else ""
        if not entry_id:
            return _ok(f"  用法: /memory {operation} <id>")
        action = (
            auto_memory.pin if operation == "pin" and auto_memory is not None
            else auto_memory.unpin if auto_memory is not None
            else store.pin if operation == "pin" else store.unpin
        )
        try:
            entry = action(entry_id)
        except OSError as exc:
            return _ok(f"  [memory] 更新失败: {exc}")
        if entry is None:
            return _ok(f"  [memory] 未找到: {entry_id}")
        sync_memory_fragment(ctx.loop, store)
        state = "固定" if entry.pinned else "取消固定"
        return _ok(f"  [memory] 已{state}: {entry.id}")

    if operation == "search":
        query = rest.strip()
        if not query:
            return _ok("  用法: /memory search <关键词>")
        entries = store.search(query, limit=10)
        if not entries:
            return _ok(f"  [memory] 没有匹配: {query}")
        lines = [f"  [memory search] {len(entries)} 条"]
        for entry in entries:
            scope = coerce_memory_scope(entry.scope)
            pinned = " · pinned" if entry.pinned else ""
            lines.append(
                f"  {entry.id} · {scope.value}/{entry.kind_value}{pinned}"
                f" · {entry.text}")
        return _ok(*lines)

    if operation == "mode":
        if auto_memory is None:
            return _ok("  （Auto Memory 未启用）")
        if not rest:
            return _ok(
                f"  [memory] Auto Memory mode={auto_memory.mode.value}",
                "  可选: off | suggest | auto；默认 suggest，仅入队不自动写入",
                "  切换: /memory mode <off|suggest|auto>")
        try:
            mode = AutoMemoryMode(rest.split()[0].lower())
        except ValueError:
            return _ok("  用法: /memory mode <off|suggest|auto>")
        auto_memory.set_mode(mode)
        note = {
            AutoMemoryMode.OFF: "关闭候选提取",
            AutoMemoryMode.SUGGEST: "候选仅入队，等待 /memory review",
            AutoMemoryMode.AUTO: (
                f"置信度 ≥ {auto_memory.auto_threshold:.2f} 且无冲突时自动写入"),
        }[mode]
        return _ok(f"  [memory] Auto Memory mode={mode.value}（{note}）")

    if operation == "review":
        if auto_memory is None:
            return _ok("  （Auto Memory 未启用）")
        suboperation, _, review_rest = rest.partition(" ")
        suboperation = suboperation.lower() or "list"
        if suboperation in {"list", ""}:
            pending = auto_memory.queue.list(
                status=MemoryCandidateStatus.PENDING, limit=50)
            return _ok(*_candidate_lines(pending))
        if suboperation not in {"accept", "reject"}:
            return _ok(
                "  用法: /memory review [accept|reject <id>]")
        token = review_rest.strip().split(maxsplit=1)[0] if review_rest.strip() else ""
        if not token:
            return _ok(f"  用法: /memory review {suboperation} <id>")
        candidate = _resolve_candidate(auto_memory.queue, token)
        if candidate is None:
            return _ok(f"  [memory] 未找到候选: {token}")
        if suboperation == "accept":
            try:
                accepted = auto_memory.accept(candidate.id)
            except (OSError, ValueError) as exc:
                return _ok(f"  [memory] 接受失败: {exc}")
            if accepted is None:
                return _ok(f"  [memory] 未找到候选: {token}")
            sync_memory_fragment(ctx.loop, store)
            return _ok(
                f"  [memory] 已接受 {accepted.id}"
                f" → knowledge {accepted.existing_memory_id}")
        try:
            rejected = auto_memory.reject(candidate.id)
        except OSError as exc:
            return _ok(f"  [memory] 拒绝失败: {exc}")
        if rejected is None:
            return _ok(f"  [memory] 未找到候选: {token}")
        return _ok(f"  [memory] 已拒绝 {rejected.id}（不会写入长期记忆）")

    return _ok(
        "  用法: /memory [list|scopes|add|remove|pin|unpin|search|mode|review]")
