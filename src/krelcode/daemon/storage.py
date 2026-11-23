"""Durable daemon metadata and append-only output/event logs.

Metadata uses atomic replacement.  Output streams use append-only JSONL with a
stable sequence, so reconnecting clients can continue from a cursor without
re-reading the whole file.  Corrupt records are isolated and surfaced rather
than interpreted as trusted state.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core.redact import SecretRedactor
from ..jobs.shell import ShellJobInfo, ShellJobStatus
from .paths import DaemonPaths, DaemonStateError, atomic_write_json, read_json


@dataclass(frozen=True)
class DurableJobPage:
    job: ShellJobInfo
    text: str
    next_sequence: int
    truncated: bool = False


@dataclass
class DurableJobRecord:
    job_id: str
    command: str = ""
    cwd: str = ""
    pid: int = 0
    status: str = ShellJobStatus.RUNNING.value
    started_at: float = 0.0
    ended_at: Optional[float] = None
    exit_code: Optional[int] = None
    latest_sequence: int = 0
    log_path: str = ""
    daemon_instance_id: str = ""
    schema_version: int = 1
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DurableJobRecord":
        job_id = str(raw.get("job_id") or "").strip()
        if not job_id:
            raise DaemonStateError("job record 缺少 job_id")
        status = str(raw.get("status") or "lost")
        if status not in {item.value for item in ShellJobStatus}:
            raise DaemonStateError(f"job record 状态非法: {status}")
        return cls(
            job_id=job_id,
            command=str(raw.get("command") or ""),
            cwd=str(raw.get("cwd") or ""),
            pid=int(raw.get("pid") or 0),
            status=status,
            started_at=float(raw.get("started_at") or 0.0),
            ended_at=(
                float(raw["ended_at"])
                if raw.get("ended_at") is not None else None),
            exit_code=(
                int(raw["exit_code"])
                if raw.get("exit_code") is not None else None),
            latest_sequence=int(raw.get("latest_sequence") or 0),
            log_path=str(raw.get("log_path") or ""),
            daemon_instance_id=str(raw.get("daemon_instance_id") or ""),
            schema_version=int(raw.get("schema_version") or 1),
            error=str(raw.get("error") or ""),
            extra={
                str(k): v for k, v in raw.items()
                if k not in {
                    "job_id", "command", "cwd", "pid", "status", "started_at",
                    "ended_at", "exit_code", "latest_sequence", "log_path",
                    "daemon_instance_id", "schema_version", "error",
                }
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "daemon_instance_id": self.daemon_instance_id,
            "job_id": self.job_id,
            "command": self.command,
            "cwd": self.cwd,
            "pid": self.pid,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "latest_sequence": self.latest_sequence,
            "log_path": self.log_path,
            "error": self.error,
            **self.extra,
        }

    def info(self) -> ShellJobInfo:
        return ShellJobInfo(
            job_id=self.job_id,
            command=self.command,
            cwd=self.cwd,
            pid=self.pid,
            status=ShellJobStatus(self.status),
            started_at=self.started_at,
            ended_at=self.ended_at,
            exit_code=self.exit_code,
            latest_sequence=self.latest_sequence,
            log_path=self.log_path,
        )


class JobRecordStore:
    def __init__(
        self,
        paths: DaemonPaths,
        *,
        redactor: Optional[SecretRedactor] = None,
        max_page_chars: int = 100_000,
    ) -> None:
        self.paths = paths.ensure()
        self.redactor = redactor
        self.max_page_chars = max(1_000, int(max_page_chars))
        self.issues: List[str] = []

    def _record_path(self, job_id: str) -> Path:
        safe = _safe_id(job_id)
        return self.paths.jobs_dir / f"{safe}.json"

    def _events_path(self, job_id: str) -> Path:
        safe = _safe_id(job_id)
        return self.paths.jobs_dir / f"{safe}.events.jsonl"

    def save(self, record: DurableJobRecord) -> None:
        record.command = self._redact(record.command)
        record.error = self._redact(record.error)
        atomic_write_json(self._record_path(record.job_id), record.to_dict())

    def upsert_from_info(
        self,
        info: ShellJobInfo,
        *,
        daemon_instance_id: str,
        error: str = "",
    ) -> DurableJobRecord:
        current = self.load(info.job_id)
        record = DurableJobRecord(
            job_id=info.job_id,
            command=info.command,
            cwd=info.cwd,
            pid=info.pid,
            status=info.status.value,
            started_at=info.started_at,
            ended_at=info.ended_at,
            exit_code=info.exit_code,
            latest_sequence=max(info.latest_sequence, current.latest_sequence if current else 0),
            log_path=info.log_path,
            daemon_instance_id=daemon_instance_id,
            error=error or (current.error if current else ""),
        )
        self.save(record)
        return record

    def append_output(self, job_id: str, sequence: int, text: str, ts: Optional[float] = None) -> None:
        record = self.load(job_id)
        if record is None:
            record = DurableJobRecord(
                job_id=job_id,
                started_at=float(ts or time.time()),
                daemon_instance_id="",
            )
        safe = self._redact(text)
        event = {
            "type": "output",
            "sequence": max(1, int(sequence)),
            "ts": float(ts if ts is not None else time.time()),
            "text": safe,
        }
        self._append_jsonl(self._events_path(job_id), event)
        if sequence > record.latest_sequence:
            record.latest_sequence = int(sequence)
            self.save(record)

    def mark_terminal(
        self,
        job_id: str,
        *,
        status: str,
        exit_code: Optional[int],
        ended_at: Optional[float] = None,
        error: str = "",
    ) -> Optional[DurableJobRecord]:
        record = self.load(job_id)
        if record is None:
            return None
        record.status = str(status)
        record.exit_code = exit_code
        record.ended_at = float(ended_at if ended_at is not None else time.time())
        record.error = self._redact(error)
        self.save(record)
        return record

    def load(self, job_id: str) -> Optional[DurableJobRecord]:
        try:
            raw = read_json(self._record_path(job_id))
        except DaemonStateError as exc:
            self.issues.append(str(exc))
            return None
        if raw is None:
            return None
        try:
            return DurableJobRecord.from_dict(raw)
        except (DaemonStateError, TypeError, ValueError) as exc:
            self.issues.append(f"损坏 job record {job_id}: {exc}")
            return None

    def list(self) -> List[DurableJobRecord]:
        records: List[DurableJobRecord] = []
        for path in sorted(self.paths.jobs_dir.glob("*.json")):
            try:
                raw = read_json(path)
                if raw is not None:
                    records.append(DurableJobRecord.from_dict(raw))
            except (DaemonStateError, TypeError, ValueError) as exc:
                self.issues.append(f"损坏 job record {path.name}: {exc}")
        return records

    def reconcile(self, instance_id: str) -> List[DurableJobRecord]:
        """Mark records owned by a dead/previous daemon as lost, never adopted."""
        changed: List[DurableJobRecord] = []
        for record in self.list():
            if record.status != ShellJobStatus.RUNNING.value:
                continue
            if record.daemon_instance_id == instance_id:
                continue
            record.status = ShellJobStatus.LOST.value
            record.ended_at = record.ended_at or time.time()
            record.error = (
                "daemon 已重启，旧 OS 进程无法安全接管；"
                "已保留日志与最终可得序列，未按 PID 自动终止")
            self.save(record)
            changed.append(record)
        return changed

    def output(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        max_chars: int = 30_000,
    ) -> Optional[DurableJobPage]:
        record = self.load(job_id)
        if record is None:
            return None
        cursor = max(0, int(after_sequence))
        limit = max(1, min(int(max_chars), self.max_page_chars))
        selected: List[Tuple[int, str]] = []
        latest = record.latest_sequence
        try:
            with self._events_path(job_id).open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                        sequence = int(item.get("sequence") or 0)
                        if item.get("type") != "output" or sequence <= cursor:
                            continue
                        selected.append((sequence, str(item.get("text") or "")))
                        latest = max(latest, sequence)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
        except FileNotFoundError:
            pass
        text = "".join(item[1] for item in selected)
        truncated = False
        if len(text) > limit:
            text = text[-limit:]
            truncated = True
        next_sequence = selected[-1][0] if selected else max(cursor, latest)
        record.latest_sequence = max(record.latest_sequence, latest)
        return DurableJobPage(
            job=record.info(),
            text=text,
            next_sequence=next_sequence,
            truncated=truncated,
        )

    @staticmethod
    def _append_jsonl(path: Path, value: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            line = (json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str,
            ) + "\n").encode("utf-8")
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _redact(self, text: str) -> str:
        return self.redactor.redact(str(text)) if self.redactor is not None else str(text)


class AgentRecordStore:
    """Content-light durable projection of SubagentManager state."""

    def __init__(self, paths: DaemonPaths, *, redactor: Optional[SecretRedactor] = None) -> None:
        self.paths = paths.ensure()
        self.redactor = redactor
        self.issues: List[str] = []

    def _path(self, agent_id: str) -> Path:
        return self.paths.agents_dir / f"{_safe_id(agent_id)}.json"

    def upsert(self, agent_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        current = self.load(agent_id) or {"agent_id": agent_id}
        record = {**current, **values, "agent_id": agent_id,
                  "updated_at": time.time(), "schema_version": 1}
        record = _redact_record(record, self.redactor)
        atomic_write_json(self._path(agent_id), record)
        return record

    def load(self, agent_id: str) -> Optional[Dict[str, Any]]:
        try:
            raw = read_json(self._path(agent_id))
        except DaemonStateError as exc:
            self.issues.append(str(exc))
            return None
        return raw

    def list(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for path in sorted(self.paths.agents_dir.glob("*.json")):
            try:
                raw = read_json(path)
                if raw is not None and raw.get("agent_id"):
                    records.append(raw)
            except DaemonStateError as exc:
                self.issues.append(f"损坏 agent record {path.name}: {exc}")
        records.sort(
            key=lambda item: float(item.get("updated_at") or item.get("started_at") or 0),
            reverse=True,
        )
        return records

    def reconcile(self, instance_id: str) -> List[Dict[str, Any]]:
        changed: List[Dict[str, Any]] = []
        for record in self.list():
            if record.get("status") not in {"running", "paused", "queued"}:
                continue
            if record.get("daemon_instance_id") == instance_id:
                continue
            updated = self.upsert(str(record["agent_id"]), {
                "status": "lost",
                "error": (
                    "daemon 已重启，旧 Python coroutine 无法原地恢复；"
                    "sidechain 与已完成结果仍可审计，可基于新任务重新 spawn"),
                "ended_at": time.time(),
            })
            changed.append(updated)
        return changed


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value))
    if not safe or safe in {".", ".."}:
        raise DaemonStateError("非法空 record id")
    return safe[:180]


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
        return {str(k): _redact_record(v, redactor) for k, v in value.items()}
    return value


__all__ = [
    "AgentRecordStore",
    "DurableJobPage",
    "DurableJobRecord",
    "JobRecordStore",
]
