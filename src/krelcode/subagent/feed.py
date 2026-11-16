"""子代理 Agent Feed：实时事件、稳定游标与有界内存缓冲。"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Tuple

from ..core.redact import SecretRedactor

DEFAULT_MAX_FEED_ENTRIES = 2_000
DEFAULT_MAX_ENTRY_CHARS = 24_000


@dataclass(frozen=True)
class AgentFeedEntry:
    agent_id: str
    sequence: int
    kind: str
    text: str
    ts: float
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentFeedPage:
    agent_id: str
    entries: Tuple[AgentFeedEntry, ...]
    after_sequence: int
    latest_sequence: int
    dropped_through: int = 0

    @property
    def next_sequence(self) -> int:
        # 分页时必须返回本页最后一条，而不是全局 latest；否则会跳过中间事件。
        return self.entries[-1].sequence if self.entries else self.after_sequence

    @property
    def truncated(self) -> bool:
        return self.after_sequence < self.dropped_through

    def render(self, *, max_chars: int = 40_000) -> str:
        header = (
            f"agent_id: {self.agent_id}\n"
            f"sequence: {self.after_sequence} -> {self.latest_sequence}"
        )
        if self.truncated:
            header += f"\ntruncated: true (dropped through {self.dropped_through})"
        lines = []
        for entry in self.entries:
            text = entry.text
            if not text and entry.data:
                text = json.dumps(entry.data, ensure_ascii=False, sort_keys=True)
            if "\n" in text:
                body = "\n".join("  " + line for line in text.splitlines())
                lines.append(f"[{entry.sequence:06d}] {entry.kind}\n{body}")
            else:
                lines.append(f"[{entry.sequence:06d}] {entry.kind}: {text}")
        body = "\n".join(lines)
        if len(body) > max_chars:
            body = body[-max_chars:]
            header += "\ntruncated: true (display limit)"
        return f"{header}\n--- feed ---\n{body or '（暂无新事件）'}"


class AgentFeed:
    def __init__(
        self,
        agent_id: str,
        *,
        redactor: Optional[SecretRedactor] = None,
        max_entries: int = DEFAULT_MAX_FEED_ENTRIES,
        max_entry_chars: int = DEFAULT_MAX_ENTRY_CHARS,
    ) -> None:
        self.agent_id = agent_id
        self.redactor = redactor
        self.max_entries = max(1, int(max_entries))
        self.max_entry_chars = max(1_000, int(max_entry_chars))
        self._entries: Deque[AgentFeedEntry] = deque()
        self._latest_sequence = 0
        self._dropped_through = 0
        # key -> (未决文本, kind, 最近 data)。暂存可能是 secret 前缀的尾块，
        # 防止 API key 被流式 chunk 拆开后绕过逐块 redactor。
        self._pending_streams: Dict[
            str, Tuple[str, str, Dict[str, Any]]] = {}

    def add(
        self,
        kind: str,
        text: str,
        *,
        ts: float,
        data: Optional[Dict[str, Any]] = None,
    ) -> Optional[AgentFeedEntry]:
        stream_key = _stream_key(kind, data or {})
        if stream_key is not None:
            pending, _, pending_data = self._pending_streams.pop(
                stream_key, ("", kind, {}))
            merged = pending + str(text or "")
            overlap = self._stream_secret_overlap(merged)
            merged_data = dict(pending_data) if not data else dict(data)
            if overlap:
                self._pending_streams[stream_key] = (
                    merged[-overlap:], kind, merged_data)
                merged = merged[:-overlap]
            if not merged:
                return None
            return self._append(kind, merged, ts=ts, data=merged_data)
        return self._append(kind, str(text or ""), ts=ts, data=data)

    def flush_stream(
        self,
        kind: str,
        *,
        ts: float,
        data: Optional[Dict[str, Any]] = None,
    ) -> Optional[AgentFeedEntry]:
        key = _stream_key(kind, data or {})
        if key is None:
            return None
        pending = self._pending_streams.pop(key, None)
        if pending is None:
            return None
        text, pending_kind, pending_data = pending
        return self._append(
            pending_kind, text, ts=ts,
            data=dict(data or pending_data))

    def flush_all(self, *, ts: float) -> Tuple[AgentFeedEntry, ...]:
        entries = []
        for key, (text, kind, data) in list(self._pending_streams.items()):
            self._pending_streams.pop(key, None)
            entries.append(self._append(kind, text, ts=ts, data=data))
        return tuple(entries)

    def _append(
        self,
        kind: str,
        text: str,
        *,
        ts: float,
        data: Optional[Dict[str, Any]],
    ) -> AgentFeedEntry:
        self._latest_sequence += 1
        safe_text = self._redact_text(text)
        if len(safe_text) > self.max_entry_chars:
            half = self.max_entry_chars // 2
            safe_text = (safe_text[:half] + "\n…[feed entry truncated]…\n"
                         + safe_text[-(self.max_entry_chars - half):])
        safe_data = _redact_object(dict(data or {}), self.redactor)
        entry = AgentFeedEntry(
            agent_id=self.agent_id,
            sequence=self._latest_sequence,
            kind=str(kind or "event"),
            text=safe_text,
            ts=float(ts),
            data=safe_data,
        )
        self._entries.append(entry)
        while len(self._entries) > self.max_entries:
            removed = self._entries.popleft()
            self._dropped_through = max(self._dropped_through, removed.sequence)
        return entry

    def page(self, after_sequence: int = 0, limit: int = 200) -> AgentFeedPage:
        after = max(0, int(after_sequence))
        count = max(1, min(int(limit), 1_000))
        entries = tuple(e for e in self._entries if e.sequence > after)[:count]
        return AgentFeedPage(
            agent_id=self.agent_id,
            entries=entries,
            after_sequence=after,
            latest_sequence=self._latest_sequence,
            dropped_through=self._dropped_through,
        )

    @property
    def latest_sequence(self) -> int:
        return self._latest_sequence

    def _redact_text(self, text: str) -> str:
        return self.redactor.redact(text) if self.redactor is not None else text

    def _stream_secret_overlap(self, text: str) -> int:
        if self.redactor is None:
            return 0
        overlap = 0
        for secret in self.redactor.secrets:
            limit = min(len(secret) - 1, len(text))
            for size in range(limit, overlap, -1):
                if text.endswith(secret[:size]):
                    overlap = size
                    break
        return overlap


def _stream_key(kind: str, data: Dict[str, Any]) -> Optional[str]:
    if kind == "assistant_delta":
        return "assistant"
    if kind == "thinking_delta":
        return "thinking"
    if kind == "tool_output":
        call_id = str(data.get("tool_call_id") or "")
        return f"tool:{call_id}" if call_id else "tool"
    return None


def _redact_object(value: Any, redactor: Optional[SecretRedactor]) -> Any:
    if redactor is None:
        return value
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, list):
        return [_redact_object(item, redactor) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_object(item, redactor) for item in value)
    if isinstance(value, dict):
        return {str(key): _redact_object(item, redactor)
                for key, item in value.items()}
    return value
