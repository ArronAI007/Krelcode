"""Bounded, private local queue for content-free telemetry events."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .privacy import TelemetryPrivacyError, validate_event


class TelemetryStoreError(RuntimeError):
    """Telemetry queue persistence failed."""


@dataclass(frozen=True)
class TelemetryStoreStatus:
    path: Path
    events: int
    bytes: int
    oldest_ts: int
    newest_ts: int
    valid: bool
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "events": self.events,
            "bytes": self.bytes,
            "oldest_ts": self.oldest_ts,
            "newest_ts": self.newest_ts,
            "valid": self.valid,
            "error": self.error,
        }


def _install_id(path: Path) -> str:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise TelemetryStoreError("拒绝读取 symlink telemetry install id")
    if path.is_file():
        try:
            os.chmod(path, 0o600)
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TelemetryStoreError("telemetry install id 读取失败") from exc
        if value:
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
                raise TelemetryStoreError("telemetry install id 格式无效")
            return value
    value = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise TelemetryStoreError("拒绝写入 symlink telemetry install id 临时文件")
    try:
        temporary.write_text(value + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise TelemetryStoreError("telemetry install id 写入失败") from exc
    return value


class TelemetryStore:
    """JSONL queue with strict schema checks and bounded retention."""

    def __init__(
        self,
        path: Path,
        *,
        install_id_path: Path,
        max_events: int = 10_000,
        max_bytes: int = 10_000_000,
        retention_days: int = 30,
    ) -> None:
        self.path = Path(path).expanduser()
        self.install_id_path = Path(install_id_path).expanduser()
        self.max_events = max(1, int(max_events))
        self.max_bytes = max(1_024, int(max_bytes))
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.RLock()
        self._install_id: str | None = None
        self._event_count: int | None = None

    @property
    def install_id(self) -> str:
        with self._lock:
            if self._install_id is None:
                self._install_id = _install_id(self.install_id_path)
            return self._install_id

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

    def _read_raw(self) -> tuple[list[dict[str, Any]], int, str]:
        if self.path.is_symlink():
            return [], 0, "拒绝读取 symlink telemetry queue"
        if not self.path.is_file():
            return [], 0, ""
        events: list[dict[str, Any]] = []
        invalid = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        event = validate_event(json.loads(line))
                    except (json.JSONDecodeError, TelemetryPrivacyError):
                        invalid += 1
                        continue
                    events.append(event)
        except OSError as exc:
            return [], 0, f"telemetry queue 读取失败: {type(exc).__name__}"
        error = f"丢弃 {invalid} 条无效遥测事件" if invalid else ""
        return events, invalid, error

    def _write_locked(self, events: Iterable[Mapping[str, Any]]) -> None:
        if self.path.is_symlink():
            raise TelemetryStoreError("拒绝写入 symlink telemetry queue")
        self._ensure_parent()
        materialized = [dict(event) for event in events]
        temporary = self.path.with_name(self.path.name + ".tmp")
        encoded = "".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            for event in materialized
        )
        if temporary.is_symlink():
            raise TelemetryStoreError("拒绝写入 symlink telemetry 临时文件")
        try:
            temporary.write_text(encoded, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            self._event_count = len(materialized)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise TelemetryStoreError("telemetry queue 写入失败") from exc

    def _compact_locked(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cutoff = int(time.time()) - self.retention_days * 86_400
        retained = [
            event for event in events
            if int(event.get("ts", 0) or 0) >= cutoff
        ]
        if len(retained) > self.max_events:
            retained = retained[-self.max_events:]
        while retained:
            encoded = "".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                for event in retained
            ).encode("utf-8")
            if len(encoded) <= self.max_bytes:
                break
            retained.pop(0)
        return retained

    def append(
        self,
        event: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        if self.path.is_symlink():
            raise TelemetryStoreError("拒绝写入 symlink telemetry queue")
        payload = dict(event)
        payload.setdefault("event_id", uuid.uuid4().hex)
        payload.setdefault("ts", int(time.time() if now is None else now))
        payload.setdefault("install_id", self.install_id)
        normalized = validate_event(payload)
        with self._lock:
            if self.path.is_symlink():
                raise TelemetryStoreError("拒绝写入 symlink telemetry queue")
            self._ensure_parent()
            if self._event_count is None:
                existing, _, _ = self._read_raw()
                self._event_count = len(existing)
            line = json.dumps(
                normalized, ensure_ascii=False, sort_keys=True) + "\n"
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path, flags, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                raise TelemetryStoreError("telemetry queue 追加失败") from exc
            self._event_count += 1
            size = self.path.stat().st_size if self.path.is_file() else 0
            if size > self.max_bytes or self._event_count > self.max_events:
                events, _, _ = self._read_raw()
                self._write_locked(self._compact_locked(events))
        return normalized

    def events(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            events, _, _ = self._read_raw()
            self._event_count = len(events)
            return tuple(events)

    def take_batch(self, limit: int = 50) -> tuple[dict[str, Any], ...]:
        with self._lock:
            events, _, _ = self._read_raw()
            self._event_count = len(events)
            return tuple(events[:max(1, int(limit))])

    def acknowledge(self, event_ids: Iterable[str]) -> int:
        remove = {str(item) for item in event_ids}
        if not remove:
            return 0
        with self._lock:
            events, _, _ = self._read_raw()
            retained = [
                event for event in events
                if str(event.get("event_id", "")) not in remove
            ]
            removed = len(events) - len(retained)
            self._write_locked(retained)
            return removed

    def prune(self) -> TelemetryStoreStatus:
        with self._lock:
            events, _, error = self._read_raw()
            self._event_count = len(events)
            compacted = self._compact_locked(events)
            if len(compacted) != len(events) or not self.path.is_file():
                self._write_locked(compacted)
            return self.status(error=error)

    def status(self, *, error: str = "") -> TelemetryStoreStatus:
        with self._lock:
            events, _, read_error = self._read_raw()
            self._event_count = len(events)
            timestamps = [int(item.get("ts", 0) or 0) for item in events]
            try:
                size = self.path.stat().st_size if self.path.is_file() else 0
            except OSError as exc:
                read_error = read_error or (
                    f"telemetry queue 状态读取失败: {type(exc).__name__}")
                size = 0
            return TelemetryStoreStatus(
                path=self.path,
                events=len(events),
                bytes=size,
                oldest_ts=min(timestamps) if timestamps else 0,
                newest_ts=max(timestamps) if timestamps else 0,
                valid=not bool(read_error or error),
                error=read_error or error,
            )

    def clear(self) -> bool:
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
                self._event_count = 0
                return True
            except OSError as exc:
                raise TelemetryStoreError("telemetry queue 删除失败") from exc

    def export(self, destination: Path) -> Path:
        destination = Path(destination).expanduser()
        if destination.is_symlink():
            raise TelemetryStoreError("拒绝导出到 symlink 目标")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        events = self.events()
        encoded = "".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            for event in events
        )
        temporary = destination.with_name(destination.name + ".tmp")
        if temporary.is_symlink():
            raise TelemetryStoreError("拒绝写入 symlink telemetry 导出临时文件")
        try:
            temporary.write_text(encoded, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise TelemetryStoreError("telemetry queue 导出失败") from exc
        return destination

    def summary(self) -> dict[str, Any]:
        events = self.events()
        by_event = Counter(str(item.get("event", "")) for item in events)
        tools: Counter[str] = Counter()
        tool_errors: Counter[str] = Counter()
        errors: Counter[str] = Counter()
        outcomes: Counter[str] = Counter()
        for event in events:
            data = event.get("data") or {}
            if event.get("event") == "tool_call":
                category = str(data.get("tool_category", "other"))
                tools[category] += 1
                if not bool(data.get("success", False)):
                    tool_errors[category] += 1
            elif event.get("event") == "error":
                errors[str(data.get("error_category", "unknown"))] += 1
            elif event.get("event") == "run_finished":
                outcomes[str(data.get("outcome", "unknown"))] += 1
        tool_total = sum(tools.values())
        tool_failed = sum(tool_errors.values())
        return {
            "schema_version": 1,
            "events": len(events),
            "event_types": dict(sorted(by_event.items())),
            "tool_calls": tool_total,
            "tool_failures": tool_failed,
            "tool_error_rate": tool_failed / tool_total if tool_total else 0.0,
            "tools": {
                category: {
                    "calls": count,
                    "failures": tool_errors[category],
                }
                for category, count in sorted(tools.items())
            },
            "errors": dict(sorted(errors.items())),
            "run_outcomes": dict(sorted(outcomes.items())),
        }


__all__ = [
    "TelemetryStore",
    "TelemetryStoreError",
    "TelemetryStoreStatus",
]
