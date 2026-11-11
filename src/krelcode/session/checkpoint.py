"""检查点与回滚：每轮对话的文件快照安全网（/undo）。

设计：
- CheckpointManager 是事件总线的旁路订阅者（内核与工具层零改动）：
  · RunStarted      → 开启一个检查组（label = 用户输入预览）
  · ToolCallStarted → write_file/edit_file 的目标文件在首次修改前快照
  · RunFinished     → 关组；空组（本轮没改文件）直接丢弃
- 快照落盘 <workspace>/.krelcode/checkpoints/NNNN-<ts>/：
  meta.json（label/时间/文件清单）+ files/ 下的原始字节备份
- undo_last()：LIFO 弹出最近的已关闭组，逐文件恢复（改过的还原、
  本轮新建的删除），并删除该组目录
- 跨重启：构造时扫描 checkpoints 目录重建组栈（/undo 在新会话里依然可用）

已知边界：run_command 对文件的修改无法追踪（命令内容不可静态分析）。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..core.events import (
    AgentEvent, EventBus, RunFinished, RunStarted, ToolCallStarted,
)

TRACKED_WRITE_TOOLS = {"write_file", "edit_file"}
LABEL_MAX_CHARS = 60
CHECKPOINT_DIRNAME = Path(".krelcode") / "checkpoints"


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


@dataclass(frozen=True)
class UndoResult:
    label: str
    restored: List[str]   # 还原为原内容的文件
    deleted: List[str]    # 删除的本轮新建文件


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
            self.begin_group(event.user_input)
        elif isinstance(event, ToolCallStarted):
            if event.tool_call.name in TRACKED_WRITE_TOOLS:
                raw_path = event.tool_call.arguments.get("path")
                if isinstance(raw_path, str):
                    self.snapshot(raw_path)
        elif isinstance(event, RunFinished):
            self.close_group()

    # -- 组生命周期 ---------------------------------------------------------------------

    def begin_group(self, label: str = "") -> CheckpointGroup:
        if self._current is not None:
            self.close_group()  # 防御：上一组未关（如异常中断）
        seq = (self._groups[-1].seq + 1) if self._groups else 1
        stamp = time.strftime("%Y%m%d-%H%M%S")
        group = CheckpointGroup(
            seq=seq,
            dir=self.dir / f"{seq:04d}-{stamp}",
            label=label[:LABEL_MAX_CHARS],
            ts=time.time(),
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
            # 空组丢弃（本轮没有文件改动）
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
        self._groups.remove(group)

        restored: List[str] = []
        deleted: List[str] = []
        for record in reversed(group.files):
            target = self.root / record.rel
            if record.existed:
                backup = group.dir / "files" / record.backup
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(backup.read_bytes())
                    restored.append(record.rel)
                except OSError:
                    pass  # 单文件失败不阻断其余恢复
            else:
                try:
                    target.unlink(missing_ok=True)
                    deleted.append(record.rel)
                except OSError:
                    pass
        shutil.rmtree(group.dir, ignore_errors=True)
        return UndoResult(label=group.label, restored=restored, deleted=deleted)

    def list_groups(self) -> List[str]:
        return [f"[{g.seq}] {g.label or '(无标签)'} · {len(g.files)} 个文件"
                for g in self._groups if g.closed]

    # -- 持久化 -----------------------------------------------------------------------------

    def _write_meta(self, group: CheckpointGroup) -> None:
        meta = {
            "seq": group.seq,
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
                ))
            except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue  # 损坏的组跳过
