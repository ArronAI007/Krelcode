"""Durable append-only event log with sequence and hash-chain integrity."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence, TextIO

from krelcode.contracts.event_log import (
    EventLogAppendResult,
    EventLogEntry,
    EventLogPort,
)

EVENT_LOG_META_KEY = "_event_log"
EVENT_LOG_VERSION = 1


class _RedactorPort(Protocol):
    def redact(self, text: str) -> str: ...


class LocalEventLog(EventLogPort):
    """Strictly ordered local JSONL event log.

    Legacy JSONL records without ``_event_log`` metadata remain readable. New
    records carry sequence, event id, previous hash and digest metadata while
    preserving the original top-level record shape used by session restore.
    """

    def __init__(
        self,
        path: Path,
        *,
        redactor: Optional[_RedactorPort] = None,
    ) -> None:
        self.path = Path(path)
        self.redactor = redactor
        self._lock = threading.RLock()
        self._latest_sequence = 0
        self._latest_digest = ""
        self._observed_size = -1
        self.last_error = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._scan()

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            self._refresh_if_changed()
            return self._latest_sequence

    def append(
        self,
        payload: Mapping[str, object],
        *,
        event_type: str = "",
        session_id: str = "",
        turn_id: str = "",
    ) -> EventLogAppendResult:
        with self._lock:
            self._refresh_if_changed()
            normalized = _normalize_mapping(payload)
            if self.redactor is not None:
                normalized = _redact_record(normalized, self.redactor)
            sequence = self._latest_sequence + 1
            event_id = uuid.uuid4().hex
            timestamp = _float_or_default(
                normalized.get("ts"), time.time())
            resolved_type = (
                str(event_type or normalized.get("type") or "event").strip()
                or "event"
            )
            digest = _entry_digest(
                sequence=sequence,
                event_id=event_id,
                event_type=resolved_type,
                session_id=str(session_id or ""),
                turn_id=str(turn_id or ""),
                timestamp=timestamp,
                payload=normalized,
                previous_hash=self._latest_digest,
            )
            record = {
                **normalized,
                EVENT_LOG_META_KEY: {
                    "version": EVENT_LOG_VERSION,
                    "sequence": sequence,
                    "event_id": event_id,
                    "event_type": resolved_type,
                    "session_id": str(session_id or ""),
                    "turn_id": str(turn_id or ""),
                    "timestamp": timestamp,
                    "previous_hash": self._latest_digest,
                    "digest": digest,
                },
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                _ensure_line_boundary(self.path)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ) + "\n")
                    handle.flush()
                    _fsync_file(handle)
                self._latest_sequence = sequence
                self._latest_digest = digest
                self._observed_size = self.path.stat().st_size
            except OSError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise
            self.last_error = ""
            return EventLogAppendResult(
                sequence=sequence,
                event_id=event_id,
                digest=digest,
            )

    def read(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> Sequence[EventLogEntry]:
        if limit < 1:
            return ()
        entries = self._entries()
        return tuple(
            entry for entry in entries
            if entry.sequence > max(0, int(after_sequence))
        )[:int(limit)]

    def close(self) -> None:
        """No persistent handle is retained; provided for port parity."""

    def _refresh_if_changed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        except OSError:
            return
        if size != self._observed_size:
            self._scan()

    def _scan(self) -> None:
        self._latest_sequence = 0
        self._latest_digest = ""
        for entry in self._entries():
            self._latest_sequence = entry.sequence
            self._latest_digest = entry.digest
        try:
            self._observed_size = self.path.stat().st_size
        except OSError:
            self._observed_size = 0

    def _entries(self) -> list[EventLogEntry]:
        if not self.path.is_file():
            return []
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        entries: list[EventLogEntry] = []
        previous_hash = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, Mapping):
                continue
            sequence = len(entries) + 1
            metadata_raw = record.get(EVENT_LOG_META_KEY)
            metadata = (
                dict(metadata_raw)
                if isinstance(metadata_raw, Mapping) else {}
            )
            payload = {
                str(key): value
                for key, value in record.items()
                if key != EVENT_LOG_META_KEY
            }
            explicit_sequence = _positive_int(metadata.get("sequence"))
            if explicit_sequence is not None:
                sequence = explicit_sequence
            event_id = str(
                metadata.get("event_id") or f"legacy-{sequence}")
            event_type = str(
                metadata.get("event_type")
                or payload.get("type")
                or "event"
            )
            session_id = str(metadata.get("session_id") or "")
            turn_id = str(metadata.get("turn_id") or "")
            timestamp = _float_or_default(
                metadata.get("timestamp", payload.get("ts")),
                time.time(),
            )
            previous = str(
                metadata.get("previous_hash") or previous_hash)
            digest = str(metadata.get("digest") or "")
            if not digest:
                digest = _entry_digest(
                    sequence=sequence,
                    event_id=event_id,
                    event_type=event_type,
                    session_id=session_id,
                    turn_id=turn_id,
                    timestamp=timestamp,
                    payload=payload,
                    previous_hash=previous,
                )
            entries.append(EventLogEntry(
                sequence=sequence,
                event_id=event_id,
                event_type=event_type,
                session_id=session_id,
                turn_id=turn_id,
                timestamp=timestamp,
                payload=payload,
                previous_hash=previous,
                digest=digest,
            ))
            previous_hash = digest
        return entries


def _redact_record(
    record: Mapping[str, object],
    redactor: _RedactorPort,
) -> dict[str, object]:
    rendered = json.dumps(
        record, ensure_ascii=False, sort_keys=True, default=str)
    redacted = redactor.redact(rendered)
    try:
        decoded = json.loads(redacted)
    except json.JSONDecodeError:
        return {"type": str(record.get("type") or "event"),
                "ts": record.get("ts"), "content": redacted}
    return _normalize_mapping(decoded) if isinstance(decoded, Mapping) else {
        "type": str(record.get("type") or "event"),
        "ts": record.get("ts"),
        "content": redacted,
    }


def _entry_digest(
    *,
    sequence: int,
    event_id: str,
    event_type: str,
    session_id: str,
    turn_id: str,
    timestamp: float,
    payload: Mapping[str, object],
    previous_hash: str,
) -> str:
    value = {
        "sequence": sequence,
        "event_id": event_id,
        "event_type": event_type,
        "session_id": session_id,
        "turn_id": turn_id,
        "timestamp": timestamp,
        "payload": payload,
        "previous_hash": previous_hash,
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_mapping(value: Mapping[str, object]) -> dict[str, object]:
    normalized = _normalize(value)
    return normalized if isinstance(normalized, dict) else {}


def _normalize(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _ensure_line_boundary(path: Path) -> None:
    try:
        if path.stat().st_size <= 0:
            return
        with open(path, "rb+") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) not in {b"\n", b"\r"}:
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
    except OSError:
        return


def _fsync_file(handle: TextIO) -> None:
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _positive_int(value: object) -> Optional[int]:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _float_or_default(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "EVENT_LOG_META_KEY",
    "EVENT_LOG_VERSION",
    "LocalEventLog",
]
