"""Durable task-scoped event journal with stable reconnect cursors."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ...contracts.task_lifecycle import TaskEventCursor, TaskEventJournalPort
from ...core.redact import SecretRedactor


class LocalTaskEventJournal(TaskEventJournalPort):
    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.redactor = redactor
        self._lock = threading.RLock()
        self._memory: list[dict[str, object]] = []
        self._latest: dict[str, TaskEventCursor] = {}
        self._observed_size = -1
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._reload()

    def append(
        self,
        task_id: str,
        payload: Mapping[str, object],
        *,
        event_type: str = "",
        parent_task_id: str = "",
    ) -> TaskEventCursor:
        identifier = str(task_id or "").strip()
        if not identifier:
            raise ValueError("task event 必须提供 task_id")
        with self._lock:
            self._reload_if_changed()
            current = self._latest.get(identifier, TaskEventCursor())
            sequence = current.sequence + 1
            event_id = uuid.uuid4().hex
            timestamp = time.time()
            normalized = _normalize(dict(payload))
            resolved = dict(normalized) if isinstance(normalized, dict) else {}
            if self.redactor is not None:
                rendered = self.redactor.redact(json.dumps(
                    resolved, ensure_ascii=False, sort_keys=True, default=str))
                try:
                    decoded = json.loads(rendered)
                except json.JSONDecodeError:
                    decoded = {"redacted": rendered}
                resolved = decoded if isinstance(decoded, dict) else {}
            digest = _digest(
                identifier,
                parent_task_id,
                sequence,
                event_id,
                event_type,
                timestamp,
                resolved,
                current.digest,
            )
            record = {
                "task_id": identifier,
                "parent_task_id": str(parent_task_id or ""),
                "sequence": sequence,
                "event_id": event_id,
                "event_type": str(event_type or resolved.get("type") or "event"),
                "timestamp": timestamp,
                "previous_hash": current.digest,
                "digest": digest,
                "payload": resolved,
            }
            self._write(record)
            cursor = TaskEventCursor(
                sequence=sequence,
                event_id=event_id,
                digest=digest,
                updated_at=timestamp,
            )
            self._latest[identifier] = cursor
            return cursor

    def read(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> Sequence[Mapping[str, object]]:
        identifier = str(task_id or "").strip()
        if not identifier or limit < 1:
            return ()
        with self._lock:
            self._reload_if_changed()
            records = (
                self._read_file() if self.path is not None else self._memory
            )
            return tuple(
                dict(record)
                for record in records
                if str(record.get("task_id") or "") == identifier
                and _int_value(record.get("sequence")) > max(
                    0, int(after_sequence))
            )[:int(limit)]

    def cursor(self, task_id: str) -> TaskEventCursor:
        with self._lock:
            self._reload_if_changed()
            return self._latest.get(str(task_id or ""), TaskEventCursor())

    def _write(self, record: Mapping[str, object]) -> None:
        if self.path is None:
            self._memory.append(dict(record))
            return
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._observed_size = self.path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"task event journal 写入失败: {exc}") from exc

    def _reload_if_changed(self) -> None:
        if self.path is None:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size != self._observed_size:
            self._reload()

    def _reload(self) -> None:
        records = self._read_file() if self.path is not None else self._memory
        latest: dict[str, TaskEventCursor] = {}
        for record in records:
            task_id = str(record.get("task_id") or "")
            if not task_id:
                continue
            latest[task_id] = TaskEventCursor(
                sequence=_int_value(record.get("sequence")),
                event_id=str(record.get("event_id") or ""),
                digest=str(record.get("digest") or ""),
                updated_at=_float_value(record.get("timestamp")),
            )
        self._latest = latest
        try:
            self._observed_size = (
                self.path.stat().st_size if self.path is not None else 0)
        except OSError:
            self._observed_size = 0

    def _read_file(self) -> list[dict[str, object]]:
        assert self.path is not None
        try:
            lines = self.path.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        records: list[dict[str, object]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping):
                records.append(dict(value))
        return records


def _digest(
    task_id: str,
    parent_task_id: str,
    sequence: int,
    event_id: str,
    event_type: str,
    timestamp: float,
    payload: Mapping[str, object],
    previous_hash: str,
) -> str:
    value = {
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "sequence": sequence,
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "payload": payload,
        "previous_hash": previous_hash,
    }
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _normalize(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _int_value(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _float_value(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["LocalTaskEventJournal"]
