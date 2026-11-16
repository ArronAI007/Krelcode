"""子代理 sidechain JSONL 持久化。

Sidechain 只属于子代理，不自动注入父 Agent history。它保存初始定义、用户
消息、feed、状态转换、history 快照和最终结果，供进程内查询或重启后审计。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core.messages import Message
from ..core.redact import SecretRedactor
from ..session.codec import history_to_dicts
from .feed import AgentFeedEntry

SIDECHAIN_DIRNAME = Path(".krelcode") / "sidechains"


@dataclass(frozen=True)
class SidechainInfo:
    path: Path
    run_id: str
    agent_id: str
    task: str = ""
    definition: str = ""
    status: str = ""
    updated_at: float = 0.0
    records: int = 0


class AgentSidechainWriter:
    def __init__(
        self,
        path: Path,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.error: str = ""
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8")

    def start(self, metadata: Dict[str, Any]) -> None:
        self.write({"type": "sidechain_start", **metadata})

    def append_feed(self, entry: AgentFeedEntry) -> None:
        self.write({
            "type": "feed",
            "ts": entry.ts,
            "sequence": entry.sequence,
            "kind": entry.kind,
            "text": entry.text,
            "data": entry.data,
        })

    def append_state(
        self,
        status: str,
        *,
        reason: str = "",
        turns: int = 0,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        record: Dict[str, Any] = {
            "type": "state",
            "ts": time.time(),
            "status": status,
            "reason": reason,
            "turns": turns,
        }
        if result is not None:
            record["result"] = result
        if error is not None:
            record["error"] = error
        self.write(record)

    def append_message(self, role: str, content: str, *, source: str = "") -> None:
        self.write({
            "type": "message",
            "ts": time.time(),
            "role": role,
            "content": content,
            "source": source,
        })

    def append_history(self, history: Iterable[Message], *, turn: int = 0) -> None:
        self.write({
            "type": "history",
            "ts": time.time(),
            "turn": int(turn),
            "messages": history_to_dicts(list(history)),
        })

    def write(self, record: Dict[str, Any]) -> None:
        if self._handle.closed:
            return
        safe = _redact_record(record, self.redactor)
        line = json.dumps(safe, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                self._handle.write(line)
                self._handle.flush()
        except OSError as exc:
            self.error = f"sidechain 写入失败: {exc}"

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                try:
                    self._handle.flush()
                finally:
                    self._handle.close()


class SidechainStore:
    def __init__(
        self,
        workspace_root: Path,
        *,
        redactor: Optional[SecretRedactor] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_id = run_id or f"{stamp}-{uuid.uuid4().hex[:8]}"
        self.directory = self.workspace_root / SIDECHAIN_DIRNAME / self.run_id
        self.redactor = redactor

    def create(self, agent_id: str, metadata: Dict[str, Any]) -> AgentSidechainWriter:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in agent_id)
        writer = AgentSidechainWriter(
            self.directory / f"{safe_id}.jsonl", redactor=self.redactor)
        writer.start({
            "run_id": self.run_id,
            "agent_id": agent_id,
            "started_at": time.time(),
            **metadata,
        })
        return writer

    def list(self, *, limit: int = 100) -> List[SidechainInfo]:
        root = self.workspace_root / SIDECHAIN_DIRNAME
        if not root.is_dir():
            return []
        infos: List[SidechainInfo] = []
        paths = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths[:max(1, int(limit))]:
            try:
                info = read_sidechain_info(path)
            except OSError:
                continue
            infos.append(info)
        return infos

    def resolve(self, value: str) -> Optional[Path]:
        raw = str(value or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        direct = self.directory / f"{raw}.jsonl"
        if direct.is_file():
            return direct
        root = self.workspace_root / SIDECHAIN_DIRNAME
        matches = sorted(root.glob(f"*/{raw}.jsonl")) if root.is_dir() else []
        return matches[-1] if matches else None


def read_sidechain(path: Path, *, max_records: int = 5_000) -> Tuple[Dict[str, Any], ...]:
    """读取 sidechain；超大文件保留首条 start 和最近记录，便于看到最终状态。"""
    limit = max(1, int(max_records))
    tail: deque[Dict[str, Any]] = deque(maxlen=limit)
    first: Optional[Dict[str, Any]] = None
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if first is None:
                first = item
            tail.append(item)
    if first is None:
        return ()
    records = list(tail)
    if records and records[0] is not first:
        records = [first] + records[-(limit - 1):] if limit > 1 else [first]
    return tuple(records)


def read_sidechain_info(path: Path) -> SidechainInfo:
    start: Dict[str, Any] = {}
    last_status = ""
    updated_at = 0.0
    count = 0
    for record in read_sidechain(path):
        count += 1
        updated_at = max(updated_at, float(record.get("ts") or 0.0))
        if record.get("type") == "sidechain_start":
            start = record
        elif record.get("type") == "state":
            last_status = str(record.get("status") or last_status)
    return SidechainInfo(
        path=Path(path),
        run_id=str(start.get("run_id") or Path(path).parent.name),
        agent_id=str(start.get("agent_id") or Path(path).stem),
        task=str(start.get("task") or ""),
        definition=str(start.get("definition") or ""),
        status=last_status,
        updated_at=updated_at,
        records=count,
    )


def _redact_record(value: Any, redactor: Optional[SecretRedactor]) -> Any:
    if redactor is None:
        return value
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, list):
        return [_redact_record(item, redactor) for item in value]
    if isinstance(value, tuple):
        return [_redact_record(item, redactor) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_record(item, redactor)
                for key, item in value.items()}
    return value
