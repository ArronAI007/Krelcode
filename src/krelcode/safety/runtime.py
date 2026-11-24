"""运行时权限模式与 Plan 审批协调器。

本模块只操作窄运行时对象，不依赖终端、命令注册表或 App 容器：
- ``RuntimePermissionManager`` 原子更新 ``ToolContext.runtime``；
- ``PlanApprovalCoordinator`` 保存最近计划快照，并把“批准”转换为下一轮执行 Prompt。

REPL 只负责把控制键/命令转发给这两个组件并渲染返回值。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from html import escape
from typing import Optional, Protocol, Sequence

from ..contracts.runtime import PermissionMode
from ..contracts.tool import ToolContext


MODE_CYCLE: tuple[PermissionMode, ...] = (
    PermissionMode.DEFAULT,
    PermissionMode.PLAN,
    PermissionMode.READ_ONLY,
    PermissionMode.AUTO_APPROVE,
)

MODE_LABELS = {
    PermissionMode.DEFAULT: "默认（按规则审批）",
    PermissionMode.PLAN: "Plan（只读调查并生成计划）",
    PermissionMode.READ_ONLY: "只读",
    PermissionMode.AUTO_APPROVE: "全自动批准",
}


def parse_permission_mode(raw: str) -> PermissionMode:
    """接受 CLI 常见拼写：read-only / read_only / readonly。"""
    normalized = str(raw or "").strip().lower().replace("_", "-")
    aliases = {
        "readonly": "read-only",
        "auto": "auto-approve",
        "autoapprove": "auto-approve",
    }
    return PermissionMode(aliases.get(normalized, normalized))


class RuntimeLoopPort(Protocol):
    """Minimal mutable loop state touched by permission mode changes."""

    tool_context: ToolContext
    _turn_tool_context: ToolContext
    _active_tool_pool: object | None


def _update_loop_state(loop: object, **values: object) -> None:
    """Use the explicit state boundary, with a lightweight host fallback."""
    update = getattr(loop, "update_runtime_state", None)
    if callable(update):
        update(**values)
        return
    for name, value in values.items():
        object.__setattr__(loop, name, value)


class RuntimePermissionManager:
    """权限模式的唯一写入口，确保下一轮工具池与当前轮策略同时更新。"""

    def __init__(self, loop: RuntimeLoopPort) -> None:
        self.loop = loop
        self._previous_mode = loop.tool_context.runtime.permission_mode

    @property
    def mode(self) -> PermissionMode:
        return self.loop.tool_context.runtime.permission_mode

    @property
    def previous_mode(self) -> PermissionMode:
        return self._previous_mode

    def set_mode(
        self,
        mode: PermissionMode | str,
        *,
        remember: bool = True,
    ) -> PermissionMode:
        target = (
            mode if isinstance(mode, PermissionMode)
            else parse_permission_mode(str(mode))
        )
        current = self.mode
        if target is current:
            return current
        if remember:
            self._previous_mode = current
        self._sync_mode(target)
        return current

    def cycle_mode(self, *, reverse: bool = False) -> PermissionMode:
        index = MODE_CYCLE.index(self.mode)
        step = -1 if reverse else 1
        target = MODE_CYCLE[(index + step) % len(MODE_CYCLE)]
        self.set_mode(target)
        return target

    def restore_previous_mode(self) -> PermissionMode:
        target = self._previous_mode
        if target is PermissionMode.PLAN:
            target = PermissionMode.DEFAULT
        self._sync_mode(target)
        return target

    def restore_modes(
        self,
        mode: PermissionMode,
        previous_mode: PermissionMode,
    ) -> None:
        """恢复一个历史边界的当前模式与 Plan 前模式（不改写规则仓库）。"""
        self._sync_mode(mode)
        self._previous_mode = previous_mode

    def _sync_mode(self, mode: PermissionMode) -> None:
        runtime = replace(
            self.loop.tool_context.runtime, permission_mode=mode)
        self.loop.tool_context.runtime = runtime
        # 本 turn 的工具池已经固化，但策略层仍应读取最新模式；否则运行中
        # Shift-Tab 会出现“界面已切换、实际审批仍沿用旧模式”的假象。
        turn_context = self.loop._turn_tool_context
        _update_loop_state(
            self.loop,
            _turn_tool_context=replace(
                turn_context,
                runtime=replace(
                    turn_context.runtime, permission_mode=mode),
            ),
        )
        # Capability filtering is part of the same permission boundary.  Drop
        # the turn pool so the next model/tool step is rebuilt from the new
        # runtime snapshot instead of advertising a stale write capability.
        _update_loop_state(self.loop, _active_tool_pool=None)
        sync = getattr(self.loop, "sync_context_attachments", None)
        if callable(sync):
            sync(
                include_environment=False,
                include_catalogs=False,
                include_mcp=False,
                include_contract=True,
                include_reminders=False,
            )

    def status_line(self) -> str:
        return f"权限: {self.mode.value} · {MODE_LABELS[self.mode]}"

    def mode_lines(self) -> list[str]:
        return [
            f"  {'*' if mode is self.mode else ' '} {mode.value}: {MODE_LABELS[mode]}"
            for mode in MODE_CYCLE
        ]


class PlanApprovalStatus(str, Enum):
    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PlanSnapshot:
    items: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    active_index: int = -1
    explanation: str = ""


@dataclass(frozen=True)
class PlanCheckpoint:
    """可随 turn 边界保存/恢复的计划与权限联合状态。"""

    snapshot: PlanSnapshot
    status: PlanApprovalStatus
    permission_mode: PermissionMode
    previous_permission_mode: PermissionMode


class PlanApprovalCoordinator:
    """计划生成 → 待确认 → 批准执行/取消的会话级状态机。"""

    def __init__(self, permissions: RuntimePermissionManager) -> None:
        self.permissions = permissions
        self.snapshot = PlanSnapshot()
        self.status = PlanApprovalStatus.NONE

    @property
    def pending(self) -> bool:
        return self.status is PlanApprovalStatus.PENDING

    def checkpoint(self) -> PlanCheckpoint:
        return PlanCheckpoint(
            snapshot=self.snapshot,
            status=self.status,
            permission_mode=self.permissions.mode,
            previous_permission_mode=self.permissions.previous_mode,
        )

    def restore(self, checkpoint: PlanCheckpoint) -> None:
        """恢复历史边界上的计划快照、审批状态与权限模式。"""
        self.permissions.restore_modes(
            checkpoint.permission_mode,
            checkpoint.previous_permission_mode,
        )
        self.snapshot = checkpoint.snapshot
        self.status = checkpoint.status

    def start_request(self, request: str) -> str:
        text = str(request or "").strip()
        if not text:
            raise ValueError("Plan 请求不能为空")
        if self.permissions.mode is not PermissionMode.PLAN:
            self.permissions.set_mode(PermissionMode.PLAN)
        self.snapshot = PlanSnapshot()
        self.status = PlanApprovalStatus.NONE
        return self.wrap_request(text)

    def wrap_request(self, request: str) -> str:
        """把进入 Plan 模式后的普通请求包装为只读计划任务。"""
        text = str(request or "").strip()
        if not text:
            return text
        if text.startswith("<plan_mode_request>"):
            return text
        return (
            "<plan_mode_request>\n"
            f"用户请求：{escape(text, quote=False)}\n\n"
            "请先只做只读调查，不要修改文件、运行有副作用的命令或调用写工具。"
            "使用 update_plan 提交完整执行计划，并在最终回复中解释关键取舍、风险和验证方式。"
            "此时只生成计划，等待用户确认，不要声称计划已经开始执行。\n"
            "</plan_mode_request>"
        )

    def record(
        self,
        items: Sequence[str],
        statuses: Sequence[str],
        active_index: int = -1,
        explanation: str = "",
    ) -> None:
        normalized_items = tuple(str(item) for item in items)
        if not normalized_items:
            return
        normalized_statuses = tuple(str(status) for status in statuses)
        if len(normalized_statuses) != len(normalized_items):
            normalized_statuses = tuple(
                "in_progress" if index == active_index else "pending"
                for index in range(len(normalized_items))
            )
        self.snapshot = PlanSnapshot(
            items=normalized_items,
            statuses=normalized_statuses,
            active_index=active_index,
            explanation=str(explanation or ""),
        )
        if self.permissions.mode is PermissionMode.PLAN:
            self.status = PlanApprovalStatus.PENDING
        elif self.status is PlanApprovalStatus.PENDING:
            # 离开 Plan 模式后收到的进度更新只是执行计划进度，不应重新要求审批。
            self.status = PlanApprovalStatus.APPROVED

    def approve(self) -> tuple[bool, str, Optional[str]]:
        if not self.snapshot.items:
            return False, "当前没有可批准的执行计划", None
        if self.status is not PlanApprovalStatus.PENDING:
            return False, "当前计划不处于待确认状态", None
        self.status = PlanApprovalStatus.APPROVED
        restored = self.permissions.restore_previous_mode()
        rendered = "\n".join(
            f"{index}. {escape(item, quote=False)}"
            for index, item in enumerate(self.snapshot.items, 1)
        )
        prompt = (
            "<approved_plan>\n"
            "用户已批准以下执行计划：\n"
            f"{rendered}\n"
        )
        if self.snapshot.explanation:
            prompt += (
                "批准时的说明："
                f"{escape(self.snapshot.explanation, quote=False)}\n"
            )
        prompt += (
            "\n请按计划开始执行。持续调用 update_plan 更新步骤状态；"
            "如实际调查发现计划必须变更，先说明原因并提交新计划。\n"
            "</approved_plan>"
        )
        return True, f"计划已批准，恢复权限模式: {restored.value}", prompt

    def cancel(self) -> tuple[bool, str]:
        if not self.snapshot.items:
            return False, "当前没有可取消的执行计划"
        if self.status is not PlanApprovalStatus.PENDING:
            return False, "当前计划不处于待确认状态"
        self.status = PlanApprovalStatus.CANCELLED
        restored = self.permissions.restore_previous_mode()
        return True, f"计划已取消，恢复权限模式: {restored.value}"

    def on_mode_change(
        self,
        previous: PermissionMode,
        current: PermissionMode,
    ) -> None:
        """用户在计划待确认时离开 Plan 模式，视为取消本次执行授权。"""
        if (
            previous is PermissionMode.PLAN
            and current is not PermissionMode.PLAN
            and self.status is PlanApprovalStatus.PENDING
        ):
            self.status = PlanApprovalStatus.CANCELLED

    def render_lines(self) -> list[str]:
        if not self.snapshot.items:
            return ["当前没有执行计划", "使用 /plan <任务描述> 生成计划"]
        marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        statuses = list(self.snapshot.statuses)
        completed = sum(status == "completed" for status in statuses)
        lines = [f"当前执行计划（{completed}/{len(statuses)} 完成）:"]
        for index, (item, status) in enumerate(
                zip(self.snapshot.items, statuses), 1):
            lines.append(f"  {marks.get(status, '[?]')} {index}. {item}")
        if self.snapshot.explanation:
            lines.append(f"  说明: {self.snapshot.explanation}")
        if self.status is PlanApprovalStatus.PENDING:
            lines.append("  状态: 待确认 · /plan approve 执行 · /plan cancel 取消")
        elif self.status is PlanApprovalStatus.APPROVED:
            lines.append("  状态: 已批准")
        elif self.status is PlanApprovalStatus.CANCELLED:
            lines.append("  状态: 已取消")
        return lines
