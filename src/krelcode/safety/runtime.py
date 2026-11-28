"""运行时权限模式与 Plan 审批协调器。

本模块只操作窄运行时对象，不依赖终端、命令注册表或 App 容器：
- ``RuntimePermissionManager`` 原子更新 ``ToolContext.runtime``；
- ``PlanApprovalCoordinator`` 保存最近计划快照，并把“批准”转换为下一轮执行 Prompt。

REPL 只负责把控制键/命令转发给这两个组件并渲染返回值。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from html import escape
from typing import Optional, Protocol, Sequence

from ..contracts.plan import (
    PlanApprovalStatus,
    PlanControl,
    PlanControlMode,
    PlanNodeStatus,
    PlanRole,
)
from ..contracts.plan_ops import (
    legacy_plan_control,
    parse_plan_nodes,
    plan_snapshot_views,
    update_plan_control,
)
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

    def invalidate_tool_pool(self) -> None: ...

    def sync_context_attachments(
        self,
        *,
        include_environment: bool = True,
        include_catalogs: bool = True,
        include_mcp: bool = True,
        include_contract: bool = True,
        include_reminders: bool = True,
    ) -> object: ...


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
        self.loop._turn_tool_context = replace(turn_context, runtime=replace(turn_context.runtime, permission_mode=mode))
        self.loop.invalidate_tool_pool()
        # Capability filtering is part of the same permission boundary.  Drop
        # the turn pool so the next model/tool step is rebuilt from the new
        # runtime snapshot instead of advertising a stale write capability.
        self.loop.sync_context_attachments(
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
    control: PlanControl = field(default_factory=PlanControl.empty)


class PlanApprovalCoordinator:
    """计划生成 → 待确认 → 批准执行/取消的会话级状态机。"""

    def __init__(self, permissions: RuntimePermissionManager) -> None:
        self.permissions = permissions
        self.snapshot = PlanSnapshot()
        self.status = PlanApprovalStatus.NONE
        self.control = PlanControl.empty()

    @property
    def pending(self) -> bool:
        return self.status is PlanApprovalStatus.PENDING

    def checkpoint(self) -> PlanCheckpoint:
        return PlanCheckpoint(
            snapshot=self.snapshot,
            status=self.status,
            permission_mode=self.permissions.mode,
            previous_permission_mode=self.permissions.previous_mode,
            control=self.control,
        )

    def restore(self, checkpoint: PlanCheckpoint) -> None:
        """恢复历史边界上的计划快照、审批状态与权限模式。"""
        self.permissions.restore_modes(
            checkpoint.permission_mode,
            checkpoint.previous_permission_mode,
        )
        self.snapshot = checkpoint.snapshot
        self.status = checkpoint.status
        self.control = checkpoint.control
        if not self.control.nodes and self.snapshot.items:
            self.control = legacy_plan_control(
                self.snapshot.items,
                self.snapshot.statuses,
                active_index=self.snapshot.active_index,
                explanation=self.snapshot.explanation,
                approval=self.status,
            )
        else:
            self.control = self.control.with_approval(self.status)
        self._sync_context()

    def begin_request(
        self,
        mode: PlanControlMode | str = PlanControlMode.TODO,
    ) -> None:
        """Start a fresh task plan while retaining the selected control mode."""
        self.control = PlanControl.empty(mode)
        self.snapshot = PlanSnapshot()
        self.status = PlanApprovalStatus.NONE
        self._sync_context()

    def start_request(self, request: str) -> str:
        text = str(request or "").strip()
        if not text:
            raise ValueError("Plan 请求不能为空")
        if self.permissions.mode is not PermissionMode.PLAN:
            self.permissions.set_mode(PermissionMode.PLAN)
        self.begin_request(PlanControlMode.PLAN_MODE)
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
        raw_nodes = [
            {"step": item, "status": status}
            for item, status in zip(normalized_items, normalized_statuses)
        ]
        mode = (
            self.control.mode
            if self.control.nodes
            else (
                PlanControlMode.PLAN_MODE
                if self.permissions.mode is PermissionMode.PLAN
                else PlanControlMode.TODO
            )
        )
        nodes = parse_plan_nodes(raw_nodes, previous=self.control)
        control = update_plan_control(
            self.control if self.control.nodes else None,
            nodes,
            mode=mode,
            trigger_fact=str(explanation or "event-derived plan update"),
        )
        self.record_control(control)

    def record_control(self, control: PlanControl) -> None:
        """Persist the authoritative structured plan control."""
        status = PlanApprovalStatus(control.approval)
        if control.nodes and self.permissions.mode is PermissionMode.PLAN:
            status = PlanApprovalStatus.PENDING
        elif (
            self.status is PlanApprovalStatus.PENDING
            and self.permissions.mode is not PermissionMode.PLAN
        ):
            status = PlanApprovalStatus.APPROVED
        self.control = control.with_approval(status)
        self.status = status
        self._sync_snapshot_from_control()
        self._sync_context()

    def edit_nodes(
        self,
        raw_items: Sequence[object],
        *,
        trigger_fact: str = "",
        trigger_observation_id: str = "",
    ) -> PlanControl:
        """Apply a human-authored structured edit while approval is pending."""
        if self.status is not PlanApprovalStatus.PENDING:
            raise ValueError("只有待确认计划可以结构化编辑")
        nodes = parse_plan_nodes(raw_items, previous=self.control)
        control = self.control.revise(
            nodes,
            mode=self.control.mode,
            approval=PlanApprovalStatus.PENDING,
            trigger_observation_id=trigger_observation_id,
            trigger_fact=trigger_fact or "user edited plan",
        )
        self.record_control(control)
        return self.control

    def approve(self) -> tuple[bool, str, Optional[str]]:
        if not self.control.nodes and not self.snapshot.items:
            return False, "当前没有可批准的执行计划", None
        if self.status is not PlanApprovalStatus.PENDING:
            return False, "当前计划不处于待确认状态", None
        self.status = PlanApprovalStatus.APPROVED
        self.control = self.control.with_approval(self.status)
        restored = self.permissions.restore_previous_mode()
        rendered = self._render_approval_nodes()
        prompt = (
            "<approved_plan>\n"
            "用户已批准以下执行计划：\n"
            f"{rendered}\n"
            f"计划修订: {self.control.revision.revision_id}\n"
        )
        if self.snapshot.explanation:
            prompt += (
                "批准时的说明："
                f"{escape(self.snapshot.explanation, quote=False)}\n"
            )
        prompt += (
            "\n请按计划开始执行。持续调用 update_plan 更新步骤状态；"
            "保留稳定 node id 与已完成项；如实际调查发现计划必须变更，"
            "先说明 trigger fact、影响项和证据缺口，再提交新修订。\n"
            "</approved_plan>"
        )
        self._sync_context()
        return True, f"计划已批准，恢复权限模式: {restored.value}", prompt

    def cancel(self) -> tuple[bool, str]:
        if not self.snapshot.items:
            return False, "当前没有可取消的执行计划"
        if self.status is not PlanApprovalStatus.PENDING:
            return False, "当前计划不处于待确认状态"
        self.status = PlanApprovalStatus.CANCELLED
        self.control = self.control.with_approval(self.status)
        restored = self.permissions.restore_previous_mode()
        self._sync_context()
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
            self.control = self.control.with_approval(self.status)
            self._sync_context()

    def render_lines(self) -> list[str]:
        if not self.control.nodes:
            return ["当前没有执行计划", "使用 /plan <任务描述> 生成计划"]
        marks = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
            "blocked": "[!]",
            "failed": "[✗]",
            "cancelled": "[-]",
        }
        statuses = [
            PlanNodeStatus(node.status).value for node in self.control.nodes
        ]
        completed = sum(status == "completed" for status in statuses)
        lines = [
            f"当前执行计划（{completed}/{len(statuses)} 完成）"
            f" · mode={PlanControlMode(self.control.mode).value}"
            f" · revision={self.control.revision.revision_id}"
        ]
        for index, node in enumerate(self.control.nodes, 1):
            status = PlanNodeStatus(node.status).value
            owner = f" · owner={node.owner}" if node.owner else ""
            depends = (
                f" · depends={','.join(node.depends_on)}"
                if node.depends_on else ""
            )
            group = (
                f" · group={node.parallel_group}"
                if node.parallel_group else ""
            )
            lines.append(
                f"  {marks.get(status, '[?]')} {index}. [{node.id}] "
                f"{node.objective}{owner}{depends}{group}"
            )
            if node.acceptance_checks:
                lines.append(
                    "     acceptance: " + "; ".join(node.acceptance_checks))
            if node.evidence_refs:
                lines.append(
                    "     evidence: " + "; ".join(node.evidence_refs))
        if self.snapshot.explanation:
            lines.append(f"  说明: {self.snapshot.explanation}")
        if self.status is PlanApprovalStatus.PENDING:
            lines.append(
                "  状态: 待确认 · /plan edit <JSON> 修改 · "
                "/plan approve 执行 · /plan cancel 取消")
        elif self.status is PlanApprovalStatus.APPROVED:
            lines.append("  状态: 已批准")
        elif self.status is PlanApprovalStatus.CANCELLED:
            lines.append("  状态: 已取消")
        return lines

    def _sync_snapshot_from_control(self) -> None:
        items, statuses, active_index, explanation = plan_snapshot_views(
            self.control)
        self.snapshot = PlanSnapshot(
            items=items,
            statuses=statuses,
            active_index=active_index,
            explanation=explanation,
        )

    def _sync_context(self) -> None:
        loop = self.permissions.loop
        loop.tool_context = replace(
            loop.tool_context,
            plan_control=self.control,
            plan_control_mode=self.control.mode,
        )
        loop._turn_tool_context = replace(
            loop._turn_tool_context,
            plan_control=self.control,
            plan_control_mode=self.control.mode,
        )

    def _render_approval_nodes(self) -> str:
        lines: list[str] = []
        for index, node in enumerate(self.control.nodes, 1):
            details = [f"owner={node.owner}"]
            if node.depends_on:
                details.append("depends_on=" + ",".join(node.depends_on))
            if node.parallel_group:
                details.append("parallel_group=" + node.parallel_group)
            lines.append(
                f"{index}. {escape(node.objective, quote=False)} "
                f"[id={node.id}] ({'; '.join(details)})"
            )
            if node.acceptance_checks:
                lines.append(
                    "   acceptance: "
                    + "; ".join(
                        escape(item, quote=False)
                        for item in node.acceptance_checks))
            if node.evidence_refs:
                lines.append(
                    "   evidence: "
                    + "; ".join(
                        escape(item, quote=False)
                        for item in node.evidence_refs))
        roles = {
            role.value: len(self.control.nodes_for_role(role))
            for role in PlanRole
        }
        present = ", ".join(
            f"{role}={count}" for role, count in roles.items() if count)
        if present:
            lines.append("roles: " + present)
        return "\n".join(lines)
