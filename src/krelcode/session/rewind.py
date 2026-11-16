"""对话级 rewind：turn 时间线、联合恢复与历史点分叉。

该模块不依赖终端或命令注册表。它以 EventBus 旁路订阅者身份记录每轮边界：
- history_before/history_after：只复制会话事实，不触碰执行内核；
- plan_before/plan_after：计划快照、审批状态与权限模式联合保存；
- checkpoint turn_id：由 CheckpointManager 按同一稳定 turn id 分组。

``RewindCoordinator`` 先完整校验并恢复文件检查点，再切换 history/plan。
失败时文件层自行回滚且会话保持不变；成功后 recorder 追加权威 history 快照。
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from ..core.events import (
    AgentEvent,
    EventBus,
    PlanUpdated,
    RunCancelled,
    RunFinished,
    RunStarted,
)
from ..core.messages import Message
from ..safety.runtime import (
    PlanApprovalStatus,
    PlanCheckpoint,
    PlanSnapshot,
)
from ..tools.base import PermissionMode
from .checkpoint import (
    CheckpointManager,
    CheckpointRestoreError,
    RestoreTurnsResult,
)


class RewindError(RuntimeError):
    """无法安全执行 rewind/fork。"""


@dataclass
class TurnEntry:
    turn_id: str
    session_id: str
    index: int
    label: str
    started_at: float
    history_before: Tuple[Message, ...]
    plan_before: PlanCheckpoint
    ended_at: Optional[float] = None
    history_after: Optional[Tuple[Message, ...]] = None
    plan_after: Optional[PlanCheckpoint] = None
    checkpoint_ids: Tuple[int, ...] = field(default_factory=tuple)
    cancelled: bool = False

    @property
    def complete(self) -> bool:
        return self.ended_at is not None and self.history_after is not None

    @property
    def status(self) -> str:
        if not self.complete:
            return "running"
        return "cancelled" if self.cancelled else "done"


class TurnTimeline:
    """当前进程会话的 turn 边界索引。"""

    def __init__(
        self,
        bus: EventBus,
        get_history: Callable[[], List[Message]],
        plan_manager,
        checkpoints: Optional[CheckpointManager] = None,
    ) -> None:
        self.bus = bus
        self._get_history = get_history
        self.plan_manager = plan_manager
        self.checkpoints = checkpoints
        self.entries: List[TurnEntry] = []
        self._active: Optional[TurnEntry] = None
        self._unsubscribe: Optional[Callable[[], None]] = None

    def attach(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self.bus.subscribe(self.on_event)

    def detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def clear(self) -> None:
        self.entries.clear()
        self._active = None

    def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            self._start(event)
        elif isinstance(event, PlanUpdated):
            self._record_plan(event)
        elif isinstance(event, RunFinished):
            self._finish(event.turn_id, cancelled=False)
        elif isinstance(event, RunCancelled):
            self._finish(event.turn_id, cancelled=True)

    def _start(self, event: RunStarted) -> None:
        if self._active is not None:
            # 防御异常订阅时序：没有终态的旧轮不应阻塞后续边界索引。
            self._active.ended_at = time.time()
            self._active.history_after = self._snapshot_history()
            self._active.plan_after = self.plan_manager.checkpoint()
            self._active.cancelled = True
        history = self._snapshot_history()
        before = history[:-1] if history else ()
        plan = self.plan_manager.checkpoint()
        entry = TurnEntry(
            turn_id=event.turn_id or f"turn-{len(self.entries) + 1}",
            session_id=event.session_id,
            index=len(self.entries) + 1,
            label=" ".join(event.user_input.split())[:80] or "(无输入)",
            started_at=time.time(),
            history_before=before,
            plan_before=plan,
            plan_after=plan,
        )
        self.entries.append(entry)
        self._active = entry

    def _record_plan(self, event: PlanUpdated) -> None:
        entry = self._active
        if entry is None:
            return
        if event.turn_id and event.turn_id != entry.turn_id:
            return
        current = self.plan_manager.checkpoint()
        status = current.status
        mode = current.permission_mode
        if mode is PermissionMode.PLAN:
            status = PlanApprovalStatus.PENDING
        elif status is PlanApprovalStatus.PENDING:
            status = PlanApprovalStatus.APPROVED
        entry.plan_after = PlanCheckpoint(
            snapshot=PlanSnapshot(
                items=tuple(event.items),
                statuses=tuple(event.statuses),
                active_index=event.active_index,
                explanation=event.explanation,
            ),
            status=status,
            permission_mode=mode,
            previous_permission_mode=current.previous_permission_mode,
        )

    def _finish(self, turn_id: str, *, cancelled: bool) -> None:
        entry = self._active
        if entry is None:
            return
        if turn_id and turn_id != entry.turn_id:
            return
        entry.ended_at = time.time()
        entry.history_after = self._snapshot_history()
        if entry.plan_after is None:
            entry.plan_after = entry.plan_before
        entry.cancelled = cancelled
        if self.checkpoints is not None:
            entry.checkpoint_ids = tuple(
                group.seq
                for group in self.checkpoints.groups_for_turn(
                    entry.turn_id, session_id=entry.session_id)
            )
        self._active = None

    def _snapshot_history(self) -> Tuple[Message, ...]:
        return tuple(copy.deepcopy(list(self._get_history())))

    def get(self, index: int) -> TurnEntry:
        if index < 1 or index > len(self.entries):
            raise RewindError(
                f"turn 编号超出范围: {index}（当前 1..{len(self.entries)}）")
        entry = self.entries[index - 1]
        if not entry.complete:
            raise RewindError(f"turn {index} 尚未完成，不能 rewind")
        return entry

    def entries_from(self, index: int) -> Tuple[TurnEntry, ...]:
        self.get(index)
        return tuple(self.entries[index - 1:])

    def truncate_before(self, index: int) -> None:
        """删除第 index 轮及之后的所有内存时间线记录。"""
        if index < 1 or index > len(self.entries) + 1:
            raise RewindError(f"turn 编号超出范围: {index}")
        del self.entries[index - 1:]
        self._active = None

    def render_lines(self) -> List[str]:
        if not self.entries:
            return ["当前没有可回退的 turn"]
        lines = ["可回退 turn（/rewind <n> 回到该轮开始前）:"]
        for entry in self.entries:
            checkpoint = (
                f" · {len(entry.checkpoint_ids)} checkpoint"
                if entry.checkpoint_ids else "")
            lines.append(
                f"  [{entry.index}] {entry.status} · {entry.label}{checkpoint}")
        return lines


@dataclass(frozen=True)
class RewindResult:
    target_index: int
    target_turn_id: str
    label: str
    removed_turns: int
    files: RestoreTurnsResult


@dataclass(frozen=True)
class TurnForkResult:
    source_turn: int
    source_turn_id: str
    label: str
    path: Path


class RewindCoordinator:
    """把 history、checkpoint 与 plan 的状态切换集中到一个事务边界。"""

    def __init__(
        self,
        *,
        runtime,
        loop,
        session_lifecycle,
        plan_manager,
        checkpoints: Optional[CheckpointManager] = None,
        compact_checkpoints=None,
        timeline: Optional[TurnTimeline] = None,
    ) -> None:
        self.runtime = runtime
        self.loop = loop
        self.session_lifecycle = session_lifecycle
        self.plan_manager = plan_manager
        self.checkpoints = checkpoints
        self.compact_checkpoints = compact_checkpoints
        self.timeline = timeline or TurnTimeline(
            loop.bus, lambda: self.loop.history, plan_manager, checkpoints)
        self.timeline.attach()
        # resume/fork 切换会话后，旧会话的 turn 边界不再有效。
        self.session_lifecycle.on_switch = self.timeline.clear

    def _ensure_idle(self) -> None:
        if bool(getattr(self.runtime, "is_running", False)):
            raise RewindError("当前有任务运行中；先 /stop 再 rewind")

    def rewind(self, turn_index: int) -> RewindResult:
        """回到第 N 轮开始前，移除 N..last 的 history/文件/plan 状态。"""
        self._ensure_idle()
        entry = self.timeline.get(turn_index)
        removed = self.timeline.entries_from(turn_index)
        try:
            files = (
                self.checkpoints.restore_turns(
                    [item.turn_id for item in removed],
                    session_id=entry.session_id,
                )
                if self.checkpoints is not None
                else RestoreTurnsResult(0, (), (), ())
            )
        except CheckpointRestoreError as exc:
            raise RewindError(f"文件检查点恢复失败；会话未改变: {exc}") from exc

        history_before = entry.history_before
        restored_compact = None
        if self.compact_checkpoints is not None:
            restored_compact = self.compact_checkpoints.find_matching_before(
                history_before,
                session_id=entry.session_id,
                created_at=entry.started_at,
            )
            if (
                restored_compact is not None
                and restored_compact.pre_compact_history
            ):
                history_before = restored_compact.pre_compact_history
        self.loop.history = self._history_with_current_system(history_before)
        if restored_compact is not None:
            self.loop.last_compact_checkpoint = restored_compact
        rebuild = getattr(self.loop, "rebuild_working_context", None)
        if callable(rebuild):
            rebuild()
        self.plan_manager.restore(entry.plan_before)
        self.timeline.truncate_before(turn_index)

        recorder = getattr(self.loop, "session_recorder", None)
        if recorder is not None:
            recorder.record_rewind(
                target_index=turn_index,
                target_turn_id=entry.turn_id,
                removed_turns=len(removed),
                restored=len(files.restored),
                deleted=len(files.deleted),
            )
        return RewindResult(
            target_index=turn_index,
            target_turn_id=entry.turn_id,
            label=entry.label,
            removed_turns=len(removed),
            files=files,
        )

    def fork_turn(self, turn_index: int, title: str = "") -> TurnForkResult:
        """从第 N 轮结束后的 history/plan 建立新会话；不恢复工作区文件。"""
        self._ensure_idle()
        entry = self.timeline.get(turn_index)
        if entry.history_after is None or entry.plan_after is None:
            raise RewindError(f"turn {turn_index} 的历史边界不完整")
        fork_title = title.strip() or (
            f"Fork · turn {turn_index} · {entry.label[:40]}")
        try:
            path = self.session_lifecycle.fork(
                fork_title,
                history=self._history_with_current_system(entry.history_after),
            )
        except Exception as exc:
            raise RewindError(f"历史分叉失败: {exc}") from exc
        self.plan_manager.restore(entry.plan_after)
        return TurnForkResult(
            source_turn=turn_index,
            source_turn_id=entry.turn_id,
            label=entry.label,
            path=path,
        )

    def _history_with_current_system(
        self,
        history: Tuple[Message, ...],
    ) -> List[Message]:
        """回退对话消息，但保留当前工作区 system prompt/技能索引。"""
        restored = copy.deepcopy(list(history))
        current = self.loop.history
        if not current or current[0].role != "system":
            return restored
        system = copy.deepcopy(current[0])
        if restored and restored[0].role == "system":
            restored[0] = system
        else:
            restored.insert(0, system)
        return restored
