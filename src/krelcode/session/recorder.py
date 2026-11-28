"""会话持久化：EventBus 的旁路订阅者（内核对其零感知）。

JSONL 格式，两类记录：
- 审计事件流：user_input / tool_call / tool_result / permission_denied /
  context_assembled / context_compressed / run_finished（每事件一行，
  便于 grep 与回放）
- history 快照：每次 RunFinished 落一行完整对话历史（--resume 用最后一条恢复）

文件：<workspace>/.krelcode/sessions/YYYYmmdd-HHMMSS-<pid>.jsonl
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.events import (
    AcceptanceEvaluated,
    AgentEvent,
    CompactCheckpointCreated,
    ContextAssembled,
    ContextCheckpointCreated,
    ContextCompressed,
    ContextDropped,
    ContextMicrocompacted,
    EventBus,
    HookTriggered,
    KnowledgeLoaded,
    LoopPhaseChanged,
    MemoryCandidateObserved,
    MemoryLoaded,
    MemoryWritten,
    ObservationRecorded,
    PlanUpdated,
    PermissionDenied,
    RunCancelled,
    RunFinished,
    RunFailed,
    RunStarted,
    SecurityDecisionRecorded,
    ShellJobFinished,
    ShellJobStarted,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
    ToolCallCompleted,
    ToolCallStarted,
    UserInputRequested,
    UserInputResolved,
    UserSteered,
)
from ..core.messages import Message
from ..core.redact import SecretRedactor
from ..core.codec import history_to_dicts, history_from_dicts
from ..storage.event_log import LocalEventLog


class SessionRecorder:
    def __init__(
        self,
        path: Path,
        get_history: Callable[[], List[Message]],
        redactor: Optional[SecretRedactor] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """get_history: 无参可调用，返回当前 List[Message]（避免循环依赖注入 loop）。"""
        self.path = Path(path)
        self._get_history = get_history
        self._redactor = redactor
        self.metadata = dict(metadata or {})
        self._unsub: Optional[object] = None
        self._session_start_written = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.event_log = LocalEventLog(
            self.path,
            redactor=self._redactor,
        )

    def attach(self, bus: EventBus) -> None:
        self._unsub = bus.subscribe(self.on_event)
        if self.metadata and not self._session_start_written:
            self._write({
                "type": "session_start",
                "ts": _now(),
                **self.metadata,
            })
            self._session_start_written = True

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()  # type: ignore[operator]
            self._unsub = None

    async def on_event(self, event: AgentEvent) -> None:
        record = self._event_record(event)
        if record is None:
            return
        self._write(record)
        if isinstance(event, (RunFinished, RunCancelled, RunFailed)):
            # 每轮结束落一份完整历史快照（resume 取最后一条）
            self.flush_history(turn_id=event.turn_id)

    def flush_history(self, *, turn_id: str = "") -> None:
        """持久化当前完整历史；取消/分叉/rewind 后均以最后一条为准。"""
        record = {"type": "history", "ts": _now(),
                  "messages": history_to_dicts(self._get_history())}
        if turn_id:
            record["turn_id"] = turn_id
        self._write(record)

    def record_rewind(
        self,
        *,
        target_index: int,
        target_turn_id: str,
        removed_turns: int,
        restored: int = 0,
        deleted: int = 0,
    ) -> None:
        """记录一次联合回退，并立即写入回退后的权威 history 快照。"""
        self._write({
            "type": "rewind",
            "ts": _now(),
            "target_index": target_index,
            "target_turn_id": target_turn_id,
            "removed_turns": removed_turns,
            "restored_files": restored,
            "deleted_files": deleted,
        })
        self.flush_history()

    def update_metadata(self, **metadata: Any) -> None:
        """追加一条可覆盖的会话元数据（当前用于 rename）。"""
        self.metadata.update(metadata)
        self._write({"type": "session_meta", "ts": _now(), **metadata})

    def record_context_checkpoint(self, record: dict[str, Any]) -> None:
        """Append a content-light ContextCheckpoint reference to this session."""
        checkpoint_id = str(record.get("id", record.get("checkpoint_id", "")) or "")
        if not checkpoint_id:
            return
        payload = {
            "type": "context_checkpoint",
            "ts": _now(),
            "checkpoint_id": checkpoint_id,
            **{
                key: value for key, value in record.items()
                if key not in {"type", "id", "checkpoint_id", "ts"}
            },
        }
        self._write(payload)

    def _event_record(self, event: AgentEvent) -> Optional[dict[str, Any]]:
        ts = _now()
        if isinstance(event, RunStarted):
            record = {"type": "user_input", "ts": ts, "content": event.user_input}
            if event.turn_id:
                record["turn_id"] = event.turn_id
            if event.session_id:
                record["session_id"] = event.session_id
            if event.agent_contract:
                record["agent_contract"] = dict(event.agent_contract)
            return record
        if isinstance(event, AcceptanceEvaluated):
            return {
                "type": "acceptance_evaluated",
                "ts": ts,
                "acceptance": dict(event.acceptance),
            }
        if isinstance(event, LoopPhaseChanged):
            return {
                "type": "loop_phase",
                "ts": ts,
                "phase": event.phase,
                "previous_phase": event.previous_phase,
                "step": event.step,
                "reason": event.reason,
                "transition": dict(event.transition),
                "turn_id": event.transition.get("turn_id", ""),
            }
        if isinstance(event, ObservationRecorded):
            return {
                "type": "observation",
                "ts": ts,
                "observation": dict(event.observation),
            }
        if isinstance(event, SecurityDecisionRecorded):
            return {
                "type": "security_decision",
                "ts": ts,
                "tool_call_id": event.tool_call_id,
                "security": dict(event.security),
            }
        if isinstance(event, ToolCallStarted):
            return {"type": "tool_call", "ts": ts, "id": event.tool_call.id,
                    "name": event.tool_call.name, "arguments": event.tool_call.arguments}
        if isinstance(event, ToolCallCompleted):
            content = event.content
            if len(content) > 500:
                content = content[:500] + "…"
            return {"type": "tool_result", "ts": ts, "id": event.tool_call.id,
                    "is_error": event.is_error, "content": content}
        if isinstance(event, HookTriggered):
            return {"type": "hook_triggered", "ts": ts,
                    "trigger": event.trigger, "matched": event.matched,
                    "blocked": event.blocked, "tool": event.tool_name,
                    "reason": event.reason,
                    "system_message": event.system_message}
        if isinstance(event, PermissionDenied):
            return {"type": "permission_denied", "ts": ts,
                    "name": event.tool_call.name, "reason": event.reason}
        if isinstance(event, UserSteered):
            return {"type": "user_steered", "ts": ts, "content": event.text[:200]}
        if isinstance(event, ShellJobStarted):
            return {"type": "shell_job_started", "ts": ts,
                    "job_id": event.job_id, "command": event.command,
                    "pid": event.pid, "cwd": event.cwd}
        if isinstance(event, ShellJobFinished):
            return {"type": "shell_job_finished", "ts": ts,
                    "job_id": event.job_id, "command": event.command,
                    "status": event.status, "exit_code": event.exit_code,
                    "preview": event.preview[:600]}
        if isinstance(event, SubagentSpawned):
            return {"type": "subagent_spawned", "ts": ts,
                    "agent_id": event.agent_id, "task": event.task[:200],
                    "definition": event.definition}
        if isinstance(event, SubagentFeedUpdated):
            return {"type": "subagent_feed", "ts": ts,
                    "agent_id": event.agent_id, "sequence": event.sequence,
                    "kind": event.kind, "text": event.text[:2_000],
                    "data": event.data}
        if isinstance(event, SubagentStateChanged):
            return {"type": "subagent_state", "ts": ts,
                    "agent_id": event.agent_id, "status": event.status,
                    "reason": event.reason[:300]}
        if isinstance(event, SubagentFinished):
            return {"type": "subagent_finished", "ts": ts,
                    "agent_id": event.agent_id, "status": event.status,
                    "preview": event.preview}
        if isinstance(event, ContextAssembled):
            record = {
                "type": "context_assembled",
                "ts": ts,
                **event.snapshot.as_record(),
            }
            if event.ledger is not None:
                record["cache_ledger"] = dict(event.ledger)
            return record
        if isinstance(event, ContextDropped):
            return {
                "type": "context_dropped",
                "ts": ts,
                "items": list(event.items),
                "reason": event.reason,
            }
        if isinstance(event, MemoryLoaded):
            return {
                "type": "memory_loaded",
                "ts": ts,
                "entry_ids": list(event.entry_ids),
                "scope_counts": dict(event.scope_counts),
                "tokens": event.tokens,
                "source": event.source,
            }
        if isinstance(event, KnowledgeLoaded):
            return {
                "type": "knowledge_loaded",
                "ts": ts,
                "document_ids": list(event.document_ids),
                "chunk_ids": list(event.chunk_ids),
                "sources": list(event.sources),
                "scope_counts": dict(event.scope_counts),
                "tokens": event.tokens,
            }
        if isinstance(event, MemoryCandidateObserved):
            return {
                "type": "memory_candidate",
                "ts": ts,
                "candidate_id": event.candidate_id,
                "status": event.status,
                "trigger": event.trigger,
                "confidence": event.confidence,
                "conflict_count": event.conflict_count,
                "digest": event.digest,
            }
        if isinstance(event, MemoryWritten):
            return {
                "type": "memory_written",
                "ts": ts,
                "memory_id": event.memory_id,
                "action": event.action,
                "scope": event.scope,
                "kind": event.kind,
                "tokens": event.tokens,
                "source": event.source,
            }
        if isinstance(event, ContextMicrocompacted):
            return {
                "type": "context_microcompacted", "ts": ts,
                "trigger": event.trigger,
                "before_tokens": event.before_tokens,
                "after_tokens": event.after_tokens,
                "compacted_count": event.compacted_count,
                "preserved_count": event.preserved_count,
                "protected_count": event.protected_count,
                "summarized_count": event.summarized_count,
                "referenced_count": event.referenced_count,
            }
        if isinstance(event, ContextCompressed):
            return {
                "type": "context_compressed",
                "ts": ts,
                "before_tokens": event.before_tokens,
                "after_tokens": event.after_tokens,
                "strategy": event.strategy,
                "summary_source": event.summary_source,
                "generation": event.generation,
                "high_watermark_tokens": event.high_watermark_tokens,
                "low_watermark_tokens": event.low_watermark_tokens,
                "source_savings": dict(event.source_savings),
                "checkpoint_id": event.checkpoint_id,
            }
        if isinstance(event, CompactCheckpointCreated):
            return {
                "type": "compact_checkpoint",
                "ts": ts,
                "checkpoint_id": event.checkpoint_id,
                "generation": event.generation,
                "before_tokens": event.before_tokens,
                "after_tokens": event.after_tokens,
                "summary_digest": event.summary_digest,
                "strategy": event.strategy,
                "trigger": event.trigger,
                "source_savings": dict(event.source_savings),
            }
        if isinstance(event, ContextCheckpointCreated):
            return {
                "type": "context_checkpoint",
                "ts": ts,
                "checkpoint_id": event.checkpoint_id,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "phase": event.phase,
                "history_hash": event.history_hash,
                "file_count": event.file_count,
                "memory_count": event.memory_count,
                "compact_checkpoint_id": event.compact_checkpoint_id,
                "context_plan_hash": event.context_plan_hash,
            }
        if isinstance(event, PlanUpdated):
            record = {"type": "plan_updated", "ts": ts,
                      "items": list(event.items), "statuses": list(event.statuses),
                      "active_index": event.active_index,
                      "explanation": event.explanation}
            if event.turn_id:
                record["turn_id"] = event.turn_id
            return record
        if isinstance(event, UserInputRequested):
            return {"type": "user_input_requested", "ts": ts,
                    "request_id": event.request_id, "prompt": event.prompt,
                    "options": list(event.options),
                    "allow_free_text": event.allow_free_text}
        if isinstance(event, UserInputResolved):
            return {"type": "user_input_resolved", "ts": ts,
                    "request_id": event.request_id, "answered": event.answered}
        if isinstance(event, RunFinished):
            record = {"type": "run_finished", "ts": ts}
            if event.turn_id:
                record["turn_id"] = event.turn_id
            return record
        if isinstance(event, RunFailed):
            record = {
                "type": "run_failed",
                "ts": ts,
                "reason": event.reason,
                "termination": event.termination,
                "detail": event.detail[:1_000],
            }
            if event.turn_id:
                record["turn_id"] = event.turn_id
            return record
        if isinstance(event, RunCancelled):
            record = {"type": "run_cancelled", "ts": ts, "reason": event.reason}
            if event.turn_id:
                record["turn_id"] = event.turn_id
            return record
        return None  # 其余事件（delta 等）不落盘，避免文件爆炸

    def _write(self, record: dict[str, Any]) -> None:
        try:
            self.event_log.append(
                record,
                event_type=str(record.get("type") or ""),
                session_id=str(
                    record.get("session_id")
                    or self.metadata.get("session_id")
                    or ""
                ),
                turn_id=str(record.get("turn_id") or ""),
            )
        except (OSError, TypeError, ValueError):
            pass  # 持久化失败不阻断对话


def _now() -> float:
    return time.time()


def new_session_path(sessions_dir: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return sessions_dir / f"{stamp}-{os.getpid()}-{suffix}.jsonl"


def latest_session(sessions_dir: Path) -> Optional[Path]:
    files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def load_history(path: Path) -> List[Message]:
    """从会话文件恢复历史：取最后一条 history 快照。"""
    if Path(path).suffix.lower() == ".json":
        return _load_native_history(Path(path))
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


def load_latest_context_checkpoint_id(path: Path) -> str:
    """Return the latest full-context checkpoint reference from a session."""
    checkpoint_id = ""
    if Path(path).suffix.lower() == ".json":
        return checkpoint_id
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "context_checkpoint":
                    continue
                candidate = str(record.get("checkpoint_id", "") or "")
                if candidate:
                    checkpoint_id = candidate
    except OSError:
        return ""
    return checkpoint_id


def _load_native_history(path: Path) -> List[Message]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("format") != "krelcode-session":
        return []
    messages = raw.get("messages")
    if not isinstance(messages, list):
        return []
    return history_from_dicts(messages)
