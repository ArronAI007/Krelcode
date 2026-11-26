"""Build panel snapshots from session view state without terminal coupling."""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from ...session.state import SessionPhase, SessionViewState
from ..diff_review import DiffReview
from .model import Panel, PanelItem

_TASK_MARKS = {
    "pending": "○",
    "in_progress": "▶",
    "completed": "✓",
}


def _compact(value: object, limit: int = 96) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _task_items(state: SessionViewState) -> tuple[PanelItem, ...]:
    if state.plan_items:
        statuses = list(state.plan_statuses)
        if len(statuses) != len(state.plan_items):
            statuses = [
                "in_progress" if index == state.plan_active_index else "pending"
                for index in range(len(state.plan_items))
            ]
        return tuple(
            PanelItem(
                id=f"task-{index}",
                label=_compact(item, 80),
                status=_TASK_MARKS.get(status, "?"),
                detail=status,
            )
            for index, (item, status) in enumerate(
                zip(state.plan_items, statuses), 1)
        )
    if state.is_running:
        return (
            PanelItem(
                id="task-current",
                label=_compact(state.progress or "当前任务执行中"),
                status="▶",
            ),
        )
    return (
        PanelItem(
            id="task-empty",
            label="暂无结构化任务",
            detail="update_plan 生成计划后自动显示",
            status="○",
        ),
    )


