"""Build panel snapshots from session view state without terminal coupling."""

from __future__ import annotations

from typing import Iterable, Optional

from ...session.state import SessionPhase, SessionViewState
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
    memory_tokens: int = 0,
    quick_status: str = "",
) -> tuple[Panel, ...]:
    """Create the four default panels shown above the input editor."""
    memories = tuple(memory_entries)
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
            f"{tool_done}/{tool_total} 完成",
            "▶",
        ))
    elif tool_total:
        activity.append(PanelItem(
            "activity-tools",
            f"tools · {tool_done}/{tool_total}",
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
                f"memory · {len(memories)}",
                f"{memory_tokens} tokens",
            ),
        )
    else:
        window_detail = (
            f"window {snapshot.model_context_window}"
            f" · reserve {snapshot.output_reserve_tokens}"
            if snapshot.model_context_window else "window unknown"
        )
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
                f"boundary {snapshot.cache_boundary_chars} chars",
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
                f"memory · {len(memories)}",
                f"{memory_tokens} tokens",
            ),
        )

    memory_items: list[PanelItem] = []
    for index, entry in enumerate(memories):
        text = getattr(entry, "text", str(entry))
        created = getattr(entry, "created_at", "")
        memory_items.append(PanelItem(
            id=f"memory-{getattr(entry, 'id', index)}",
            label=_compact(text, 100),
            detail=_compact(created, 19),
            status="•",
        ))
    if not memory_items:
        memory_items.append(PanelItem(
            "memory-empty", "暂无项目记忆", "输入 #内容 添加", "○"))

    return (
        Panel("tasks", "Tasks", _task_items(state)),
        Panel("activity", "Activity", tuple(activity)),
        Panel("context", "Context", context_items),
        Panel("memory", "Memory", tuple(memory_items)),
    )
