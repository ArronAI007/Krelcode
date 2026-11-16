"""检查点与回滚：每轮对话的文件快照安全网。

设计：
- CheckpointManager 是事件总线的旁路订阅者（内核与工具层零改动）：
  · RunStarted      → 开启一个检查组（label = 用户输入预览，turn_id 关联）
  · ToolCallStarted → write_file/edit_file 的目标文件在首次修改前快照
  · RunFinished/RunCancelled → 关组；空组（本轮没改文件）直接丢弃
- 快照落盘 <workspace>/.krelcode/checkpoints/NNNN-<ts>/：
  meta.json（turn_id/label/时间/文件清单）+ files/ 下的原始字节备份
- undo_last()：回退最近一组；restore_turns()：按 turn 边界批量逆序恢复
- 跨重启：构造时扫描 checkpoints 目录重建组栈；meta 中的 turn_id 一并恢复

已知边界：run_command 对文件的修改无法追踪（命令内容不可静态分析）。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.events import (
    AgentEvent,
    EventBus,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallStarted,
)

TRACKED_WRITE_TOOLS = {"write_file", "edit_file"}
LABEL_MAX_CHARS = 60
CHECKPOINT_DIRNAME = Path(".krelcode") / "checkpoints"


class CheckpointRestoreError(RuntimeError):
    """检查点无法安全完整恢复；调用方应在修改会话前终止 rewind。"""


@dataclass
class FileRecord:
    rel: str            # 相对工作区的路径
    existed: bool       # 快照时文件是否已存在（False=本轮新建，undo 应删除）
    backup: str         # 备份文件名（existed=False 时无备份）


@dataclass
class CheckpointGroup:
    seq: int
    dir: Path
    label: str
    ts: float
    files: List[FileRecord] = field(default_factory=list)
    closed: bool = False
    turn_id: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class UndoResult:
    label: str
    restored: List[str]   # 还原为原内容的文件
    deleted: List[str]    # 删除的本轮新建文件


@dataclass(frozen=True)
class RestoreTurnsResult:
    groups: int
    labels: Tuple[str, ...]
    restored: Tuple[str, ...]
    deleted: Tuple[str, ...]


@dataclass(frozen=True)
class _RestoreOperation:
    record: FileRecord
    target: Path
    content: Optional[bytes]  # None 表示恢复后该路径应不存在


class CheckpointManager:
    def __init__(self, workspace_root: Path) -> None:
        self.root = Path(workspace_root).resolve()
        self.dir = self.root / CHECKPOINT_DIRNAME
        self._groups: List[CheckpointGroup] = []
        self._current: Optional[CheckpointGroup] = None
        self._load_existing()

    # -- 事件接线 ----------------------------------------------------------------------

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(self.on_event)

    def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            self.begin_group(
                event.user_input,
                turn_id=event.turn_id,
                session_id=event.session_id,
            )
        elif isinstance(event, ToolCallStarted):
            if event.tool_call.name in TRACKED_WRITE_TOOLS:
                raw_path = event.tool_call.arguments.get("path")
                if isinstance(raw_path, str):
                    self.snapshot(raw_path)
        elif isinstance(event, (RunFinished, RunCancelled)):
            self.close_group()

    # -- 组生命周期 ---------------------------------------------------------------------

    def begin_group(
        self,
        label: str = "",
        *,
        turn_id: str = "",
        session_id: str = "",
    ) -> CheckpointGroup:
        if self._current is not None:
            self.close_group()  # 防御：上一组未关（如异常中断）
        seq = (self._groups[-1].seq + 1) if self._groups else 1
        stamp = time.strftime("%Y%m%d-%H%M%S")
        group = CheckpointGroup(
            seq=seq,
            dir=self.dir / f"{seq:04d}-{stamp}",
            label=label[:LABEL_MAX_CHARS],
            ts=time.time(),
            turn_id=turn_id,
            session_id=session_id,
        )
        self._groups.append(group)
        self._current = group
        return group

    def close_group(self) -> None:
        group = self._current
        if group is None:
            return
        self._current = None
        if not group.files:
            # 空组丢弃（本轮没有文件改动）；turn timeline 仍保留纯聊天轮。
            self._groups.remove(group)
            if group.dir.exists():
                shutil.rmtree(group.dir, ignore_errors=True)
            return
        group.closed = True
        self._write_meta(group)

    # -- 快照 ----------------------------------------------------------------------------

    def snapshot(self, raw_path: str) -> Optional[FileRecord]:
        """在当前组里快照一个文件（同组同文件只记第一次，保最原始版本）。"""
        group = self._current
        if group is None:
            return None
        target = self._resolve(raw_path)
        if target is None:
            return None  # 越界/非法路径：工具层自会拒绝，这里静默跳过
        rel = str(target.relative_to(self.root))
        if any(f.rel == rel for f in group.files):
            return None

        existed = target.is_file()
        backup_name = ""
        if existed:
            group.dir.joinpath("files").mkdir(parents=True, exist_ok=True)
            backup_name = f"{len(group.files):03d}-{target.name}"
            try:
                group.dir.joinpath("files", backup_name).write_bytes(target.read_bytes())
            except OSError:
                return None  # 备份失败则不记录（宁可不快照，不做半套）
        record = FileRecord(rel=rel, existed=existed, backup=backup_name)
        group.files.append(record)
        return record

    def _resolve(self, raw_path: str) -> Optional[Path]:
        """与工具层同规则：相对路径基于工作区，解析后必须仍在工作区内。"""
        try:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = self.root / candidate
            resolved = candidate.resolve()
        except (OSError, ValueError):
            return None
        if resolved != self.root and self.root not in resolved.parents:
            return None
        return resolved

    # -- 回滚 ----------------------------------------------------------------------------

    def undo_last(self) -> Optional[UndoResult]:
        """弹出最近的已关闭组并恢复文件；无可回滚返回 None。"""
        closed = [g for g in self._groups if g.closed]
        if not closed:
            return None
        group = closed[-1]
        result = self._restore_groups([group], remove=True)
        return UndoResult(
            label=group.label,
            restored=list(result.restored),
            deleted=list(result.deleted),
        )

    def restore_turns(
        self,
        turn_ids: Sequence[str],
        *,
        session_id: str = "",
    ) -> RestoreTurnsResult:
        """恢复指定 turn 及其后所有文件检查点。

        调用方按“回到 turn N 开始前”传入 N..last 的 turn id。恢复严格按
        checkpoint seq 逆序执行，因此同一文件跨多轮修改会逐步退回边界版本。
        任一备份缺失/越界时先失败，不修改工作区。
        """
        wanted = {turn_id for turn_id in turn_ids if turn_id}
        if not wanted:
            return RestoreTurnsResult(0, (), (), ())
        groups = [
            g for g in self._groups
            if g.turn_id in wanted and g.session_id == session_id
        ]
        if not groups:
            return RestoreTurnsResult(0, (), (), ())
        if any(not group.closed for group in groups):
            raise CheckpointRestoreError("存在尚未关闭的检查点，拒绝回退")
        result = self._restore_groups(groups, remove=True)
        labels = tuple(group.label for group in sorted(groups, key=lambda g: g.seq))
        return RestoreTurnsResult(
            groups=len(groups),
            labels=labels,
            restored=result.restored,
            deleted=result.deleted,
        )

    def groups_for_turn(
        self,
        turn_id: str,
        *,
        session_id: str = "",
    ) -> List[CheckpointGroup]:
        if not turn_id:
            return []
        return [
            g for g in self._groups
            if g.turn_id == turn_id and g.session_id == session_id
        ]

    def _restore_groups(
        self,
        groups: Iterable[CheckpointGroup],
        *,
        remove: bool,
    ) -> RestoreTurnsResult:
        selected = sorted(groups, key=lambda g: g.seq, reverse=True)
        operations = self._prepare_operations(selected)
        originals = self._capture_originals(operations)
        restored: List[str] = []
        deleted: List[str] = []
        try:
            for operation in operations:
                if operation.content is None:
                    operation.target.unlink(missing_ok=True)
                    deleted.append(operation.record.rel)
                else:
                    operation.target.parent.mkdir(parents=True, exist_ok=True)
                    operation.target.write_bytes(operation.content)
                    restored.append(operation.record.rel)
        except OSError as exc:
            self._rollback_originals(originals)
            raise CheckpointRestoreError(f"文件恢复失败，已尝试回滚: {exc}") from exc

        if remove:
            selected_set = {id(group) for group in selected}
            self._groups[:] = [
                group for group in self._groups if id(group) not in selected_set]
            for group in selected:
                shutil.rmtree(group.dir, ignore_errors=True)
        labels = tuple(group.label for group in reversed(selected))
        return RestoreTurnsResult(
            groups=len(selected),
            labels=labels,
            restored=tuple(restored),
            deleted=tuple(deleted),
        )

    def _prepare_operations(
        self, groups: Sequence[CheckpointGroup],
    ) -> List[_RestoreOperation]:
        """预检全部路径和备份，再开始写；坏 meta 不得造成半回退。"""
        operations: List[_RestoreOperation] = []
        for group in groups:
            group_root = group.dir.resolve()
            for record in reversed(group.files):
                target = self._resolve(record.rel)
                if target is None:
                    raise CheckpointRestoreError(
                        f"检查点路径越界: {record.rel!r}")
                if target.exists() and not target.is_file():
                    raise CheckpointRestoreError(
                        f"恢复目标不是普通文件: {record.rel}")
                content: Optional[bytes] = None
                if record.existed:
                    if not record.backup:
                        raise CheckpointRestoreError(
                            f"检查点缺少备份名: {record.rel}")
                    backup = (group.dir / "files" / record.backup).resolve()
                    if group_root not in backup.parents or not backup.is_file():
                        raise CheckpointRestoreError(
                            f"检查点备份缺失或越界: {record.rel}")
                    try:
                        content = backup.read_bytes()
                    except OSError as exc:
                        raise CheckpointRestoreError(
                            f"无法读取检查点备份: {record.rel}: {exc}") from exc
                operations.append(_RestoreOperation(
                    record=record,
                    target=target,
                    content=content,
                ))
        return operations

    def _capture_originals(
        self, operations: Sequence[_RestoreOperation],
    ) -> Dict[Path, Optional[bytes]]:
        originals: Dict[Path, Optional[bytes]] = {}
        for operation in operations:
            if operation.target in originals:
                continue
            try:
                originals[operation.target] = (
                    operation.target.read_bytes()
                    if operation.target.is_file() else None)
            except OSError as exc:
                raise CheckpointRestoreError(
                    f"无法为回退创建安全快照: {operation.record.rel}: {exc}"
                ) from exc
        return originals

    @staticmethod
    def _rollback_originals(originals: Dict[Path, Optional[bytes]]) -> None:
        for target, content in originals.items():
            try:
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            except OSError:
                pass

    def list_groups(self) -> List[str]:
        return [f"[{g.seq}] {g.label or '(无标签)'} · {len(g.files)} 个文件"
                for g in self._groups if g.closed]

    # -- 持久化 -----------------------------------------------------------------------------

    def _write_meta(self, group: CheckpointGroup) -> None:
        meta = {
            "seq": group.seq,
            "turn_id": group.turn_id,
            "session_id": group.session_id,
            "label": group.label,
            "ts": group.ts,
            "files": [
                {"rel": f.rel, "existed": f.existed, "backup": f.backup}
                for f in group.files
            ],
        }
        try:
            group.dir.mkdir(parents=True, exist_ok=True)
            (group.dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _load_existing(self) -> None:
        """重启后从磁盘重建组栈（按目录名排序 = 时间序）。"""
        if not self.dir.is_dir():
            return
        for group_dir in sorted(self.dir.iterdir()):
            meta_path = group_dir / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                files = [
                    FileRecord(rel=f["rel"], existed=bool(f["existed"]),
                               backup=f.get("backup", ""))
                    for f in meta.get("files", [])
                ]
                if not files:
                    continue
                self._groups.append(CheckpointGroup(
                    seq=int(meta.get("seq", len(self._groups) + 1)),
                    dir=group_dir,
                    label=str(meta.get("label", "")),
                    ts=float(meta.get("ts", 0.0)),
                    files=files,
                    closed=True,
                    turn_id=str(meta.get("turn_id", "")),
                    session_id=str(meta.get("session_id", "")),
                ))
            except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue  # 损坏的组跳过