def build_panels(
    state: SessionViewState,
    *,
    permission_mode: str,
    queue_size: int = 0,
    context_tokens: int = 0,
    context_budget: Optional[int] = None,
    fragment_count: int = 0,
    stable_fragments: int = 0,
    memory_entries: Iterable[object] = (),
    memory_total: Optional[int] = None,
    memory_tokens: int = 0,
    memory_pending: int = 0,
    memory_mode: str = "",
    quick_status: str = "",
    diff_review: Optional[DiffReview] = None,
    show_empty_review: bool = False,
) -> tuple[Panel, ...]:
    """Create the fixed panel snapshots shown above the input editor."""
    memories = tuple(memory_entries)
    total_memories = len(memories) if memory_total is None else memory_total
    phase = {
        SessionPhase.IDLE: "idle",
        SessionPhase.RUNNING: "running",
        SessionPhase.STOPPING: "stopping",
    }.get(state.phase, state.phase.value)
    tool_total = state.tool_calls_started
    tool_done = state.tool_calls_completed
    activity = [
        PanelItem(
            "activity-phase",
            f"phase · {phase}",
            state.query_phase,
            "●" if state.is_running else "○",
        ),
    ]
    if state.is_running:
        activity.append(PanelItem(
            "activity-step",
            f"step · {state.current_step or '-'}",
            _compact(state.progress),
            "▶",
        ))
    if state.active_tool_name:
        activity.append(PanelItem(
            "activity-tool",
            f"tool · {state.active_tool_name}",
            f"{tool_done} 已完成 · "
            f"{max(0, tool_total - tool_done)} 执行中",
            "▶",
        ))
    elif tool_total:
        activity.append(PanelItem(
            "activity-tools",
            f"tools · {tool_done} 已完成",
            f"输出 {state.tool_output_chars} 字符",
            "✓" if tool_done >= tool_total else "○",
        ))
    if queue_size:
        activity.append(PanelItem(
            "activity-queue", f"queue · {queue_size}", "等待当前任务完成", "○"))
    if quick_status:
        activity.append(PanelItem(
            "activity-shell", _compact(quick_status, 100), "", "▶"))

    snapshot = state.context_snapshot
    if snapshot is not None:
        context_tokens = snapshot.estimated_input_tokens
        context_budget = snapshot.budget_tokens or context_budget
        fragment_count = snapshot.fragment_count or fragment_count
        memory_tokens = snapshot.memory_tokens
    budget_text = (
        f"{context_tokens}/{context_budget} tokens"
        if context_budget else f"{context_tokens} tokens"
    )
    context_items: tuple[PanelItem, ...]
    if snapshot is None:
        context_items = (
            PanelItem("context-permission", f"permission · {permission_mode}"),
            PanelItem("context-window", f"context · {budget_text}"),
            PanelItem(
                "context-fragments",
                f"fragments · {fragment_count}",
                f"stable {stable_fragments}",
            ),
            PanelItem(
                "context-memory",
                f"memory · {total_memories}",
                f"hits {state.context_memory_hits}"
                f" · {memory_tokens} tokens",
            ),
        )
    else:
        window_detail = (
            f"window {snapshot.model_context_window}"
            f" · reserve {snapshot.output_reserve_tokens}"
            if snapshot.model_context_window else "window unknown"
        )
        window_detail += f" · wire {snapshot.wire_fidelity}"
        context_items = (
            PanelItem("context-permission", f"permission · {permission_mode}"),
            PanelItem(
                "context-window", f"context · {budget_text}", window_detail),
            PanelItem(
                "context-system",
                f"system · {snapshot.system_tokens}",
                f"workspace {snapshot.workspace_tokens}"
                f" · runtime {snapshot.runtime_tokens}",
            ),
            PanelItem(
                "context-history",
                f"history · {snapshot.history_tokens}",
                f"overhead {snapshot.overhead_tokens}",
            ),
            PanelItem(
                "context-cache",
                f"cache · {snapshot.stable_tokens}"
                f"/{snapshot.volatile_tokens}",
                f"blocks {snapshot.stable_system_block_count}"
                f"/{snapshot.dynamic_system_block_count}"
                f" · control {snapshot.cache_control_boundary} chars"
                f" · {snapshot.cache_mode} {snapshot.cache_segment_count}"
                f" · bp {snapshot.cache_breakpoint_count}"
                f"/{snapshot.cache_max_breakpoints}"
                f" · {snapshot.cache_requested_mode or 'unknown'}"
                f"{' fallback' if snapshot.cache_fallback else ''}"
                f" · hash {'changed' if state.context_cache_changed else 'same'}",
            ),
            PanelItem(
                "context-fragments",
                f"fragments · {fragment_count}",
                f"sources {snapshot.source_count}"
                f" · tools {snapshot.tool_count}"
                f" · schema {snapshot.tool_schema_tokens}",
            ),
            PanelItem(
                "context-memory",
                f"memory · {total_memories}",
                f"hits {state.context_memory_hits}"
                f" · {memory_tokens} tokens",
            ),
        )
    ledger = state.context_cache_ledger
    if isinstance(ledger, Mapping):
        diff = ledger.get("diff")
        reasons = (
            diff.get("reasons", ())
            if isinstance(diff, Mapping) else ()
        )
        complete_hash = str(ledger.get("complete_request_hash", "") or "")
        inheritance = str(ledger.get("instruction_inheritance", "") or "")
        detail = (
            f"complete {(complete_hash or 'unknown')[:12]}"
            f" · miss {', '.join(str(item) for item in reasons) or 'none'}"
        )
        if inheritance and inheritance != "not-applicable":
            detail += f" · inherit {inheritance}"
        context_items += (PanelItem(
            "context-ledger",
            (
                "ledger · "
                f"{str(ledger.get('wire_fidelity', '') or 'estimated')}"
            ),
            detail,
        ),)
    if state.context_dropped_items:
        dropped = state.context_dropped_items
        preview = ", ".join(dropped[:3])
        if len(dropped) > 3:
            preview += f" … +{len(dropped) - 3}"
        context_items += (PanelItem(
            "context-dropped",
            f"dropped · {len(dropped)}",
            _compact(preview, 96),
        ),)
    if state.microcompact_count:
        context_items += (PanelItem(
            "context-microcompact",
            f"microcompact · {state.microcompact_count}",
            f"saved {state.microcompact_saved_tokens} tokens",
        ),)
    if state.compact_generation:
        context_items += (PanelItem(
            "context-compact",
            f"compact · generation {state.compact_generation}",
            f"{state.compact_source} · saved {state.compact_saved_tokens} tokens"
            + (f" · watermarks {state.compact_high_watermark_tokens}/"
               f"{state.compact_low_watermark_tokens}"
               if state.compact_high_watermark_tokens else "")
            + (f" · {state.compact_checkpoint_id}"
               if state.compact_checkpoint_id else ""),
        ),)

    memory_items: list[PanelItem] = []
    for index, entry in enumerate(memories):
        text = getattr(entry, "text", str(entry))
        created = getattr(entry, "created_at", "")
        scope = getattr(entry, "scope", "project")
        scope_value = getattr(scope, "value", str(scope))
        memory_items.append(PanelItem(
            id=f"memory-{getattr(entry, 'id', index)}",
            label=_compact(text, 100),
            detail=_compact(f"{scope_value} · {created}", 32),
            status="•",
        ))
    pending_memories = max(0, int(memory_pending))
    if pending_memories:
        mode_detail = f" · mode {memory_mode}" if memory_mode else ""
        memory_items.append(PanelItem(
            id="memory-pending",
            label=f"Auto Memory · {pending_memories} 条待审核",
            detail=_compact(f"/memory review{mode_detail}", 64),
            status="◐",
        ))
    if not memory_items:
        memory_items.append(PanelItem(
            "memory-empty", "暂无记忆",
            "输入 #内容 添加", "○"))

    review_items: list[PanelItem] = []
    if diff_review is not None and diff_review.files:
        for index, item in enumerate(diff_review.files, 1):
            status = {
                "added": "+", "deleted": "-", "modified": "~",
                "renamed": "→", "binary": "■",
            }.get(item.status, "~")
            detail = f"+{item.additions} −{item.deletions}"
            if item.binary:
                detail += " · binary"
            if item.note:
                detail += f" · {_compact(item.note, 32)}"
            review_items.append(PanelItem(
                id=f"review-file-{index}",
                label=_compact(item.path, 100),
                detail=_compact(detail, 64),
                status=status,
            ))
        if diff_review.notice:
            review_items.insert(0, PanelItem(
                "review-note", _compact(diff_review.notice, 100),
                diff_review.stats, "ℹ"))
    else:
        review_items.append(PanelItem(
            "review-empty", "暂无待审阅差异",
            "文件写入审批或 /diff 时显示", "○"))

    panels = [
        Panel("tasks", "Tasks", _task_items(state)),
        Panel("activity", "Activity", tuple(activity)),
        Panel("context", "Context", context_items),
        Panel("review", "Review", tuple(review_items)),
        Panel("memory", "Memory", tuple(memory_items)),
    ]
    if (
        diff_review is None
        or (not diff_review.files and not diff_review.notice)
    ) and not show_empty_review:
        panels = [panel for panel in panels if panel.id != "review"]
    return tuple(panels)
