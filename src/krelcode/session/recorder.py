"""会话持久化：EventBus 的旁路订阅者（内核对其零感知）。

JSONL 格式，两类记录：
- 审计事件流：user_input / tool_call / tool_result / permission_denied /
  context_compressed / run_finished（每事件一行，便于 grep 与回放）
- history 快照：每次 RunFinished 落一行完整对话历史（--resume 用最后一条恢复）

文件：<workspace>/.krelcode/sessions/YYYYmmdd-HHMMSS-<pid>.jsonl
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List, Optional

from ..core.events import (
    AgentEvent,
    ContextCompressed,
    EventBus,
    PermissionDenied,
    RunFinished,
    RunStarted,
    SubagentFinished,
    SubagentSpawned,
    ToolCallCompleted,
    ToolCallStarted,
)
from ..core.messages import Message
from .codec import history_to_dicts, history_from_dicts


class SessionRecorder:
    def __init__(self, path: Path, get_history) -> None:
        """get_history: 无参可调用，返回当前 List[Message]（避免循环依赖注入 loop）。"""
        self.path = Path(path)
        self._get_history = get_history
        self._unsub: Optional[object] = None
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def attach(self, bus: EventBus) -> None:
        self._unsub = bus.subscribe(self.on_event)

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()  # type: ignore[operator]
            self._unsub = None

    async def on_event(self, event: AgentEvent) -> None:
        record = self._event_record(event)
        if record is None:
            return
        self._write(record)
        if isinstance(event, RunFinished):
            # 每轮结束落一份完整历史快照（resume 取最后一条）
            self._write({"type": "history", "ts": _now(),
                         "messages": history_to_dicts(self._get_history())})

    def _event_record(self, event: AgentEvent) -> Optional[dict]:
        ts = _now()
        if isinstance(event, RunStarted):
            return {"type": "user_input", "ts": ts, "content": event.user_input}
        if isinstance(event, ToolCallStarted):
            return {"type": "tool_call", "ts": ts, "id": event.tool_call.id,
                    "name": event.tool_call.name, "arguments": event.tool_call.arguments}
        if isinstance(event, ToolCallCompleted):
            content = event.content
            if len(content) > 500:
                content = content[:500] + "…"
            return {"type": "tool_result", "ts": ts, "id": event.tool_call.id,
                    "is_error": event.is_error, "content": content}
        if isinstance(event, PermissionDenied):
            return {"type": "permission_denied", "ts": ts,
                    "name": event.tool_call.name, "reason": event.reason}
        if isinstance(event, SubagentSpawned):
            return {"type": "subagent_spawned", "ts": ts,
                    "agent_id": event.agent_id, "task": event.task[:200]}
        if isinstance(event, SubagentFinished):
            return {"type": "subagent_finished", "ts": ts,
                    "agent_id": event.agent_id, "status": event.status,
                    "preview": event.preview}
        if isinstance(event, ContextCompressed):
            return {"type": "context_compressed", "ts": ts,
                    "before_tokens": event.before_tokens, "after_tokens": event.after_tokens}
        if isinstance(event, RunFinished):
            return {"type": "run_finished", "ts": ts}
        return None  # 其余事件（delta 等）不落盘，避免文件爆炸

    def _write(self, record: dict) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 持久化失败不阻断对话


def _now() -> float:
    return time.time()


def new_session_path(sessions_dir: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return sessions_dir / f"{stamp}-{os.getpid()}.jsonl"


def latest_session(sessions_dir: Path) -> Optional[Path]:
    files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def load_history(path: Path) -> List[Message]:
    """从会话文件恢复历史：取最后一条 history 快照。"""
    last: Optional[List[Message]] = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "history":
                last = history_from_dicts(record.get("messages", []))
    return last or []
